# Jarvis memory

Jarvis now remembers past conversations and can search them to inform new
answers. This is retrieval-based memory (RAG): everything is saved, and only the
relevant bits are pulled into context each turn — so it scales without blowing up
cost or context.

## How it works
- **Auto-save:** every exchange (your message + Jarvis's reply) is saved to
  `memory_store.json` automatically. You don't do anything.
- **Auto-recall:** on each new message, Jarvis embeds your question, finds the
  most similar past chunks, and injects them into that turn's context. If nothing
  is relevant, nothing is injected.
- **Local embeddings:** uses `sentence-transformers` (model `all-MiniLM-L6-v2`)
  running on your Mac. No extra paid API. First run downloads the model (~90MB)
  once; after that it's offline and fast.

## One-time setup
The library installs with the others:
```bash
pip install -r requirements.txt
```
The first message you send after upgrading will pause a few seconds while the
embedding model downloads. That's normal and only happens once.

## Importing your Claude.ai chats
1. Export your data from Claude.ai (Settings → Account → export, or copy a
   transcript into a text file).
2. Put the files into the `imported_chats/` folder inside `jarvis`
   (it's created automatically; supports `.txt`, `.md`, and `.json`).
3. Click **Import chats** in the Jarvis header (or POST to `/memory/import`).
   Jarvis chunks and embeds them into the same memory, so it can recall them
   alongside its own past chats.

Note: there's no automatic sync from Claude.ai — you drop in the transcripts you
want it to know. Re-running import is safe; duplicates are skipped.

## Privacy
- `memory_store.json` and `imported_chats/` hold your conversation data. Both are
  gitignored so they never get committed.
- Everything stays local on your Mac. Embeddings are computed locally; only the
  small recalled snippets are sent to the model as context on a given turn (the
  same way your live messages are).
- To wipe memory: delete `memory_store.json`. To forget imports: clear
  `imported_chats/` and delete the store (it rebuilds from future chats).

## Checking what it knows
- Header shows nothing by default; hit `/memory/stats` (or just ask Jarvis to
  fetch it) to see how many chunks are stored and from which sources.
