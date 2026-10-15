"""
Local multi-tool agent web app ("Jarvis").

The browser page is just the UI. THIS process runs on your machine, holds the
API key + duck password, and is the only thing that can touch anything. Every
state-changing action is held until you click Approve in the browser.

Tools Claude can use:
  - run_shell     : run a command on THIS machine (your Mac)
  - run_on_duck   : run a command on the Open Duck Mini (SSH to adhav@duckpi.local)
  - browser       : drive a Playwright-controlled Chrome (navigate, click, type,
                    read text, list/click links, screenshot). Separate profile by
                    default; a UI toggle can switch to your logged-in profile.

Setup:
    pip install flask anthropic playwright paramiko
    playwright install chromium
    export ANTHROPIC_API_KEY=sk-ant-...
    export DUCK_PASSWORD=...            # your duck's SSH password (never hardcoded)
    python server.py
    # open http://localhost:5001

Safety model:
  - Read-only actions (safe shell/duck reads, browser navigate/read/screenshot)
    run without a prompt. Anything that changes state (writes, installs, sudo,
    kill, browser clicks/typing) is GATED — parked until you Approve in the UI.
  - API key and duck password stay server-side; never sent to the browser.
  - Command/page output is UNTRUSTED input (prompt-injection surface). Read what
    Claude proposes before approving.
"""

import os
import re
import shlex
import subprocess
import threading
import anthropic
from flask import Flask, request, jsonify, send_from_directory


