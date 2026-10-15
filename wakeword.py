"""
wakeword.py — always-on wake word listener that runs in the SERVER, not the
browser.

Why: browser speech recognition stops when the tab is hidden, times out on
silence, and fights audio playback. This listener uses your Mac's microphone
directly (sounddevice) and transcribes locally with Vosk (free, offline), so it
keeps listening whether the Jarvis tab is open, hidden, or closed.

Flow:
  - Continuously transcribe mic audio.
  - If a finished utterance contains the wake word:
      "jarvis what's the weather"  -> command = "what's the weather"
      "jarvis" (alone)             -> arm for ~8s; next utterance = command
  - Commands are handed to a callback (server.py runs them as a normal turn).
  - While Jarvis is speaking, the listener is muted so it doesn't hear itself.

First start downloads a small English speech model (~40MB) once into
./vosk-model. macOS will ask to allow microphone access for your terminal.
"""

import io
import os
import json
import time
import queue
import zipfile
import threading
from difflib import SequenceMatcher

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(HERE, "vosk-model")
MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"

SAMPLE_RATE = 16000
ARM_SECONDS = 8          # after a bare wake word, how long to wait for the command
MATCH_THRESHOLD = 0.75   # fuzzy match: small models often misspell names

_state = {
    "running": False,
    "word": "jarvis",
    "status": "off",
    "heard": "",
    "armed_until": 0.0,
    "mute_until": 0.0,
}
_thread = None
_on_command = None


# ------------------------------------------------------------------ public ---
def start(on_command):
    """Start listening in a background thread (no-op if already running)."""
    global _thread, _on_command
    _on_command = on_command
    if _state["running"]:
        return status()
    _state["running"] = True
    _state["status"] = "starting..."
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    return status()


def stop():
    _state["running"] = False
    _state["armed_until"] = 0.0
    _state["status"] = "off"
    return status()


def set_word(word):
    w = (word or "").strip().lower()
    if w:
        _state["word"] = w[:40]


def mute(seconds):
    """Ignore mic input for `seconds` from now (used while Jarvis speaks)."""
    _state["mute_until"] = time.time() + max(0.0, seconds)


def status():
    s = dict(_state)
    if s["running"] and s["armed_until"] > time.time():
        s["status"] = "listening for command..."
    s.pop("armed_until", None)
    s.pop("mute_until", None)
    return s


# ------------------------------------------------------------- matching ------
def match_wake(text, word):
    """Return (index, span) of the wake word in `text`, or None.
    Fuzzy: compares each token (and joined neighbouring tokens, so "jar vis"
    still matches "jarvis") against the wake word."""
    toks = text.lower().split()
    target = word.lower().replace(" ", "")
    if not toks or not target:
        return None
    max_span = len(word.split()) + 1
    for i in range(len(toks)):
        for span in range(1, max_span + 1):
            if i + span > len(toks):
                break
            cand = "".join(toks[i:i + span])
            if cand == target or SequenceMatcher(None, cand, target).ratio() >= MATCH_THRESHOLD:
                return i, span
    return None


def handle_final(text):
    """Process one finished utterance. Returns the command fired, or None."""
    text = (text or "").strip().lower()
    if not text:
        return None
    _state["heard"] = text
    now = time.time()

    # armed by a bare wake word: this whole utterance is the command
    if _state["armed_until"] > now:
        _state["armed_until"] = 0.0
        return _fire(text)

    m = match_wake(text, _state["word"])
    if not m:
        return None
    i, span = m
    cmd = " ".join(text.split()[i + span:]).strip()
    if len(cmd) > 1:
        return _fire(cmd)
    _state["armed_until"] = now + ARM_SECONDS
    return None


def _fire(cmd):
    _state["status"] = f'got: "{cmd}"'
    cb = _on_command
    if cb:
        # run the turn off the audio thread so listening never stalls
        threading.Thread(target=cb, args=(cmd,), daemon=True).start()
    return cmd


# ------------------------------------------------------------------ audio ----
def _ensure_model():
    if os.path.isdir(MODEL_DIR) and os.listdir(MODEL_DIR):
        return MODEL_DIR
    _state["status"] = "downloading speech model (~40MB, one time)..."
    import requests
    r = requests.get(MODEL_URL, timeout=300)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        top = z.namelist()[0].split("/")[0]
        z.extractall(HERE)
    os.rename(os.path.join(HERE, top), MODEL_DIR)
    return MODEL_DIR


def _loop():
    try:
        import sounddevice as sd
        from vosk import Model, KaldiRecognizer, SetLogLevel
    except ImportError as e:
        _state["status"] = f"missing package ({e.name}) — run: pip install -r requirements.txt"
        _state["running"] = False
        return
    except Exception as e:   # e.g. sounddevice raises OSError if PortAudio is missing
        _state["status"] = f"audio library error: {e} — try: brew install portaudio"
        _state["running"] = False
        return

    try:
        SetLogLevel(-1)
        model = Model(_ensure_model())
    except Exception as e:
        _state["status"] = f"speech model error: {e}"
        _state["running"] = False
        return

    rec = KaldiRecognizer(model, SAMPLE_RATE)
    audio_q = queue.Queue()

    def _callback(indata, frames, t, st):
        audio_q.put(bytes(indata))

    try:
        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=8000,
                               dtype="int16", channels=1, callback=_callback):
            while _state["running"]:
                if _state["armed_until"] <= time.time() and not _state["status"].startswith("got"):
                    _state["status"] = f'listening for "{_state["word"]}"'
                try:
                    data = audio_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if time.time() < _state["mute_until"]:
                    rec.Reset()          # drop Jarvis's own voice
                    continue
                if rec.AcceptWaveform(data):
                    handle_final(json.loads(rec.Result()).get("text", ""))
                else:
                    partial = json.loads(rec.PartialResult()).get("partial", "")
                    if partial:
                        _state["heard"] = partial
                        if _state["status"].startswith("got"):
                            _state["status"] = f'listening for "{_state["word"]}"'
    except Exception as e:
        _state["status"] = (f"mic error: {e} — allow microphone access for your "
                            "terminal in System Settings → Privacy & Security → Microphone")
    finally:
        _state["running"] = False
