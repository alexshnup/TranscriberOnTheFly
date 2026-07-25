# TranscriberOnTheFly

Real-time speech transcription with optional live translation into 12 languages and an interview assistant panel. Two modes:

- **Browser** — full transcript view with two scrollable panels (English + translation)
- **Electron overlay** — transparent always-on-top subtitle window for use during video calls; video shows through the top, subtitles float at the bottom

## How it works

```
Mic → AudioWorklet → WebSocket → Python backend
                                      ↓
                        OpenAI Realtime session (gpt-realtime-whisper)
                                      ↓
                         word-by-word transcript deltas (EN)
                                      ↓
                              ┌───────┴────────┐
                           LLM translation   LLM question detector
                              ↓                    ↓
                        translation (opt)     suggested answer (opt)
                              ↓
                         WebSocket → Browser / Electron UI
```

**Streaming ASR + silence-driven commit:**

- Browser audio is forwarded live into a persistent OpenAI Realtime transcription session (`gpt-realtime-whisper`), which streams text back word-by-word as `...transcription.delta` events — no local batching or re-transcription of overlapping audio
- The backend still does its own RMS-based silence detection on the raw audio (`ENERGY_THRESHOLD`/`SILENCE_COMMIT_SECS`/`MIN_SPEECH_FOR_COMMIT`), but only to decide **when to call `input_audio_buffer.commit()`** on the realtime session — `gpt-realtime-whisper` has no server-side VAD of its own
- Committing finalizes the segment (`...transcription.completed`); if it's shorter than `MIN_WORDS_FOR_COMMIT` words it's merged into the next segment instead of becoming its own paragraph (up to `MAX_PENDING_MERGES` merges before it's committed anyway)
- `WINDOW_MAX_SECONDS` remains a safety net: force-commit long non-stop speech, and drop long stretches of pure silence instead of committing empty audio

## Requirements

- Python 3.11+
- Node.js + npm (for the Electron overlay only)
- Chrome / Firefox / Edge (browser mode — requires AudioWorklet)
- **OpenAI API key (required)** — ASR runs on the Realtime API (`gpt-realtime-whisper`); no other provider offers it
- Groq API key (optional) — only speeds up/cheapens the translation + interview-answer LLM calls

## Installation

```bash
# Python dependencies
/opt/homebrew/opt/python@3.11/bin/pip3.11 install fastapi "uvicorn[standard]" "openai>=2.48" python-dotenv numpy scipy python-multipart

# Electron dependencies (overlay mode only)
cd electron && npm install
```

## Configuration

Add your key(s) to `.env`:

```env
# Required — ASR runs on OpenAI Realtime (gpt-realtime-whisper); no other provider supports it
OPENAI_API_KEY=sk-...

# Optional — speeds up/cheapens translation + interview-answer LLM calls; ASR is unaffected
#GROQ_API_KEY=gsk_...
```

**Model selection:**

| Role | Model |
|---|---|
| ASR (always OpenAI) | `gpt-realtime-whisper` (Realtime API) |
| LLM — if `GROQ_API_KEY` set | `llama-3.1-8b-instant` |
| LLM — otherwise | `gpt-4o-mini` |

## Running — browser mode

**Start:**
```bash
/opt/homebrew/opt/python@3.11/bin/python3.11 -m uvicorn main:app --host 0.0.0.0 --port 8000
```

**Stop:**
```bash
pkill -f "uvicorn main:app" 2>/dev/null; sleep 0.5; echo "killed"
```

**Restart:**
```bash
pkill -f "uvicorn main:app" 2>/dev/null; sleep 0.5; /opt/homebrew/opt/python@3.11/bin/python3.11 -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Then open **http://localhost:8000** in your browser.

## Running — Electron overlay

```bash
cd electron && npm start
```

The Electron app starts the Python backend automatically on port **8765** (separate from the browser version on 8000). No need to start the server manually.

### Overlay controls

| Control | Action |
|---|---|
| Drag bar (dotted strip) | Reposition the window anywhere on screen |
| **START / STOP** | Begin or end recording |
| **◑ slider** | Adjust window transparency (saved across sessions) |
| Language selector | Choose translation language (locked while recording) |
| **💬 button** | Toggle topic hints on/off (locked while recording, persists across restarts) |
| **💡 button** | Toggle automatic hints on/off — off by default (locked while recording, persists across restarts) |
| **✕** | Close the overlay and stop the backend |

### Overlay layout

```
┌─────────────────────────────────────────────────┐
│  [START]  ············  ◑━━━  🇷🇺 Russian 💬💡 [✕]│  ← controls (draggable)
├─────────────────────────────────────────────────┤
│  Confirmed paragraph EN   │  Translation RU      │
│  (dimmed, stays visible)  │  (dimmed)            │
├ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┤
│  Live draft (white)       │  Draft translation   │
├─────────────────────────────────────────────────┤
│  💬 Ansible — short definition (blue, dismiss)   │  ← current topic, if 💬 is on
├─────────────────────────────────────────────────┤
│  💡 Suggested answer (green, dismissable)        │  ← appears on questions
└─────────────────────────────────────────────────┘
```

### Overlay behaviour

- The window floats above all other apps including Zoom and Meet in full-screen mode
- Drag the dotted bar to reposition; resize by dragging any edge or corner
- The opacity slider controls OS-level window transparency — at minimum the window is nearly invisible; at maximum it is fully opaque
- English transcript on the left, translation on the right, aligned paragraph-by-paragraph
- Confirmed text stays on screen and scrolls — text never disappears mid-read
- Auto-scroll follows new content; scroll up manually to read back; **↓ Latest** button jumps back

### Interview assistant (💡 panel)

When the transcribed speech contains a technical question (or, if the **💡** toggle is on, any substantive topic), a green panel appears below the transcript with a concise expert answer (3–5 sentences). Click **✕** to dismiss it. The panel stays hidden for non-technical speech and casual questions.

**Automatic mode (💡 toggle, off by default):** when on, this fires on every committed paragraph, same as before this toggle existed.

**Manual mode (always available, regardless of the 💡 toggle):** select any already-transcribed text with the mouse. If the selection stays unchanged for ~1 second, it's sent as "Tell me about `<selection>`" and the answer appears in the same 💡 panel — no button, no click needed, just hold the selection still. Extending, shrinking, or clearing the selection within that second resets the timer.

### Topic hints (💬 marker, toggleable)

Unlike the 💡 panel (which only fires on direct questions), this reacts to *any* committed segment that mentions a specific identifiable subject — even without a question being asked — and shows a one-line marker: **"Ansible — a short definition"**. Off by default; enable with the **💬** button in the controls bar (locked while recording, like the language selector). To avoid spam, a topic is only announced once — it won't re-fire on every paragraph while the conversation stays on the same subject, only when it actually changes. Always in English, regardless of the translation language.

## Tuning parameters

All in `main.py` at the top of the file:

| Parameter | Default | Effect |
|---|---|---|
| `SILENCE_COMMIT_SECS` | `0.55` | Silence duration that triggers a paragraph commit. Lower = more paragraph breaks (may split mid-sentence). Higher = fewer, larger paragraphs. Typical range: 0.4–0.8 |
| `MIN_WORDS_FOR_COMMIT` | `7` | Minimum words in a segment before it can become its own paragraph; shorter segments are merged into the next one |
| `MAX_PENDING_MERGES` | `3` | Force-commit a short segment anyway after this many merges, so it can't grow unbounded |
| `PENDING_GRACE_SECS` | `1.5` | If a short segment (below `MIN_WORDS_FOR_COMMIT`) is pending and nothing more is said, commit it anyway after this many seconds — otherwise a short-but-complete phrase (e.g. "Let's talk about Linux") could sit unanalyzed indefinitely waiting for more speech that never comes |
| `MIN_SPEECH_FOR_COMMIT` | `0.9` | Minimum seconds of speech accumulated before silence can trigger a commit |
| `WINDOW_MAX_SECONDS` | `8.0` | Force-commit after this many seconds even without silence (for non-stop speakers); also the threshold for dropping long stretches of pure silence |
| `ENERGY_THRESHOLD` | `0.006` | RMS amplitude below which audio is treated as silence. Raise if background noise keeps triggering; lower if mic is weak |
| `REALTIME_DELAY` | `"low"` | `gpt-realtime-whisper`'s latency/accuracy tradeoff for streamed deltas (`minimal`/`low`/`medium`/`high`/`xhigh`) |
| `DRAFT_TRANSLATE_THROTTLE` | `1.0` | Minimum seconds between live-draft translation calls (deltas arrive far more often than that) |

### Tuning for different scenarios

**Fast talker, few pauses:**
```python
WINDOW_MAX_SECONDS = 12.0
SILENCE_COMMIT_SECS = 0.7
MIN_WORDS_FOR_COMMIT = 10
```

**Many short sentences / Q&A:**
```python
SILENCE_COMMIT_SECS = 0.4
MIN_WORDS_FOR_COMMIT = 5
```

**Noisy environment:**
```python
ENERGY_THRESHOLD = 0.015
```

## Browser UI features

- **Language selector** — dropdown in the right panel header; 12 languages or "No Translation"; locked during recording
- **White text** — confirmed (committed) paragraphs, will not change
- **Blue text** — current rolling draft, updates word-by-word as it streams in
- **Purple text** — live translation draft
- **↓ Latest** — jump-to-bottom button, appears when scrolled up; auto-scroll resumes at bottom
- **Clear** — wipe both panels without stopping the session

## Project structure

```
transcriberonthefly/
├── main.py              # FastAPI backend — WebSocket, transcription, translation, interview assistant
├── static/
│   └── index.html       # Browser frontend (AudioWorklet + WebSocket + two-panel UI)
├── electron/
│   ├── main.js          # Electron main process — window, Python process, IPC
│   ├── preload.js       # Context bridge (IPC → renderer)
│   ├── overlay.html     # Transparent subtitle overlay UI
│   └── package.json
├── requirements.txt
├── .env.example
└── README.md
```

## WebSocket message types

| Type | Direction | Payload |
|---|---|---|
| `ready` | server → client | `{ model, llm }` |
| `transcript` | server → client | `{ confirmed_en, current_en, confirmed_ru, current_ru, wid }` |
| `translation` | server → client | `{ current_ru, wid }` |
| `commit` | server → client | `{ confirmed_en, confirmed_ru }` |
| `ru_update` | server → client | `{ confirmed_ru }` |
| `answer` | server → client | `{ text }` — interview answer suggestion |
| `topic` | server → client | `{ topic, definition }` — current subject being discussed (only if `topicHints` was enabled at connect time; fires once per topic, not on every paragraph) |
| `error` | server → client | `{ message }` |
| `ask` | client → server | `{ text }` — manual "tell me about this" request (selected transcript text); answered with a normal `answer` message |

## Known limitations

- **ASR requires OpenAI** — `gpt-realtime-whisper` is only available through OpenAI's Realtime API; Groq cannot be used for transcription
- **Silence detection is energy-based** — cannot distinguish a breath from a sentence boundary; `MIN_WORDS_FOR_COMMIT` compensates for most false positives
- **Live-draft translation is throttled** (`DRAFT_TRANSLATE_THROTTLE`) — the English draft streams word-by-word, but its translation only refreshes about once a second; committed-paragraph translation (`ru_update`) is not throttled
- **Browser mode** — AudioWorklet not available in Safari on older macOS; use Chrome or Firefox
- **Electron overlay** — Python binary path is hardcoded to `/opt/homebrew/opt/python@3.11/bin/python3.11`; edit `electron/main.js` if your path differs
- **Interview assistant** — question relevance is judged entirely by the LLM prompt (no regex pre-filter); may occasionally trigger on rhetorical or non-technical questions
