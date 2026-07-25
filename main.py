import asyncio
import base64
import itertools
import json
import os
from typing import Optional

import numpy as np
import scipy.signal
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import openai

load_dotenv()

app = FastAPI(title="TranscriberOnTheFly")
app.mount("/static", StaticFiles(directory="static"), name="static")

REALTIME_RATE = 24000              # gpt-realtime-whisper only supports 24kHz PCM
REALTIME_MODEL = "gpt-realtime-whisper"
REALTIME_DELAY = "low"             # latency/accuracy tradeoff for streamed deltas
WINDOW_MAX_SECONDS = 8.0           # force-commit safety net if no silence detected
ENERGY_THRESHOLD = 0.006           # RMS below this = silence
SILENCE_COMMIT_SECS = 0.55         # commit after this much continuous silence
MIN_SPEECH_FOR_COMMIT = 0.9        # need at least this much speech before silence-commit
MIN_WORDS_FOR_COMMIT = 7           # merge short fragments instead of starting a new paragraph
MAX_PENDING_MERGES = 3             # force-commit after this many short-fragment merges
PENDING_GRACE_SECS = 1.5           # commit a short pending segment anyway if nothing more arrives
DRAFT_TRANSLATE_THROTTLE = 1.0     # min seconds between live-draft translation calls

ANSWER_PROMPT = (
    "You are a knowledgeable friend listening to a conversation.\n"
    "If the text mentions any technical or substantive topic (Linux, Kubernetes, Docker, "
    "networking, programming, databases, cloud, DevOps, system design, security, history, "
    "science, business, or any other specific domain), drop in a quick helpful tip or fact "
    "(2-3 sentences max).\n"
    "Write like you're texting a friend — casual, plain words, zero jargon. "
    "No fancy terms unless you immediately explain them in simple words. "
    "No corporate tone, no 'it is worth noting', no 'leveraging', no 'robust'.\n"
    "If the text is small talk, filler, or greetings with nothing to add: "
    "respond with exactly one dash: —\n"
    "Output only the tip, no preamble, no quotes."
)

TOPIC_PROMPT = (
    "You are watching a live conversation transcript to spot what specific subject "
    "is being discussed right now.\n"
    "Identify the single specific subject, technology, tool, or concept the text is "
    "about (e.g. 'Ansible', 'Kubernetes', 'quantum computing').\n"
    "If there is no clear specific subject — small talk, filler, greetings, or vague "
    "chat with nothing identifiable — respond with exactly one dash: —\n"
    "Otherwise respond on a single line in exactly this format, nothing else:\n"
    "<Topic Name> || <one-sentence plain-English definition>"
)


HALLUCINATIONS = frozenset([
    "thank you for watching", "thanks for watching", "thank you",
    "thanks for watching!", "you", "bye", "okay", "ok", "hmm", "um", "uh",
    "the", ".", "!", "?", "...", "i", "a", "thanks", "please subscribe",
    "subscribe", "like and subscribe",
])


def get_asr_client() -> openai.AsyncOpenAI:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "Set OPENAI_API_KEY in .env (required for gpt-realtime-whisper transcription)"
        )
    return openai.AsyncOpenAI(api_key=key)


def get_llm_client() -> tuple[openai.AsyncOpenAI, str]:
    groq_key = os.getenv("GROQ_API_KEY")
    if groq_key:
        client = openai.AsyncOpenAI(
            api_key=groq_key, base_url="https://api.groq.com/openai/v1"
        )
        return client, "llama-3.1-8b-instant"
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        return openai.AsyncOpenAI(api_key=openai_key), "gpt-4o-mini"
    raise RuntimeError("Set GROQ_API_KEY or OPENAI_API_KEY in .env")


