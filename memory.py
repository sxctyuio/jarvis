"""
memory.py — persistent, searchable memory for Jarvis.

Design (matches what you asked for):
  - Auto-save EVERY exchange (your message + Jarvis's reply) to disk.
  - On each new message, embed it, find the most relevant past chunks, and
    hand them back to be injected into context — so Jarvis "remembers" without
    stuffing the whole history in.
  - Also ingest Claude.ai transcripts you drop into ./imported_chats/ (txt, md,
    or json export). Same retrieval treatment.

Storage is a single JSON file (chunks + their vectors). Embeddings are computed
LOCALLY with sentence-transformers — no extra paid API, runs on your Mac. First
run downloads a small (~90MB) model once.

Scale note: JSON + in-memory cosine is fine for tens of thousands of chunks
(personal use). If it ever gets huge, swap in a real vector DB — the interface
here (add / search) stays the same.
"""

import os
import json
import time
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "memory_store.json")
IMPORT_DIR = os.path.join(HERE, "imported_chats")

_model = None          # lazy-loaded embedding model
_data = None           # {"chunks": [ {id, text, source, ts, vec:[...]}, ... ]}


# ----------------------------------------------------------------- model -----
def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        # small, fast, good enough for semantic recall
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def _embed(text):
    return _get_model().encode(text, normalize_embeddings=True).tolist()


# ----------------------------------------------------------------- store -----
def _load():
    global _data
    if _data is None:
        if os.path.exists(STORE):
            with open(STORE) as f:
                _data = json.load(f)
        else:
            _data = {"chunks": []}
    return _data


def _save():
    with open(STORE, "w") as f:
        json.dump(_data, f)


def _chunk_id(text, source):
    return hashlib.sha1((source + "|" + text[:200]).encode()).hexdigest()[:16]


def _add_chunk(text, source):
    """Add one chunk if not already present (dedup by id)."""
    text = text.strip()
    if len(text) < 20:          # skip trivially short fragments
        return False
    data = _load()
    cid = _chunk_id(text, source)
    if any(c["id"] == cid for c in data["chunks"]):
        return False
    data["chunks"].append({
        "id": cid, "text": text, "source": source,
        "ts": time.time(), "vec": _embed(text),
    })
    _enforce_cap(data)
    return True


# Keep memory bounded so recall stays fast and the file stays small. When over
# the cap, drop the OLDEST chunks (by timestamp) to make room — a rolling window.
# This never slices the file mid-chunk (which would corrupt it); it removes whole
# chunks. Recent memory is kept; ancient memory ages out.
MAX_CHUNKS = 4000   # ~a few MB; tune here. 4000 keeps recall fast (<~0.1s).


def _enforce_cap(data):
    chunks = data["chunks"]
    if len(chunks) > MAX_CHUNKS:
        chunks.sort(key=lambda c: c.get("ts", 0))     # oldest first
        del chunks[0:len(chunks) - MAX_CHUNKS]         # drop the oldest overflow


# ------------------------------------------------------------- public API ----
def remember_exchange(user_msg, assistant_text):
    """Save one Jarvis turn. Called after every response."""
    try:
        combined = f"[You]: {user_msg}\n[Jarvis]: {assistant_text}"
        if _add_chunk(combined, source="jarvis_chat"):
            _save()
    except Exception as e:
        print(f"[memory] save skipped: {e}")


# Cache the stacked vector matrix so we don't rebuild it every message. Keyed on
# chunk count — if it changes (added/trimmed), we rebuild. This plus numpy makes
# recall ~fast regardless of store size.
_matrix_cache = {"n": -1, "mat": None}


def recall(query, k=4):
    """Return up to k relevant past chunks as a formatted string, or ''."""
    try:
        import numpy as np
        data = _load()
        chunks = data["chunks"]
        if not chunks:
            return ""
        # (re)build the cached matrix only when the store size changed
        if _matrix_cache["n"] != len(chunks) or _matrix_cache["mat"] is None:
            _matrix_cache["mat"] = np.array([c["vec"] for c in chunks], dtype=np.float32)
            _matrix_cache["n"] = len(chunks)
        mat = _matrix_cache["mat"]
        qv = np.array(_embed(query), dtype=np.float32)
        sims = mat @ qv                      # one vectorized dot-product over all chunks
        idx = np.argsort(-sims)[:k]          # top-k indices
        lines = []
        for i in idx:
            if sims[i] <= 0.25:              # relevance floor
                continue
            c = chunks[int(i)]
            tag = "past chat" if c["source"] == "jarvis_chat" else c["source"]
            lines.append(f"({tag}) {c['text'][:600]}")
        return "\n\n".join(lines)
    except Exception as e:
        print(f"[memory] recall skipped: {e}")
        return ""


def ingest_imported(chunk_chars=1200):
    """Read everything in ./imported_chats/ and add it to memory.
    Supports .txt, .md, and .json (Claude export). Returns a summary string."""
    if not os.path.isdir(IMPORT_DIR):
        os.makedirs(IMPORT_DIR, exist_ok=True)
        return f"Created {IMPORT_DIR}. Drop .txt/.md/.json transcripts there, then run import again."
    added, files = 0, 0
    for name in os.listdir(IMPORT_DIR):
        path = os.path.join(IMPORT_DIR, name)
        if not os.path.isfile(path):
            continue
        try:
            raw = _read_transcript(path)
        except Exception as e:
            print(f"[memory] couldn't read {name}: {e}")
            continue
        files += 1
        # split long transcripts into overlapping-ish chunks by paragraph
        for chunk in _split_text(raw, chunk_chars):
            if _add_chunk(chunk, source=f"import:{name}"):
                added += 1
    _save()
    return f"Imported {files} file(s), added {added} new memory chunks."


def _read_transcript(path):
    if path.endswith(".json"):
        with open(path) as f:
            obj = json.load(f)
        # Claude export is a list of conversations with messages; be liberal.
        parts = []

        def walk(o):
            if isinstance(o, dict):
                if "text" in o and isinstance(o["text"], str):
                    parts.append(o["text"])
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(obj)
        return "\n".join(parts)
    with open(path, errors="replace") as f:
        return f.read()


def _split_text(text, size):
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    buf, out = "", []
    for p in paras:
        if len(buf) + len(p) > size:
            if buf:
                out.append(buf)
            buf = p
        else:
            buf = (buf + "\n\n" + p) if buf else p
    if buf:
        out.append(buf)
    return out


def trim_now(keep=MAX_CHUNKS):
    """One-time shrink of an already-oversized store: keep the most recent
    `keep` chunks, drop the rest. Returns a summary."""
    data = _load()
    before = len(data["chunks"])
    data["chunks"].sort(key=lambda c: c.get("ts", 0))   # oldest first
    if before > keep:
        data["chunks"] = data["chunks"][-keep:]         # keep newest
    _save()
    after = len(data["chunks"])
    return f"Trimmed memory: {before} -> {after} chunks."


def stats():
    data = _load()
    n = len(data["chunks"])
    srcs = {}
    for c in data["chunks"]:
        key = c["source"].split(":")[0]
        srcs[key] = srcs.get(key, 0) + 1
    return {"total_chunks": n, "by_source": srcs}