# ---- .env loader: set your keys once in a file, not every terminal ----------
# Reads KEY=value lines from a .env file next to this script and puts them in
# the environment (without overwriting anything already set in the shell). No
# external dependency. Lines starting with # are comments; quotes are stripped.
def _load_dotenv(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:   # shell exports win over the file
                os.environ[key] = val


# Load order (first one to set a key wins): shell exports, then a .env in this
# folder, then ~/.config/jarvis/.env. Keep your real keys in the ~/.config one —
# it lives outside the project, so re-downloading or re-cloning never wipes it.
_load_dotenv()
_load_dotenv(os.path.expanduser("~/.config/jarvis/.env"))

# ---------------------------------------------------------------- config -----
MODEL = "claude-sonnet-5"          # default; switchable from the UI
ALLOWED_MODELS = {
    "claude-sonnet-5": "Sonnet (fast, cheap)",
    "claude-opus-4-8": "Opus 4.8 (most capable)",
}
current_model = [MODEL]            # mutable holder so routes can change it
assistant_name = ["Jarvis"]        # changeable from the UI
MAX_OUTPUT_CHARS = 12_000
COMMAND_TIMEOUT = 60

DUCK_HOST = "duckpi.local"
DUCK_USER = "adhav"
DUCK_PASSWORD = os.environ.get("DUCK_PASSWORD", "")

# Commands we consider read-only -> auto-run without approval. Everything else
# is gated. Deliberately conservative: prefix match on the first token.
SAFE_PREFIXES = {
    "ls", "pwd", "cat", "head", "tail", "grep", "find", "df", "du", "ps",
    "whoami", "uname", "uptime", "date", "echo", "which", "wc", "stat",
    "top", "free", "systemctl status", "journalctl", "git status", "git log",
    "git diff", "ifconfig", "ip addr", "hostname", "vcgencmd", "i2cdetect",
}

# Browser actions that only read/observe -> auto. Others gated.
SAFE_BROWSER_ACTIONS = {"navigate", "read_text", "list_links", "screenshot", "get_images"}

app = Flask(__name__, static_folder="static")
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

import integrations as ig  # external-service tools + OAuth
import memory               # persistent, searchable conversation memory
import wakeword             # server-side always-on wake word (Vosk)
import shutil
import tempfile

messages = []

SYSTEM_PROMPT = f"""You are a local assistant for Arun. Tools:

Local / robot / web:
- run_shell — a command on his Mac.
- run_on_duck — a command on his Open Duck Mini v2 robot via SSH
  ({DUCK_USER}@{DUCK_HOST}). Robot debugging: servo bus, IMU, systemd, logs,
  i2cdetect, vcgencmd.
- browser — a real Chrome (navigate/read_text/list_links/get_images/screenshot/
  click/type). Use for pages you must SEE or interact with behind a login.

Prefer APIs over the browser for anything with one:
- web_search — search the web (built in). Use for looking things up.
- fetch_page — get a web page's TEXT by URL. Faster than the browser for reading.
- gmail — search/read/send email. Use this, NOT the browser, for email.
- calendar — list/create events.
- github — whoami/list_repos/list_issues/list_prs/check_runs/read_file/
  create_issue. If Arun refers to a repo by a fuzzy name, use list_repos to
  find the real owner/name instead of guessing.
- news — headlines or a topic search.
- weather — current conditions.
- spotify — current/search/play/pause.

Persona: You are __NAME__ — Arun's assistant, but talk like a real person, not a
corporate AI. Natural, warm, a little dry wit when it fits. Use contractions,
react like a human ("nice", "ah, that's the problem", "yeah, that'll do it"),
and speak the way a sharp friend who happens to be great with computers would.
No robotic hedging, no "As an AI", no bullet-point-everything reflex, no
customer-service tone.

Still: talk LESS. Human doesn't mean chatty. Default to short, real replies —
a sentence or two, or just the result with a natural line around it. Don't pad,
don't restate the question, don't end every turn with a follow-up question. Save
the depth for when Arun actually asks you to explain something — then be as
thorough as a good teacher.

You know Arun and his projects (the Open Duck Mini robot, Druid, his ML work) —
reference that context naturally, like someone who's been working alongside him,
not like you're reading a file.

Rules:
- Pick the cheapest tool that works (API > browser > shell hacks).
- Say what you're doing in a few natural words, then do it — no narration.
- ONE shell command (run_shell) per message, maximum. Run the single most useful
  command, look at the result, and if more is needed, say the next step and take
  it next turn (or combine into that one command). Limit is for run_shell only.
- Shell permission: when Arun EXPLICITLY asks you to run a shell command ("run
  ls", "check disk with df", "run this: ..."), just do it — that request IS the
  permission. When a task doesn't obviously call for the shell but you'd need it
  anyway, say in text that you'd use a shell command and why, and ask first.
  (Destructive commands — rm, overwriting, sudo, kill — still surface the
  approval gate regardless.)
- Other state-changing actions (send email, create event, create issue,
  play/pause music, browser click/type) wait for Arun's approval.
- If a Google/Spotify tool says it's not connected, tell Arun to connect it from
  the header buttons."""

tools = [
    {
        "name": "run_shell",
        "description": "Run a shell command on the user's Mac. Returns combined stdout/stderr.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "run_on_duck",
        "description": (
            "Run a shell command on the Open Duck Mini v2 robot over SSH "
            f"({DUCK_USER}@{DUCK_HOST}). Use for robot/hardware debugging."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "browser",
        "description": (
            "Control a real Chrome browser. One action per call. "
            "actions: navigate (needs url), read_text, list_links, get_images, "
            "screenshot, click (needs target: a CSS selector or visible text), "
            "type (needs target selector and text)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["navigate", "read_text", "list_links", "get_images",
                             "screenshot", "click", "type"],
                },
                "url": {"type": "string"},
                "target": {"type": "string", "description": "CSS selector or visible text"},
                "text": {"type": "string", "description": "text to type"},
            },
            "required": ["action"],
        },
    },
    # ---- built-in web search (native Anthropic tool type) ----
    {"type": "web_search_20250305", "name": "web_search"},
    # ---- external-service tools ----
    {
        "name": "fetch_page",
        "description": "Fetch a web page and return its readable text. Read-only.",
        "input_schema": {"type": "object",
                         "properties": {"url": {"type": "string"}},
                         "required": ["url"]},
    },
    {
        "name": "gmail",
        "description": ("Gmail. action=search|read|send. search: optional query "
                        "(Gmail syntax). read: query=message id. send: to, subject, body."),
        "input_schema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["search", "read", "send"]},
            "query": {"type": "string"}, "to": {"type": "string"},
            "subject": {"type": "string"}, "body": {"type": "string"}},
            "required": ["action"]},
    },
    {
        "name": "calendar",
        "description": "Calendar. action=list (next N days) | create (summary, start, end as RFC3339).",
        "input_schema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "create"]},
            "summary": {"type": "string"}, "start": {"type": "string"},
            "end": {"type": "string"}, "days": {"type": "integer"}},
            "required": ["action"]},
    },
    {
        "name": "github",
        "description": ("GitHub. action=whoami|list_repos|list_issues|list_prs|"
                        "check_runs|read_file|create_issue. Use whoami to get the "
                        "user's login, list_repos to see ALL their repos (use this "
                        "when they don't remember an exact repo name). repo='owner/name'. "
                        "read_file: title=path. create_issue: title, body."),
        "input_schema": {"type": "object", "properties": {
            "action": {"type": "string"}, "repo": {"type": "string"},
            "number": {"type": "integer"}, "title": {"type": "string"},
            "body": {"type": "string"}}, "required": ["action"]},
    },
    {
        "name": "news",
        "description": "News headlines. Optional query for a topic search.",
        "input_schema": {"type": "object",
                         "properties": {"query": {"type": "string"}}},
    },
    {
        "name": "weather",
        "description": "Current weather. Give place (name) or lat+lon.",
        "input_schema": {"type": "object", "properties": {
            "place": {"type": "string"}, "lat": {"type": "number"},
            "lon": {"type": "number"}}},
    },
    {
        "name": "spotify",
        "description": "Spotify. action=current|search|play|pause. search/play take query.",
        "input_schema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["current", "search", "play", "pause"]},
            "query": {"type": "string"}}, "required": ["action"]},
    },
]