def resample(audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    if from_rate == to_rate:
        return audio
    g = int(np.gcd(from_rate, to_rate))
    return scipy.signal.resample_poly(audio, to_rate // g, from_rate // g)


def pcm16_base64(audio_float: np.ndarray, src_rate: int) -> Optional[str]:
    if len(audio_float) == 0:
        return None
    resampled = resample(audio_float, src_rate, REALTIME_RATE)
    int16 = (np.clip(resampled, -1.0, 1.0) * 32767).astype(np.int16)
    return base64.b64encode(int16.tobytes()).decode("ascii")


def clean_text(text: str) -> str:
    import re
    return re.sub(r'^\s+|\s+$', '', text, flags=re.UNICODE)

def capitalize_first(text: str) -> str:
    return text[0].upper() + text[1:] if text else text

def is_hallucination(text: str) -> bool:
    cleaned = text.lower().strip('.,!? \n\t-–—"\'')
    return cleaned in HALLUCINATIONS or len(cleaned) < 3

def merge_fragments(a: str, b: str) -> str:
    """Join a short unfinished fragment with the next transcribed chunk."""
    a, b = a.strip(), b.strip()
    if not a:
        return b
    if not b:
        return a
    return f"{a} {b}"


async def translate_text(
    client: openai.AsyncOpenAI, model: str, text: str, lang: str = ""
) -> Optional[str]:
    if not lang or not text.strip():
        return None
    prompt = (
        f"Translate the following English text to {lang}. "
        "Output only the translation, no explanations, no quotes."
    )
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
            max_tokens=1024,
            temperature=0.1,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        print(f"[translate] {exc}")
        return None


async def answer_question(
    client: openai.AsyncOpenAI, model: str, text: str
) -> Optional[str]:
    if not text.strip():
        return None
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": ANSWER_PROMPT},
                {"role": "user",   "content": text},
            ],
            max_tokens=350,
            temperature=0.4,
        )
        result = resp.choices[0].message.content.strip()
        return None if result in ("—", "-", "") else result
    except Exception as exc:
        print(f"[answer] {exc}")
        return None


async def detect_topic(
    client: openai.AsyncOpenAI, model: str, text: str
) -> Optional[tuple[str, str]]:
    if not text.strip():
        return None
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": TOPIC_PROMPT},
                {"role": "user",   "content": text},
            ],
            max_tokens=100,
            temperature=0.2,
        )
        result = resp.choices[0].message.content.strip()
        if result in ("—", "-", "") or "||" not in result:
            return None
        topic, definition = result.split("||", 1)
        topic, definition = topic.strip(), definition.strip()
        if not topic or not definition:
            return None
        return topic, definition
    except Exception as exc:
        print(f"[topic] {exc}")
        return None


