const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
  quit:              ()    => ipcRenderer.send('quit'),
  setWindowOpacity:  (v)  => ipcRenderer.send('set-window-opacity', v),
  backendPort:       8765,
})
