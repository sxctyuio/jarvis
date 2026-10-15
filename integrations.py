"""
integrations.py — external-service tools for Jarvis.

Auth model:
  - Google (Gmail, Calendar): OAuth. You create a Google Cloud OAuth client once
    (client_secret.json placed next to this file). The first time you use a
    Google tool, the app opens Google's real consent screen in your browser;
    you approve; a revocable token is cached in token_google.json. No password
    ever touches the app. Revoke anytime at myaccount.google.com/permissions.
  - Spotify: same idea — create an app in the Spotify dashboard, put its client
    id/secret in env vars, approve once via the browser, token cached.
  - GitHub, News: simple API keys in env vars (GITHUB_TOKEN, NEWS_API_KEY).
  - Weather, web fetch: no auth.

Every function here returns a plain string (for the model) and does ONE thing.
Whether an action is gated (needs your Approve click) is decided in server.py,
not here.
"""

import os
import json
import base64
import datetime
import requests

HERE = os.path.dirname(os.path.abspath(__file__))

# ============================================================ no-auth tools ==

def weather(lat: float = None, lon: float = None, place: str = None) -> str:
    """Current weather via open-meteo (no key)."""
    try:
        if place and (lat is None or lon is None):
            g = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                             params={"name": place, "count": 1}, timeout=15).json()
            if not g.get("results"):
                return f"(couldn't find location '{place}')"
            lat = g["results"][0]["latitude"]
            lon = g["results"][0]["longitude"]
            place = g["results"][0]["name"]
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,precipitation,weather_code,wind_speed_10m",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        }, timeout=15).json()
        c = r.get("current", {})
        return (f"{place or f'{lat},{lon}'}: {c.get('temperature_2m')}°F, "
                f"precip {c.get('precipitation')}mm, wind {c.get('wind_speed_10m')}mph")
    except Exception as e:
        return f"(weather error: {e})"


def fetch_page_text(url: str, limit: int = 8000) -> str:
    """Fetch a URL and return readable text (crude HTML strip). Read-only."""
    try:
        import re
        html = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (JarvisBot)"}).text
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit] + ("..." if len(text) > limit else "")
    except Exception as e:
        return f"(fetch error: {e})"


def news(query: str = None, limit: int = 6) -> str:
    """Top headlines or a search, via GNews (free key in NEWS_API_KEY)."""
    key = os.environ.get("NEWS_API_KEY")
    if not key:
        return "(NEWS_API_KEY not set — get a free key at gnews.io and set the env var)"
    try:
        if query:
            url = "https://gnews.io/api/v4/search"
            params = {"q": query, "max": limit, "apikey": key, "lang": "en"}
        else:
            url = "https://gnews.io/api/v4/top-headlines"
            params = {"max": limit, "apikey": key, "lang": "en", "category": "general"}
        arts = requests.get(url, params=params, timeout=20).json().get("articles", [])
        if not arts:
            return "(no articles)"
        return "\n".join(f"- {a['title']} ({a['source']['name']}) {a['url']}"
                         for a in arts[:limit])
    except Exception as e:
        return f"(news error: {e})"


# ============================================================== GitHub =======