@app.get("/")
async def index():
    with open("static/index.html") as f:
        return HTMLResponse(f.read())


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()

    try:
        meta = await ws.receive_json()
        browser_rate = int(meta.get("sampleRate", 48000))
        target_lang = str(meta.get("targetLang", "")).strip()
        topic_hints_enabled = bool(meta.get("topicHints", False))
        auto_hints_enabled = bool(meta.get("autoHints", False))
    except Exception:
        await ws.close()
        return

    try:
        asr_client = get_asr_client()
        llm_client, llm_model = get_llm_client()
    except RuntimeError as exc:
        await ws.send_json({"type": "error", "message": str(exc)})
        await ws.close()
        return

    print(f"[session] rate={browser_rate}  asr={REALTIME_MODEL}  llm={llm_model}  lang={target_lang}")
    await ws.send_json({"type": "ready", "model": REALTIME_MODEL, "llm": llm_model})

    # ── State ─────────────────────────────────────────────────────────────
    confirmed_paras_en: list[str] = []   # one entry per committed segment
    confirmed_paras_ru: list[str] = []
    current_en = ""                      # text shown as the live draft (pending + streaming)
    current_ru = ""
    pending_en = ""                      # merged short fragments not yet promoted to a paragraph
    pending_merges = 0
    live_en = ""                         # deltas accumulated since the last completed event
    text_lock = asyncio.Lock()
    counter = itertools.count(1)
    latest_wid = 0
    last_translate_ts = 0.0
    last_topic = ""                      # normalized (lowercase) name of the last announced topic
    pending_commit_task: Optional[asyncio.Task] = None  # debounce timer for short pending segments

    def _join_paras(paras: list[str]) -> str:
        """Join paragraphs: \n\n if previous ends with sentence punctuation, else space."""
        if not paras:
            return ""
        result = paras[0]
        for i in range(1, len(paras)):
            prev_tail = paras[i - 1].rstrip()
            sep = "\n\n" if prev_tail and prev_tail[-1] in ".?!" else " "
            result += sep + paras[i]
        return result

    def full_confirmed_en() -> str:
        return _join_paras(confirmed_paras_en)

    def full_confirmed_ru() -> str:
        return _join_paras(confirmed_paras_ru)

    async def safe_send(payload: dict):
        try:
            await ws.send_json(payload)
        except Exception:
            pass

    def _cancel_pending_timer():
        nonlocal pending_commit_task
        if pending_commit_task and pending_commit_task is not asyncio.current_task():
            pending_commit_task.cancel()
        pending_commit_task = None

    # ── Commit a transcribed segment ──────────────────────────────────────
    async def do_commit(segment_en: str):
        nonlocal current_en, current_ru, pending_en, pending_merges, live_en

        _cancel_pending_timer()

        async with text_lock:
            if segment_en:
                confirmed_paras_en.append(capitalize_first(clean_text(segment_en)))
            current_en = ""
            current_ru = ""
            pending_en = ""
            pending_merges = 0
            live_en = ""

        snap_en = full_confirmed_en()
        snap_ru = full_confirmed_ru()
        print(f"[commit] paras={len(confirmed_paras_en)}  last={segment_en!r:.60}")

        await safe_send({
            "type": "commit",
            "confirmed_en": snap_en,
            "confirmed_ru": snap_ru,
        })

        async def translate_committed(seg=segment_en):
            if not seg:
                return
            ru = await translate_text(llm_client, llm_model, seg, target_lang)
            if ru:
                async with text_lock:
                    confirmed_paras_ru.append(ru.strip())
                await safe_send({
                    "type": "ru_update",
                    "confirmed_ru": full_confirmed_ru(),
                })

        asyncio.create_task(translate_committed())

        async def maybe_answer(seg=segment_en):
            if not auto_hints_enabled or not seg:
                return
            ans = await answer_question(llm_client, llm_model, seg)
            if ans:
                print(f"[answer] {ans[:80]!r}")
                await safe_send({"type": "answer", "text": ans})

        asyncio.create_task(maybe_answer())

        async def maybe_topic(seg=segment_en):
            nonlocal last_topic
            if not topic_hints_enabled or not seg:
                return
            result = await detect_topic(llm_client, llm_model, seg)
            if not result:
                return  # no clear topic in this segment — leave last_topic untouched
            topic, definition = result
            if topic.lower() == last_topic:
                return  # same topic as last time — don't repeat
            last_topic = topic.lower()
            print(f"[topic] {topic}: {definition[:80]!r}")
            await safe_send({"type": "topic", "topic": topic, "definition": definition})

        asyncio.create_task(maybe_topic())

    # ── Give up waiting for more speech and commit a short pending segment ──
    async def _delayed_pending_commit():
        await asyncio.sleep(PENDING_GRACE_SECS)
        async with text_lock:
            text = pending_en
        if text:
            print(f"[commit] grace-period timeout, committing pending: {text!r:.60}")
            await do_commit(text)

    # ── Manual "ask about selection" request from the client ────────────────
    async def handle_manual_ask(text: str):
        ans = await answer_question(llm_client, llm_model, f"Tell me about {text}")
        if ans:
            print(f"[ask] {text[:40]!r} -> {ans[:80]!r}")
            await safe_send({"type": "answer", "text": ans})

    async with asr_client.realtime.connect(extra_query={"intent": "transcription"}) as conn:
        await conn.session.update(session={
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": REALTIME_RATE},
                    "turn_detection": None,
                    "transcription": {
                        "model": REALTIME_MODEL,
                        "language": "en",
                        "delay": REALTIME_DELAY,
                    },
                }
            },
        })

        # ── Stream browser audio into the realtime session ──────────────────
        async def asr_send_loop():
            silent_samples = 0
            speech_samples = 0
            segment_samples = 0
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect(message.get("code", 1000), message.get("reason"))

                if message.get("text") is not None:
                    try:
                        control = json.loads(message["text"])
                    except Exception:
                        continue
                    if control.get("type") == "ask":
                        text = str(control.get("text", "")).strip()
                        if text:
                            asyncio.create_task(handle_manual_ask(text))
                    continue

                data = message.get("bytes")
                if not data:
                    continue
                chunk = np.frombuffer(data, dtype=np.float32)
                if len(chunk) == 0:
                    continue

                b64 = pcm16_base64(chunk, browser_rate)
                if b64:
                    await conn.input_audio_buffer.append(audio=b64)

                rms = float(np.sqrt(np.mean(chunk ** 2)))
                segment_samples += len(chunk)
                if rms < ENERGY_THRESHOLD:
                    silent_samples += len(chunk)
                else:
                    silent_samples = 0
                    speech_samples += len(chunk)

                silence_secs = silent_samples / browser_rate
                speech_secs = speech_samples / browser_rate
                segment_secs = segment_samples / browser_rate

                if speech_secs > 0 and (
                    (silence_secs >= SILENCE_COMMIT_SECS and speech_secs >= MIN_SPEECH_FOR_COMMIT)
                    or segment_secs >= WINDOW_MAX_SECONDS
                ):
                    await conn.input_audio_buffer.commit()
                    silent_samples = speech_samples = segment_samples = 0
                elif speech_secs == 0 and segment_secs >= WINDOW_MAX_SECONDS:
                    # long stretch of pure silence — drop it instead of committing empty audio
                    await conn.input_audio_buffer.clear()
                    silent_samples = segment_samples = 0

        # ── Handle transcript events from the realtime session ──────────────
        async def asr_recv_loop():
            nonlocal current_en, current_ru, pending_en, pending_merges, live_en, latest_wid, last_translate_ts, pending_commit_task

            async for event in conn:
                if event.type == "conversation.item.input_audio_transcription.delta":
                    delta = event.delta or ""
                    if not delta:
                        continue
                    async with text_lock:
                        live_en += delta
                        current_en = merge_fragments(pending_en, live_en)
                        wid = latest_wid
                        snap_en = full_confirmed_en()
                        snap_ru = full_confirmed_ru()
                        draft_ru = current_ru

                    await safe_send({
                        "type": "transcript",
                        "confirmed_en": snap_en,
                        "current_en": current_en,
                        "confirmed_ru": snap_ru,
                        "current_ru": draft_ru,
                        "wid": wid,
                    })

                    if target_lang:
                        now = asyncio.get_event_loop().time()
                        if now - last_translate_ts >= DRAFT_TRANSLATE_THROTTLE:
                            last_translate_ts = now
                            draft_snapshot = current_en
                            ru = await translate_text(llm_client, llm_model, draft_snapshot, target_lang)
                            if ru:
                                async with text_lock:
                                    if wid < latest_wid:
                                        continue
                                    current_ru = ru
                                await safe_send({"type": "translation", "current_ru": ru, "wid": wid})

                elif event.type == "conversation.item.input_audio_transcription.completed":
                    text = clean_text(event.transcript or "")
                    async with text_lock:
                        live_en = ""
                        latest_wid = next(counter)
                        if not text or is_hallucination(text):
                            print(f"[completed] filtered: {text!r}")
                            merged = pending_en
                        else:
                            merged = merge_fragments(pending_en, text)

                    if not merged:
                        continue

                    word_count = len(merged.split())
                    pending_merges += 1

                    if word_count >= MIN_WORDS_FOR_COMMIT or pending_merges >= MAX_PENDING_MERGES:
                        print(f"[commit] words={word_count} {merged!r:.60}")
                        await do_commit(merged)
                    else:
                        print(f"[skip-commit] {word_count} words: {merged!r}")
                        async with text_lock:
                            pending_en = merged
                            current_en = merged
                            snap_en = full_confirmed_en()
                            snap_ru = full_confirmed_ru()
                        await safe_send({
                            "type": "transcript",
                            "confirmed_en": snap_en,
                            "current_en": merged,
                            "confirmed_ru": snap_ru,
                            "current_ru": "",
                            "wid": latest_wid,
                        })
                        if pending_commit_task:
                            pending_commit_task.cancel()
                        pending_commit_task = asyncio.create_task(_delayed_pending_commit())

                elif event.type == "error":
                    print(f"[realtime] error: {event.error.message}")
                    await safe_send({"type": "error", "message": event.error.message})

        send_task = asyncio.create_task(asr_send_loop())
        recv_task = asyncio.create_task(asr_recv_loop())
        try:
            await asyncio.gather(send_task, recv_task)
        except WebSocketDisconnect:
            print("[session] disconnected")
        except Exception as exc:
            print(f"[session] error: {exc}")
        finally:
            send_task.cancel()
            recv_task.cancel()
            if pending_commit_task:
                pending_commit_task.cancel()
            await asyncio.gather(send_task, recv_task, return_exceptions=True)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
