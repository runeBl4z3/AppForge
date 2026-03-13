# AppForge — Free App Builder

Turn any website into a real Android APK, Windows EXE, iOS IPA, and Linux AppImage.  
No account required. No paywall. Everything free.

---

## Files

| File | Purpose |
|---|---|
| `appbuilder.html` | The website frontend |
| `build.py` | The app builder (Python, no extra packages needed) |
| `server.py` | Flask backend that connects website → builder |
| `requirements.txt` | Python dependencies (just Flask) |
| `Procfile` | For Railway / Heroku deployment |

---

## Run Locally

```bash
# Install Flask
pip install flask

# Start the server
python server.py

# Visit in browser
http://localhost:5000
```

Paste any website URL → click Build → real files download.

---

## Deploy Free on Railway

1. Push this repo to GitHub (don't commit the `builds/` folder)
2. Go to [railway.app](https://railway.app) → **New Project → Deploy from GitHub**
3. Select your repo → Railway auto-detects Python → deploys
4. Copy your Railway URL (e.g. `https://appforge-xyz.up.railway.app`)
5. Open `appbuilder.html`, find this line near the top of the JS:
   ```js
   const API_BASE = '';
   ```
   Change it to:
   ```js
   const API_BASE = 'https://appforge-xyz.up.railway.app';
   ```
6. Redeploy → the website now works for anyone in the world

---

## Build from Command Line (no server needed)

```bash
python build.py --url https://yoursite.com --name "My App" --package com.you.myapp
```

**Options:**
```
--url         Website URL to wrap (required)
--name        App name (required)
--package     Bundle ID (auto-generated if blank)
--version     Version name (default: 1.0.0)
--vcode       Version code int (default: 1)
--minsdk      Android min SDK (default: 21)
--targetsdk   Android target SDK (default: 34)
--out         Output directory (default: ./output)
--platform    all | android,windows,ios,linux (default: all)
```

---

## .gitignore

Add this to avoid committing build outputs:

```
builds/
output/
__pycache__/
*.pyc
```