# ---------------------------------------------------- approval machinery -----
_pending = {}
_lock = threading.Lock()


def _truncate(s: str) -> str:
    s = s.strip()
    return s if len(s) <= MAX_OUTPUT_CHARS else s[:MAX_OUTPUT_CHARS] + "\n...(truncated)"


def _is_safe_command(command: str) -> bool:
    c = command.strip()
    if any(tok in c for tok in ("&&", "||", ";", "|", ">", "<", "`", "$(")):
        return False  # compound/redirecting commands always get a look
    return any(c == p or c.startswith(p + " ") for p in SAFE_PREFIXES)


def _wait_for_approval(action_id: str, label: str, detail: str) -> bool:
    """Park an action, block until the browser approves/denies. Unanswered
    approvals auto-DENY after APPROVAL_TIMEOUT so a voice command can never
    freeze Jarvis when no browser is open to click Approve."""
    event = threading.Event()
    with _lock:
        _pending[action_id] = {"label": label, "detail": detail,
                               "event": event, "decision": None}
    event.wait(timeout=APPROVAL_TIMEOUT)
    with _lock:
        entry = _pending.pop(action_id)
    return entry["decision"] == "approve"   # None (timed out) -> denied


APPROVAL_TIMEOUT = 180   # seconds


# ------------------------------------------------------------- executors -----
def _run_local(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True,
                           timeout=COMMAND_TIMEOUT)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return _truncate(out or f"(no output, exit {r.returncode})")
    except subprocess.TimeoutExpired:
        return f"(timed out after {COMMAND_TIMEOUT}s)"
    except Exception as e:
        return f"(error: {e})"


_ssh_client = None