def github(action: str, repo: str = None, number: int = None,
           title: str = None, body: str = None) -> str:
    """GitHub via REST. Token in GITHUB_TOKEN. repo like 'owner/name'."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return "(GITHUB_TOKEN not set — add it to your .env, from github.com/settings/tokens)"
    h = {"Authorization": f"Bearer {token}",
         "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    base = "https://api.github.com"

    def _get(url, **params):
        """GET that raises a readable error instead of returning a dict the
        caller then indexes like a list."""
        resp = requests.get(url, headers=h, params=params or None, timeout=20)
        if resp.status_code == 401:
            raise RuntimeError("Bad credentials — check GITHUB_TOKEN in your .env.")
        if resp.status_code == 403:
            raise RuntimeError("Forbidden — token lacks permission or rate-limited.")
        if resp.status_code == 404:
            raise RuntimeError("Not found — repo doesn't exist or token can't see it.")
        if resp.status_code >= 400:
            body = resp.json() if resp.content else {}
            raise RuntimeError(f"GitHub {resp.status_code}: {body.get('message', resp.text[:120])}")
        return resp.json()

    try:
        if action == "whoami":
            me = _get(f"{base}/user")
            return f"Logged in as {me['login']} ({me.get('name','')})"
        if action == "list_repos":
            # the authenticated user's own repos, most recently pushed first
            repos = _get(f"{base}/user/repos", per_page=100, sort="pushed",
                         affiliation="owner")
            if not isinstance(repos, list):
                raise RuntimeError(f"unexpected response: {repos}")
            return "\n".join(f"{r['full_name']}  ({'private' if r['private'] else 'public'})"
                             for r in repos) or "(no repos)"
        if action == "list_issues":
            r = _get(f"{base}/repos/{repo}/issues", state="open", per_page=20)
            return "\n".join(f"#{i['number']} {i['title']}" for i in r
                             if "pull_request" not in i) or "(no open issues)"
        if action == "list_prs":
            r = _get(f"{base}/repos/{repo}/pulls", state="open", per_page=20)
            return "\n".join(f"#{p['number']} {p['title']}" for p in r) or "(no open PRs)"
        if action == "check_runs":
            repo_info = _get(f"{base}/repos/{repo}")
            sha = _get(f"{base}/repos/{repo}/commits/{repo_info['default_branch']}")["sha"]
            runs = _get(f"{base}/repos/{repo}/commits/{sha}/check-runs").get("check_runs", [])
            return "\n".join(f"{c['name']}: {c.get('conclusion') or c['status']}"
                             for c in runs) or "(no checks)"
        if action == "read_file":
            r = _get(f"{base}/repos/{repo}/contents/{title}")
            return base64.b64decode(r["content"]).decode()[:8000]
        if action == "create_issue":  # GATED in server.py
            resp = requests.post(f"{base}/repos/{repo}/issues", headers=h,
                                 json={"title": title, "body": body or ""}, timeout=20)
            if resp.status_code >= 400:
                return f"(github {resp.status_code}: {resp.json().get('message','')})"
            r = resp.json()
            return f"Created issue #{r['number']}: {r['html_url']}"
        return f"(unknown github action: {action})"
    except RuntimeError as e:
        return f"(github: {e})"
    except Exception as e:
        return f"(github error: {e})"


# ============================================= Google OAuth (Gmail/Calendar) =

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]
_GOOGLE_TOKEN = os.path.join(HERE, "token_google.json")
_GOOGLE_SECRET = os.path.join(HERE, "client_secret.json")


def google_status() -> dict:
    return {"configured": os.path.exists(_GOOGLE_SECRET),
            "connected": os.path.exists(_GOOGLE_TOKEN)}


def _google_creds():
    """Return valid creds, refreshing if needed. None if not connected yet."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    if not os.path.exists(_GOOGLE_TOKEN):
        return None
    creds = Credentials.from_authorized_user_file(_GOOGLE_TOKEN, GOOGLE_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(_GOOGLE_TOKEN, "w") as f:
            f.write(creds.to_json())
    return creds


def google_authorize() -> str:
    """Run the OAuth consent flow (opens Google in the browser). One-time."""
    if not os.path.exists(_GOOGLE_SECRET):
        return ("(no client_secret.json — create a Google OAuth client first; "
                "see README Google setup)")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(_GOOGLE_SECRET, GOOGLE_SCOPES)
        creds = flow.run_local_server(port=0)  # opens the consent button in browser
        with open(_GOOGLE_TOKEN, "w") as f:
            f.write(creds.to_json())
        return "Google connected. Gmail and Calendar are ready."
    except Exception as e:
        return f"(google authorize error: {e})"


def _gmail_service():
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=_google_creds())


def gmail(action: str, to: str = None, subject: str = None, body: str = None,
          query: str = None, limit: int = 8) -> str:
    creds = _google_creds()
    if creds is None:
        return "(Google not connected — connect it from the Jarvis header first)"
    try:
        svc = _gmail_service()
        if action == "search" or action == "list":
            q = query or "in:inbox"
            msgs = svc.users().messages().list(userId="me", q=q,
                                               maxResults=limit).execute().get("messages", [])
            out = []
            for m in msgs[:limit]:
                full = svc.users().messages().get(userId="me", id=m["id"],
                                                  format="metadata",
                                                  metadataHeaders=["From", "Subject", "Date"]).execute()
                hdr = {h["name"]: h["value"] for h in full["payload"]["headers"]}
                out.append(f"- {hdr.get('From','?')} | {hdr.get('Subject','(no subj)')} | {hdr.get('Date','')}")
            return "\n".join(out) or "(no messages)"
        if action == "read":
            full = svc.users().messages().get(userId="me", id=query, format="full").execute()
            return _extract_gmail_body(full)[:8000]
        if action == "send":  # GATED in server.py
            raw = _build_mime(to, subject, body)
            svc.users().messages().send(userId="me", body={"raw": raw}).execute()
            return f"Sent email to {to}."
        return f"(unknown gmail action: {action})"
    except Exception as e:
        return f"(gmail error: {e})"


def _build_mime(to, subject, body):
    from email.mime.text import MIMEText
    msg = MIMEText(body or "")
    msg["to"] = to
    msg["subject"] = subject or "(no subject)"
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _extract_gmail_body(full):
    import base64 as b64
    def walk(part):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return b64.urlsafe_b64decode(part["body"]["data"]).decode(errors="replace")
        for p in part.get("parts", []) or []:
            t = walk(p)
            if t:
                return t
        return ""
    return walk(full["payload"]) or "(no plain-text body)"


