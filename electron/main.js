const { app, BrowserWindow, ipcMain, screen } = require('electron')
const { spawn } = require('child_process')
const path = require('path')

const BACKEND_PORT = 8765
const PYTHON_BIN   = '/opt/homebrew/opt/python@3.11/bin/python3.11'
const PROJECT_DIR  = path.join(__dirname, '..')

let mainWindow
let pythonProcess

// ── Start Python backend ───────────────────────────────────────────────────
function startPythonServer() {
  pythonProcess = spawn(
    PYTHON_BIN,
    ['-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', String(BACKEND_PORT)],
    { cwd: PROJECT_DIR }
  )
  pythonProcess.stdout.on('data', d => process.stdout.write('[py] ' + d))
  pythonProcess.stderr.on('data', d => process.stderr.write('[py] ' + d))
  pythonProcess.on('exit', code => console.log('[py] exited', code))
}

// ── Create overlay window ──────────────────────────────────────────────────
function createWindow() {
  const { width, height } = screen.getPrimaryDisplay().workAreaSize
  const winW = Math.min(960, width)
  const winH = 230

  mainWindow = new BrowserWindow({
    width:     winW,
    height:    winH,
    minWidth:  320,
    minHeight: 100,
    x: Math.floor((width - winW) / 2),
    y: height - winH - 10,
    transparent:  true,
    frame:        false,
    alwaysOnTop:  true,
    resizable:    true,
    hasShadow:    false,
    skipTaskbar:  false,
    webPreferences: {
      nodeIntegration:  false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  })

  // Stay on top above screen-saver / full-screen apps (Zoom, Meet, etc.)
  mainWindow.setAlwaysOnTop(true, 'screen-saver')
  mainWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true })

  mainWindow.loadFile(path.join(__dirname, 'overlay.html'))
}

// ── App lifecycle ──────────────────────────────────────────────────────────
app.whenReady().then(() => {
  startPythonServer()
  // Give the server ~2 s to boot before opening the window
  setTimeout(createWindow, 2000)

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (pythonProcess) pythonProcess.kill()
  app.quit()
})

// ── IPC ───────────────────────────────────────────────────────────────────
ipcMain.on('set-window-opacity', (_, v) => {
  mainWindow?.setOpacity(Math.min(1, Math.max(0.05, v)))
})

ipcMain.on('quit', () => {
  if (pythonProcess) pythonProcess.kill()
  app.quit()
})