def _run_duck(command: str) -> str:
    global _ssh_client
    if not DUCK_PASSWORD:
        return "(DUCK_PASSWORD not set on the server; can't SSH to the duck)"
    try:
        import paramiko
    except ImportError:
        return "(paramiko not installed: pip install paramiko)"
    try:
        if _ssh_client is None:
            _ssh_client = paramiko.SSHClient()
            _ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            _ssh_client.connect(DUCK_HOST, username=DUCK_USER,
                                password=DUCK_PASSWORD, timeout=10)
        stdin, stdout, stderr = _ssh_client.exec_command(command, timeout=COMMAND_TIMEOUT)
        out = (stdout.read().decode() + stderr.read().decode()).strip()
        return _truncate(out or "(no output)")
    except Exception as e:
        _ssh_client = None  # force reconnect next time
        return f"(duck SSH error: {e})"


# --- browser (Playwright), lazily started, one shared page -------------------
# Playwright's sync API is bound to the thread that created it. Flask serves
# each request on a different worker thread, so we CANNOT touch Playwright from
# request threads. Instead we run ONE dedicated browser thread that owns all
# Playwright objects; request threads submit jobs to it over a queue and block
# on a per-job Event for the result. This is what makes the browser survive
# across many requests instead of dying with "cannot switch to a different
# thread".

import queue as _queue

_use_account_profile = False        # toggled from the UI
_browser_jobs = _queue.Queue()      # (job_dict, event, result_box)
_browser_thread = None
_browser_thread_lock = threading.Lock()


def _browser_worker():
    """Single long-lived thread: owns Playwright, processes jobs serially."""
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = None
    page = None
    current_profile = None  # tracks which profile the live browser uses

    def _page_is_dead(pg):
        """A page/browser closed externally leaves stale objects that raise
        when touched. Probe cheaply; treat any error as dead."""
        if pg is None:
            return True
        try:
            if pg.is_closed():
                return True
            _ = pg.url  # touching a dead page raises
            return False
        except Exception:
            return True

    def ensure_page(force_new=False):
        nonlocal browser, page, current_profile
        profile_changed = browser is not None and current_profile != _use_account_profile
        if force_new or profile_changed or _page_is_dead(page):
            # tear down whatever's there (it's dead, wrong profile, or forced)
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            browser, page = None, None
        if browser is None:
            launch_args = ["--start-maximized", "--window-size=1600,1000"]
            if _use_account_profile:
                profile_dir = os.path.expanduser("~/.jarvis-chrome-profile")
                browser = pw.chromium.launch_persistent_context(
                    profile_dir, headless=False, channel="chrome",
                    args=launch_args, no_viewport=True)
                page = browser.pages[0] if browser.pages else browser.new_page()
            else:
                browser = pw.chromium.launch(headless=False, channel="chrome",
                                             args=launch_args)
                context = browser.new_context(no_viewport=True)
                page = context.new_page()
            current_profile = _use_account_profile
        return page

    def handle(inp):
        action = inp.get("action")
        if action == "_shutdown_browser":
            nonlocal browser, page
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            browser, page = None, None
            return "Browser closed."
        pg = ensure_page()
        if action == "navigate":
            pg.goto(inp["url"], timeout=30000, wait_until="domcontentloaded")
            return f"Navigated to {pg.url} — title: {pg.title()}"
        if action == "read_text":
            return _truncate(pg.inner_text("body"))
        if action == "list_links":
            links = pg.eval_on_selector_all(
                "a[href]", "els => els.slice(0,60).map(e => e.innerText.trim()+' -> '+e.href).filter(Boolean)")
            return _truncate("\n".join(links) or "(no links found)")
        if action == "get_images":
            imgs = pg.eval_on_selector_all(
                "img", "els => els.slice(0,40).map(e => (e.alt||'(no alt)')+' -> '+e.src)")
            return _truncate("\n".join(imgs) or "(no images found)")
        if action == "screenshot":
            pg.screenshot(path=os.path.join("static", "shot.png"), full_page=False)
            return "Screenshot saved (view at /static/shot.png)."
        if action == "click":
            target = inp["target"]
            try:
                pg.click(target, timeout=5000)
            except Exception:
                pg.get_by_text(target, exact=False).first.click(timeout=5000)
            return f"Clicked '{target}'. Now at {pg.url}"
        if action == "type":
            pg.fill(inp["target"], inp["text"], timeout=5000)
            return f"Typed into '{inp['target']}'."
        return f"(unknown browser action: {action})"

    while True:
        job, event, box = _browser_jobs.get()
        try:
            box["result"] = handle(job)
        except Exception as e:
            # If the browser died mid-action, force a fresh launch and retry once.
            msg = str(e)
            if any(s in msg for s in ("has been closed", "Target page", "crashed",
                                      "Connection closed", "Browser closed")):
                try:
                    ensure_page(force_new=True)
                    box["result"] = handle(job)
                except Exception as e2:
                    box["result"] = f"(browser action '{job.get('action')}' failed after relaunch: {e2})"
            else:
                box["result"] = f"(browser action '{job.get('action')}' failed: {e})"
        finally:
            event.set()


