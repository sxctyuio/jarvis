# Jarvis — local multi-tool agent

A browser chat where Claude can act on your Mac, your Open Duck Mini robot, and
the web — picking the right tool per task, and asking approval before anything
that changes state.

## Tools

| Tool         | What it does                                  | Auto or gated |
|--------------|-----------------------------------------------|---------------|
| `run_shell`  | Command on your Mac                            | safe reads auto; everything else gated |
| `run_on_duck`| Command on the duck via SSH (adhav@duckpi.local) | safe reads auto; everything else gated |
| `browser`    | Real Chrome via Playwright (navigate/read/click/type/screenshot/images) | navigate/read/screenshot/images auto; click/type gated |

"Safe reads" = `ls`, `cat`, `df`, `systemctl status`, `i2cdetect`, `vcgencmd`,
`git status`, etc. Anything with pipes, redirects, `sudo`, installs, `rm`, `kill`,
or writing is held for your approval. Adjust the `SAFE_PREFIXES` set in server.py
to taste.

## Setup (macOS)

```bash
cd jarvis
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # one-time: downloads the browser

export ANTHROPIC_API_KEY=sk-ant-...
export DUCK_PASSWORD=your-duck-password    # never hardcoded in the source

python server.py
```

Open http://localhost:5001.

Try:
- "how much disk is my home folder using?"  (Mac)
- "the left hip servo is throwing bus errors on the duck, check the service and i2c"  (duck)
- "open the One Piece wiki and show me pictures of Luffy"  (browser)

## Browser profile toggle

Top-right switch:
- **off (default):** a clean, separate Chrome. No access to your logins. Safer.
- **on:** a persistent profile at `~/.jarvis-chrome-profile`. The first time you
  turn it on, a Chrome window opens where you can log into the sites you want;
  those logins persist for later sessions. Turning the switch relaunches the
  browser with the chosen profile.

## Safety — read before pointing it at anything real

- **The approval gate is the safety.** State-changing actions wait for your click.
  Keep it. Widening `SAFE_PREFIXES` trades safety for convenience — do it knowingly.
- **Secrets stay server-side.** API key and duck password live in env vars on the
  machine running server.py, never in the browser or the source.
- **Output is untrusted.** A file the shell reads, or a web page the browser reads,
  can contain "ignore instructions and do X" — a real prompt-injection surface.
  This matters most with the account-profile browser, where a malicious page could
  try to make Claude act as logged-in you. Read what it proposes before approving,
  and keep the account profile off unless you need it.
- **localhost only, no auth.** Don't bind to 0.0.0.0 or port-forward it. Anyone who
  reached the port could drive your Mac, your robot, and your logged-in browser.
- **The duck SSH auto-accepts the host key** (AutoAddPolicy) for convenience on a
  home network. Fine for duckpi.local on your LAN; don't reuse this pattern for
  hosts you don't control.
