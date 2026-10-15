# Connecting the integrations

Seven new tools. Three need nothing, two need a free key, two use an OAuth
"Connect" button. Set the env-var ones in the same terminal where you run
`python3 server.py`, alongside ANTHROPIC_API_KEY and DUCK_PASSWORD.

## Works immediately (no setup)
- **web_search** — built into the Anthropic API.
- **fetch_page** — reads any page's text.
- **weather** — open-meteo, no key.

## Needs a free API key (2 min each)

**News (GNews):**
1. Sign up at https://gnews.io/ → copy your API key.
2. `export NEWS_API_KEY='your-key'`

**GitHub:**
1. https://github.com/settings/tokens → Generate new token (fine-grained is best).
   Give it read access to the repos you care about; add "Issues: write" only if
   you want Jarvis to create issues.
2. `export GITHUB_TOKEN='your-token'`

## OAuth "Connect" button (5 min, one-time)

### Google (Gmail + Calendar)
You create a Google Cloud OAuth client once; after that the header "Connect
Google" button does the rest.

1. Go to https://console.cloud.google.com/ → create a project (any name).
2. APIs & Services → **Enable APIs** → enable **Gmail API** and
   **Google Calendar API**.
3. APIs & Services → **OAuth consent screen** → choose **External** → fill the
   minimum (app name, your email) → add yourself under **Test users** (this lets
   you skip Google's verification while it's just you).
4. APIs & Services → **Credentials** → Create Credentials → **OAuth client ID** →
   application type **Desktop app** → Create → **Download JSON**.
5. Rename that file to **`client_secret.json`** and put it in the `jarvis` folder
   (next to server.py).
6. Start the server, open the page, click **Connect Google**. A real Google
   consent screen opens in your browser — approve Gmail + Calendar. Done. A
   `token_google.json` is cached so you won't repeat this.

Revoke anytime at https://myaccount.google.com/permissions.

### Spotify
1. https://developer.spotify.com/dashboard → Create app (any name).
2. In the app settings, add redirect URI **exactly**:
   `http://localhost:8888/callback`
3. Copy the **Client ID** and **Client Secret**, then:
   ```
   export SPOTIFY_CLIENT_ID='...'
   export SPOTIFY_CLIENT_SECRET='...'
   export SPOTIFY_REDIRECT_URI='http://localhost:8888/callback'
   ```
4. Start the server, click **Connect Spotify**, approve in the browser.
   (Playback control needs an active Spotify device — have the app open.)

## What's gated (asks Approve before running)
- gmail **send**
- calendar **create**
- github **create_issue**
- spotify **play / pause**

Everything else (searches, reads, listing, weather, news, headlines) runs
immediately.

## Full startup with everything

**Easiest: use a `.env` file (set keys once).** Rename `.env.example` to `.env`,
fill in your values, and just run `python3 server.py` — the server loads them
automatically. No more re-exporting every session.

```bash
cd jarvis
source venv/bin/activate
cp .env.example .env      # then edit .env with your keys
python3 server.py
```

Anything you'd rather export in the shell still works and overrides the file:
```bash
 export ANTHROPIC_API_KEY='sk-ant-...'   # wins over .env if both set
 export DUCK_PASSWORD='...'
export NEWS_API_KEY='...'          # optional
export GITHUB_TOKEN='...'          # optional
export SPOTIFY_CLIENT_ID='...'     # optional
export SPOTIFY_CLIENT_SECRET='...' # optional
export SPOTIFY_REDIRECT_URI='http://localhost:8888/callback'
python3 server.py
```
Google needs no env var — just client_secret.json in the folder + the button.