def _ensure_browser_thread():
    global _browser_thread
    with _browser_thread_lock:
        if _browser_thread is None or not _browser_thread.is_alive():
            _browser_thread = threading.Thread(target=_browser_worker, daemon=True)
            _browser_thread.start()


def _run_browser(inp: dict) -> str:
    """Submit a browser job to the dedicated worker thread and wait for it."""
    _ensure_browser_thread()
    event = threading.Event()
    box = {"result": None}
    _browser_jobs.put((inp, event, box))
    if not event.wait(timeout=90):
        return "(browser action timed out)"
    return box["result"]


def _reset_browser():
    """Ask the worker to close the browser (e.g. on profile switch)."""
    if _browser_thread is not None and _browser_thread.is_alive():
        _run_browser({"action": "_shutdown_browser"})


# --------------------------------------------------------------- dispatch ----
def _dispatch(tool_name: str, tool_input: dict, action_id: str, transcript: list) -> str:
    """Decide safe-vs-gated, run, and append to transcript."""
    if tool_name == "run_shell":
        cmd = tool_input["command"]
        gated = not _is_safe_command(cmd)
        if gated and not _wait_for_approval(action_id, f"Run on Mac: {cmd}", cmd):
            out = "User denied this command."
        else:
            out = _run_local(cmd)
        transcript.append({"type": "action", "tool": "shell", "detail": cmd, "output": out})
        return out

    if tool_name == "run_on_duck":
        cmd = tool_input["command"]
        gated = not _is_safe_command(cmd)
        if gated and not _wait_for_approval(action_id, f"Run on DUCK: {cmd}", cmd):
            out = "User denied this command."
        else:
            out = _run_duck(cmd)
        transcript.append({"type": "action", "tool": "duck", "detail": cmd, "output": out})
        return out

    if tool_name == "browser":
        action = tool_input.get("action")
        label_detail = action + (f" {tool_input.get('url','')}" if action == "navigate" else "") \
            + (f" '{tool_input.get('target','')}'" if action in ("click", "type") else "")
        gated = action not in SAFE_BROWSER_ACTIONS
        if gated and not _wait_for_approval(action_id, f"Browser: {label_detail}", label_detail):
            out = "User denied this browser action."
        else:
            out = _run_browser(tool_input)
        transcript.append({"type": "action", "tool": "browser", "detail": label_detail, "output": out})
        return out

    # ---- external-service tools -------------------------------------------
    # Actions that change state / act on your behalf are gated; reads run free.
    GATED = {
        ("gmail", "send"), ("calendar", "create"), ("github", "create_issue"),
        ("spotify", "play"), ("spotify", "pause"),
    }
    action = tool_input.get("action")

    def _gate_and_run(tool_label, detail, fn):
        if (tool_name, action) in GATED:
            if not _wait_for_approval(action_id, f"{tool_label}: {detail}", detail):
                out = "User denied this action."
                transcript.append({"type": "action", "tool": tool_label, "detail": detail, "output": out})
                return out
        out = fn()
        transcript.append({"type": "action", "tool": tool_label, "detail": detail, "output": out})
        return out

    if tool_name == "fetch_page":
        return _gate_and_run("web", tool_input.get("url", ""),
                             lambda: ig.fetch_page_text(tool_input["url"]))
    if tool_name == "gmail":
        detail = f"{action} " + (tool_input.get("to") or tool_input.get("query") or "")
        return _gate_and_run("gmail", detail, lambda: ig.gmail(**tool_input))
    if tool_name == "calendar":
        detail = f"{action} " + (tool_input.get("summary") or f"next {tool_input.get('days',7)}d")
        return _gate_and_run("calendar", detail, lambda: ig.calendar(**tool_input))
    if tool_name == "github":
        detail = f"{action} {tool_input.get('repo','')}"
        return _gate_and_run("github", detail, lambda: ig.github(**tool_input))
    if tool_name == "news":
        return _gate_and_run("news", tool_input.get("query", "headlines"),
                             lambda: ig.news(tool_input.get("query")))
    if tool_name == "weather":
        return _gate_and_run("weather", tool_input.get("place", ""),
                             lambda: ig.weather(tool_input.get("lat"), tool_input.get("lon"),
                                                tool_input.get("place")))
    if tool_name == "spotify":
        detail = f"{action} " + (tool_input.get("query") or "")
        return _gate_and_run("spotify", detail, lambda: ig.spotify(**tool_input))

    return f"(unknown tool {tool_name})"