def calendar(action: str, summary: str = None, start: str = None, end: str = None,
             days: int = 7) -> str:
    creds = _google_creds()
    if creds is None:
        return "(Google not connected — connect it from the Jarvis header first)"
    try:
        from googleapiclient.discovery import build
        svc = build("calendar", "v3", credentials=creds)
        if action == "list":
            now = datetime.datetime.utcnow().isoformat() + "Z"
            end_t = (datetime.datetime.utcnow() + datetime.timedelta(days=days)).isoformat() + "Z"
            evs = svc.events().list(calendarId="primary", timeMin=now, timeMax=end_t,
                                    singleEvents=True, orderBy="startTime").execute().get("items", [])
            out = []
            for e in evs:
                s = e["start"].get("dateTime", e["start"].get("date"))
                out.append(f"- {s} — {e.get('summary','(no title)')}")
            return "\n".join(out) or "(no upcoming events)"
        if action == "create":  # GATED in server.py
            ev = {"summary": summary,
                  "start": {"dateTime": start}, "end": {"dateTime": end}}
            r = svc.events().insert(calendarId="primary", body=ev).execute()
            return f"Created event '{summary}': {r.get('htmlLink')}"
        return f"(unknown calendar action: {action})"
    except Exception as e:
        return f"(calendar error: {e})"


# ================================================================ Spotify ====

_SPOTIFY_TOKEN = os.path.join(HERE, "token_spotify.json")
SPOTIFY_SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing"


def spotify_status() -> dict:
    return {"configured": bool(os.environ.get("SPOTIFY_CLIENT_ID")),
            "connected": os.path.exists(_SPOTIFY_TOKEN)}


def _spotify_client():
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
    auth = SpotifyOAuth(
        client_id=os.environ.get("SPOTIFY_CLIENT_ID"),
        client_secret=os.environ.get("SPOTIFY_CLIENT_SECRET"),
        redirect_uri=os.environ.get("SPOTIFY_REDIRECT_URI", "http://localhost:8888/callback"),
        scope=SPOTIFY_SCOPES, cache_path=_SPOTIFY_TOKEN, open_browser=True)
    return __import__("spotipy").Spotify(auth_manager=auth)


def spotify_authorize() -> str:
    if not os.environ.get("SPOTIFY_CLIENT_ID"):
        return "(SPOTIFY_CLIENT_ID/SECRET not set — create an app at developer.spotify.com)"
    try:
        sp = _spotify_client()
        me = sp.current_user()
        return f"Spotify connected as {me.get('display_name', me['id'])}."
    except Exception as e:
        return f"(spotify authorize error: {e})"


def spotify(action: str, query: str = None) -> str:
    if not os.environ.get("SPOTIFY_CLIENT_ID"):
        return "(Spotify not configured)"
    try:
        sp = _spotify_client()
        if action == "current":
            cur = sp.current_playback()
            if not cur or not cur.get("item"):
                return "(nothing playing)"
            it = cur["item"]
            return f"Now playing: {it['name']} — {', '.join(a['name'] for a in it['artists'])}"
        if action == "search":
            res = sp.search(q=query, type="track", limit=5)["tracks"]["items"]
            return "\n".join(f"- {t['name']} — {', '.join(a['name'] for a in t['artists'])} "
                             f"(uri: {t['uri']})" for t in res) or "(no results)"
        if action == "play":  # GATED in server.py
            if query and query.startswith("spotify:"):
                sp.start_playback(uris=[query])
            else:
                res = sp.search(q=query, type="track", limit=1)["tracks"]["items"]
                if not res:
                    return f"(no track found for '{query}')"
                sp.start_playback(uris=[res[0]["uri"]])
                return f"Playing {res[0]['name']} — {res[0]['artists'][0]['name']}"
            return "Playback started."
        if action == "pause":  # GATED
            sp.pause_playback()
            return "Paused."
        return f"(unknown spotify action: {action})"
    except Exception as e:
        return f"(spotify error: {e})"


# ================================================== ElevenLabs text-to-speech =

_ELEVEN_VOICE_DEFAULT = "21m00Tcm4TlvDq8ikWAM"  # "Rachel" — a common default voice
_selected_voice = [None]   # runtime override set from the UI