# ----------------------------------------------------------------- routes ----
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/static/<path:p>")
def static_files(p):
    return send_from_directory("static", p)


turn_lock = threading.Lock()   # browser chats and voice commands never overlap


@app.route("/chat", methods=["POST"])
def chat():
    return jsonify(run_turn(request.json["message"]))


def run_turn(user_msg):
    """One full assistant turn (may chain tools). Used by the browser /chat
    route AND the server-side voice listener. Returns the transcript list."""
    with turn_lock:
        return _run_turn_locked(user_msg)


def _run_turn_locked(user_msg):
    messages.append({"role": "user", "content": user_msg})

    # Recall relevant past conversation and inject it into the system prompt
    # for THIS turn only (so context stays focused and cheap).
    system_for_turn = SYSTEM_PROMPT.replace("__NAME__", assistant_name[0])
    try:
        recalled = memory.recall(user_msg, k=4)
        if recalled:
            system_for_turn = (SYSTEM_PROMPT.replace("__NAME__", assistant_name[0]) +
                "\n\n--- Things you already know about Arun and past work "
                "(treat these as your own knowledge — do NOT say 'according to "
                "my memory' or 'you previously told me'; just know it naturally) "
                "---\n" + recalled)
    except Exception as e:
        print(f"[memory] recall error: {e}")

    transcript = []
    assistant_texts = []
    shell_count = [0]   # enforce: at most one run_shell per user turn
    while True:
        resp = client.messages.create(
            model=current_model[0], max_tokens=1024, system=system_for_turn,
            tools=tools, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})

        for block in resp.content:
            if block.type == "text":
                transcript.append({"type": "text", "text": block.text})
                assistant_texts.append(block.text)

        if resp.stop_reason != "tool_use":
            break

        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                # Enforce one-shell-command-per-message: block extra shell calls.
                if block.name == "run_shell":
                    shell_count[0] += 1
                    if shell_count[0] > 1:
                        out = ("(Only one shell command per message. Tell Arun "
                               "the next command you'd run and take it on your "
                               "next turn.)")
                        transcript.append({"type": "action", "tool": "shell",
                                           "detail": block.input.get("command", ""),
                                           "output": out})
                        tool_results.append({"type": "tool_result",
                                             "tool_use_id": block.id, "content": out})
                        continue
                out = _dispatch(block.name, block.input, block.id, transcript)
                tool_results.append({"type": "tool_result",
                                     "tool_use_id": block.id, "content": out})
        messages.append({"role": "user", "content": tool_results})

    # Auto-save this exchange to memory.
    memory.remember_exchange(user_msg, " ".join(assistant_texts))

    return transcript