def elevenlabs_list_voices():
    """Return [(voice_id, name), ...] from the user's ElevenLabs account."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return []
    try:
        r = requests.get("https://api.elevenlabs.io/v1/voices",
                         headers={"xi-api-key": key}, timeout=15)
        if r.status_code != 200:
            return []
        return [(v["voice_id"], v["name"]) for v in r.json().get("voices", [])]
    except Exception:
        return []


def elevenlabs_set_voice(voice_id):
    _selected_voice[0] = voice_id or None


def _current_voice():
    return (_selected_voice[0]
            or os.environ.get("ELEVENLABS_VOICE_ID")
            or _ELEVEN_VOICE_DEFAULT)


def elevenlabs_tts(text, voice_id=None):
    """Return (audio_bytes, mimetype) for the given text, or (None, error_str).
    Key in ELEVENLABS_API_KEY. Voice via ELEVENLABS_VOICE_ID or default."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        return None, "(ELEVENLABS_API_KEY not set — get one at elevenlabs.io)"
    voice = voice_id or _current_voice()
    # keep requests reasonable — very long replies cost a lot of characters
    text = (text or "").strip()[:2500]
    if not text:
        return None, "(nothing to speak)"
    try:
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
            headers={"xi-api-key": key, "Content-Type": "application/json"},
            json={"text": text, "model_id": "eleven_turbo_v2_5",
                  "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
            timeout=30)
        if r.status_code != 200:
            try:
                msg = r.json().get("detail", {})
                msg = msg.get("message", str(msg)) if isinstance(msg, dict) else str(msg)
            except Exception:
                msg = r.text[:120]
            return None, f"(ElevenLabs {r.status_code}: {msg})"
        return r.content, "audio/mpeg"
    except Exception as e:
        return None, f"(ElevenLabs error: {e})"


def elevenlabs_status():
    return {"configured": bool(os.environ.get("ELEVENLABS_API_KEY"))}


# ================================================== Fish Audio text-to-speech =
# Newer, cheaper, higher-rated than ElevenLabs, and (during the free window)
# the s2.1-pro-free model has no character cap. Key in FISH_API_KEY.
#
# Voice & speed are switchable at runtime from the UI. Fish exposes many voices
# by reference_id (browse fish.audio to find more). A small curated set of
# popular free/preset voices is offered by default; you can also paste any
# reference_id from the Fish playground.

# Only "Default" is guaranteed (needs no id — uses the model's built-in voice).
# For other voices, browse fish.audio, open a voice, copy its reference_id, and
# paste it in the UI's voice field (or set FISH_REFERENCE_ID). We don't hardcode
# a list of ids because Fish's catalog changes and stale ids would just error.
FISH_PRESET_VOICES = {
    "Default (model voice)": "",
}

_fish_voice = [os.environ.get("FISH_REFERENCE_ID", "")]   # runtime-settable
_fish_speed = [1.0]                                        # runtime-settable


def fish_set_voice(reference_id):
    _fish_voice[0] = reference_id or ""


def fish_set_speed(speed):
    try:
        s = float(speed)
        _fish_speed[0] = max(0.5, min(2.0, s))   # clamp to a sane range
    except (TypeError, ValueError):
        pass


def fish_list_voices():
    return {"presets": FISH_PRESET_VOICES,
            "current_voice": _fish_voice[0],
            "current_speed": _fish_speed[0]}


def fish_tts(text, reference_id=None, speed=None):
    """Return (audio_bytes, mimetype) or (None, error_str)."""
    key = os.environ.get("FISH_API_KEY")
    if not key:
        return None, "(FISH_API_KEY not set — get one at fish.audio)"
    text = (text or "").strip()[:2500]
    if not text:
        return None, "(nothing to speak)"
    spd = speed if speed is not None else _fish_speed[0]
    body = {"text": text, "format": "mp3", "mp3_bitrate": 128,
            "prosody": {"speed": spd, "volume": 0}}
    ref = reference_id if reference_id is not None else _fish_voice[0]
    if ref:
        body["reference_id"] = ref
    model = os.environ.get("FISH_MODEL", "s2.1-pro-free")  # model set via HEADER
    try:
        r = requests.post(
            "https://api.fish.audio/v1/tts",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "model": model},
            json=body, timeout=30)
        if r.status_code != 200:
            return None, f"(Fish Audio {r.status_code}: {r.text[:140]})"
        return r.content, "audio/mpeg"
    except Exception as e:
        return None, f"(Fish Audio error: {e})"


def fish_status():
    return {"configured": bool(os.environ.get("FISH_API_KEY"))}


# ---- unified TTS: prefer Fish Audio, fall back to ElevenLabs -----------------
def tts(text):
    """Try Fish Audio first, then ElevenLabs. (audio_bytes, mime) or (None, err)."""
    errors = []
    if os.environ.get("FISH_API_KEY"):
        audio, mime = fish_tts(text)
        if audio is not None:
            return audio, mime
        errors.append(mime)
    if os.environ.get("ELEVENLABS_API_KEY"):
        audio, mime = elevenlabs_tts(text)
        if audio is not None:
            return audio, mime
        errors.append(mime)
    if not errors:
        errors.append("(no TTS configured — set FISH_API_KEY or ELEVENLABS_API_KEY)")
    return None, " ".join(errors)