@app.route("/memory/import", methods=["POST"])
def memory_import():
    return jsonify({"result": memory.ingest_imported()})


@app.route("/memory/trim", methods=["POST"])
def memory_trim():
    return jsonify({"result": memory.trim_now()})


@app.route("/memory/stats")
def memory_stats():
    return jsonify(memory.stats())


@app.route("/memory/upload", methods=["POST"])
def memory_upload():
    """Accept transcript files uploaded from the browser, save them into
    imported_chats/, then ingest. One-step import — no manual file copying."""
    import_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "imported_chats")
    os.makedirs(import_dir, exist_ok=True)
    files = request.files.getlist("files")
    if not files:
        return jsonify({"result": "No files received."}), 400
    saved = 0
    for f in files:
        name = os.path.basename(f.filename or "")
        if not name:
            continue
        # only accept the transcript types we can read
        if not name.lower().endswith((".txt", ".md", ".json")):
            continue
        f.save(os.path.join(import_dir, name))
        saved += 1
    result = memory.ingest_imported()
    return jsonify({"result": f"Uploaded {saved} file(s). {result}"})



@app.route("/pending")
def pending():
    with _lock:
        for tid, e in _pending.items():
            if e["decision"] is None:
                return jsonify({"id": tid, "label": e["label"], "detail": e["detail"]})
    return jsonify({"id": None})


@app.route("/decide", methods=["POST"])
def decide():
    with _lock:
        e = _pending.get(request.json["id"])
        if e:
            e["decision"] = request.json["decision"]
            e["event"].set()
    return jsonify({"ok": True})


@app.route("/browser_profile", methods=["POST"])
def browser_profile():
    """UI toggle: switch between clean browser and your logged-in profile."""
    global _use_account_profile
    want_account = bool(request.json.get("account"))
    if want_account != _use_account_profile:
        _use_account_profile = want_account
        _reset_browser()  # next browser action relaunches with the chosen profile
    return jsonify({"account": _use_account_profile})


@app.route("/connections")
def connections():
    """Report which external services are configured/connected (for the header)."""
    return jsonify({"google": ig.google_status(), "spotify": ig.spotify_status()})


@app.route("/connect/<service>", methods=["POST"])
def connect(service):
    """Kick off the OAuth consent flow (opens the provider's button in a browser)."""
    if service == "google":
        return jsonify({"result": ig.google_authorize()})
    if service == "spotify":
        return jsonify({"result": ig.spotify_authorize()})
    return jsonify({"result": f"(unknown service {service})"}), 400


@app.route("/model", methods=["GET"])
def get_model():
    return jsonify({"current": current_model[0], "options": ALLOWED_MODELS})


@app.route("/model", methods=["POST"])
def set_model():
    m = request.json.get("model")
    if m in ALLOWED_MODELS:
        current_model[0] = m
        return jsonify({"current": current_model[0]})
    return jsonify({"error": "unknown model"}), 400


@app.route("/speak", methods=["POST"])
def speak():
    """Turn text into speech audio via ElevenLabs; stream bytes back to browser."""
    from flask import Response
    text = request.json.get("text", "")
    audio, mime = ig.tts(text)
    if audio is None:
        return jsonify({"error": mime}), 400   # mime holds the error string here
    return Response(audio, mimetype=mime)


@app.route("/voice_status")
def voice_status():
    return jsonify({"elevenlabs": ig.elevenlabs_status()["configured"],
                    "fish": ig.fish_status()["configured"]})


@app.route("/fish_voices")
def fish_voices():
    return jsonify(ig.fish_list_voices())


@app.route("/fish_voice", methods=["POST"])
def fish_voice():
    if "reference_id" in request.json:
        ig.fish_set_voice(request.json.get("reference_id"))
    if "speed" in request.json:
        ig.fish_set_speed(request.json.get("speed"))
    return jsonify(ig.fish_list_voices())


# ---------------------------------------------- server-side voice commands ---
# The wake word listener (wakeword.py) hands commands here. We run them as a
# normal turn, record the result so the browser can display it, and speak the
# reply through the Mac's speakers (afplay) so it works with the tab hidden.
voice_events = []
_voice_counter = [0]
_voice_lock = threading.Lock()
voice_reply_on = [True]   # mirrors the 🔊 toggle in the UI


def _speak_local(text):
    if not voice_reply_on[0] or not text.strip():
        return
    afplay = shutil.which("afplay")          # macOS built-in player
    if not afplay:
        return
    audio, mime = ig.tts(text)
    if audio is None:
        print(f"[voice] TTS failed: {mime}")
        return
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(audio)
        path = f.name
    wakeword.mute(600)                       # don't hear ourselves talking
    try:
        subprocess.run([afplay, path], timeout=300)
    except Exception as e:
        print(f"[voice] playback failed: {e}")
    finally:
        wakeword.mute(0.8)                   # short tail, then listen again
        try:
            os.unlink(path)
        except OSError:
            pass


def _on_voice_command(cmd):
    try:
        items = run_turn(cmd)
    except Exception as e:
        items = [{"type": "text", "text": f"(voice command failed: {e})"}]
    with _voice_lock:
        _voice_counter[0] += 1
        voice_events.append({"id": _voice_counter[0], "user": cmd, "items": items})
        del voice_events[:-50]               # keep the last 50
    _speak_local(" ".join(i["text"] for i in items if i.get("type") == "text"))


@app.route("/wake/start", methods=["POST"])
def wake_start():
    wakeword.set_word((request.json or {}).get("word"))
    return jsonify(wakeword.start(_on_voice_command))


@app.route("/wake/stop", methods=["POST"])
def wake_stop():
    return jsonify(wakeword.stop())


@app.route("/wake/config", methods=["POST"])
def wake_config():
    wakeword.set_word((request.json or {}).get("word"))
    return jsonify(wakeword.status())


@app.route("/wake/status")
def wake_status():
    return jsonify(wakeword.status())


@app.route("/voice_events")
def get_voice_events():
    after = int(request.args.get("after", 0))
    with _voice_lock:
        return jsonify({"events": [e for e in voice_events if e["id"] > after]})


@app.route("/voice_reply", methods=["POST"])
def set_voice_reply():
    voice_reply_on[0] = bool((request.json or {}).get("on"))
    return jsonify({"on": voice_reply_on[0]})


@app.route("/name", methods=["GET"])
def get_name():
    return jsonify({"name": assistant_name[0]})


@app.route("/name", methods=["POST"])
def set_name():
    n = (request.json.get("name") or "").strip()
    if n:
        assistant_name[0] = n[:30]
    return jsonify({"name": assistant_name[0]})


@app.route("/voices")
def voices():
    return jsonify({"voices": ig.elevenlabs_list_voices(),
                    "current": ig._current_voice()})


@app.route("/voice", methods=["POST"])
def set_voice():
    ig.elevenlabs_set_voice(request.json.get("voice_id"))
    return jsonify({"current": ig._current_voice()})


@app.route("/reset", methods=["POST"])
def reset():
    messages.clear()
    return jsonify({"ok": True})


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY first.")
    if not DUCK_PASSWORD:
        print("[warn] DUCK_PASSWORD not set — the duck tool will error until you set it.")
    app.run(port=5001, threaded=True)
