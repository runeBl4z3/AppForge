"""
AppForge Backend — Multi-Platform GitHub Actions Build Engine
Triggers Android, Windows, and Linux builds in parallel.
"""
import os, sys, uuid, json, time, threading, shutil, zipfile, requests
from pathlib import Path
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

GITHUB_TOKEN  = os.environ.get('GITHUB_TOKEN', '')
GITHUB_OWNER  = os.environ.get('GITHUB_OWNER', 'runeBl4z3')
GITHUB_REPO   = os.environ.get('GITHUB_REPO',  'AppForge')
WORKFLOW_FILE = 'build-apk.yml'
BASE_DIR      = Path(__file__).parent
BUILDS_DIR    = BASE_DIR / 'builds'
BUILDS_DIR.mkdir(exist_ok=True)
GH_API        = 'https://api.github.com'
jobs          = {}

def gh_headers():
    return {'Authorization': f'token {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github.v3+json',
            'X-GitHub-Api-Version': '2022-11-28'}

def trigger_workflow(job_id, config, platforms='android,windows,linux'):
    url = f'{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches'
    r = requests.post(url, headers=gh_headers(), json={
        'ref': 'main',
        'inputs': {
            'app_name':    config['name'],
            'package_name': config['package'],
            'website_url': config['url'],
            'version_name': config.get('version_name', '1.0.0'),
            'job_id':      job_id,
            'platforms':   platforms,
        }
    }, timeout=30)
    return r.status_code == 204

def find_workflow_run(before_time, max_wait=60):
    url = f'{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs'
    for _ in range(max_wait):
        time.sleep(3)
        r = requests.get(url, headers=gh_headers(),
                         params={'workflow_id': WORKFLOW_FILE, 'per_page': 5}, timeout=30)
        if r.status_code != 200:
            continue
        for run in r.json().get('workflow_runs', []):
            if run.get('created_at', '') >= before_time:
                return run['id']
    return None

def wait_for_run(run_id, job_id, max_minutes=15):
    url = f'{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs/{run_id}'
    deadline = time.time() + max_minutes * 60
    while time.time() < deadline:
        time.sleep(12)
        r = requests.get(url, headers=gh_headers(), timeout=30)
        if r.status_code != 200:
            continue
        run = r.json()
        status = run.get('status', '')
        conclusion = run.get('conclusion')
        pct = {'queued': 15, 'in_progress': 55, 'completed': 90}.get(status, 30)
        jobs[job_id].update({'progress': pct, 'message': f'Building on GitHub Actions: {status}...'})
        if status == 'completed':
            return conclusion in ('success', 'partial'), conclusion
    return False, 'timeout'

def download_artifacts(run_id, job_id, out_dir):
    """Download all artifacts (android, windows, linux) from the run."""
    url = f'{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs/{run_id}/artifacts'
    r = requests.get(url, headers=gh_headers(), timeout=30)
    if r.status_code != 200:
        return []

    downloaded = []
    platform_map = {
        'android': {'ext': '.apk',      'label': 'Android APK',      'icon': '🤖'},
        'windows': {'ext': '_Setup.exe', 'label': 'Windows Installer', 'icon': '🪟'},
        'linux':   {'ext': '.AppImage',  'label': 'Linux AppImage',    'icon': '🐧'},
    }

    for artifact in r.json().get('artifacts', []):
        name = artifact['name']
        platform = None
        for p in platform_map:
            if name.startswith(p + '-'):
                platform = p
                break
        if not platform:
            continue

        r2 = requests.get(artifact['archive_download_url'], headers=gh_headers(),
                          stream=True, timeout=180)
        if r2.status_code != 200:
            continue

        zp = out_dir / f'{platform}_artifact.zip'
        with open(zp, 'wb') as f:
            for chunk in r2.iter_content(8192):
                f.write(chunk)

        with zipfile.ZipFile(zp) as zf:
            zf.extractall(out_dir)
        zp.unlink()

        info = platform_map[platform]
        found = list(out_dir.glob(f'*{info["ext"]}'))
        if not found:
            # Try any file that was extracted
            found = [f for f in out_dir.iterdir() if f.suffix in ('.apk', '.exe', '.AppImage')]

        if found:
            file_path = found[0]
            kb = file_path.stat().st_size // 1024
            downloaded.append({
                'name':     file_path.name,
                'label':    info['label'],
                'platform': platform,
                'url':      f'/api/download/{job_id}/{file_path.name}',
                'icon':     info['icon'],
                'size_kb':  kb,
            })

    return downloaded

def run_build(job_id, config, platforms):
    try:
        out_dir = BUILDS_DIR / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        jobs[job_id].update({'status': 'running', 'progress': 5,
                             'message': f'Triggering GitHub Actions ({platforms})...'})

        if not GITHUB_TOKEN:
            raise RuntimeError('GITHUB_TOKEN not set in Railway environment variables.')

        import datetime
        before = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

        if not trigger_workflow(job_id, config, platforms):
            raise RuntimeError('Failed to trigger GitHub Actions workflow. Check GITHUB_TOKEN permissions.')

        jobs[job_id].update({'progress': 10, 'message': 'Queued — waiting for runners to start...'})

        run_id = find_workflow_run(before)
        if not run_id:
            raise RuntimeError('Could not find GitHub Actions run. Workflow may have failed to queue.')

        jobs[job_id].update({'progress': 15, 'message': f'Run #{run_id} started — building all platforms...'})

        success, conclusion = wait_for_run(run_id, job_id)
        if not success and conclusion not in ('success', 'partial'):
            raise RuntimeError(f'Build {conclusion}. See https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/actions')

        jobs[job_id].update({'progress': 92, 'message': 'Build complete! Downloading files...'})

        files = download_artifacts(run_id, job_id, out_dir)
        if not files:
            raise RuntimeError('No build artifacts found. Check GitHub Actions logs.')

        jobs[job_id].update({
            'status':   'done',
            'progress': 100,
            'message':  f'Done! {len(files)} platform(s) built successfully.',
            'files':    files,
        })

    except Exception as e:
        jobs[job_id].update({'status': 'error', 'progress': 0,
                             'message': str(e), 'error': str(e)})

# ── Routes ────────────────────────────────────────────────────────────

@app.route('/')
def index():
    p = BASE_DIR / 'appbuilder.html'
    return p.read_text() if p.exists() else '<h1>AppForge</h1>'

@app.route('/api/build', methods=['POST'])
def api_build():
    data = request.get_json(force=True) or {}
    url  = (data.get('url') or '').strip()
    name = (data.get('name') or 'My App').strip()
    pkg  = (data.get('package') or '').strip()
    platforms = (data.get('platforms') or 'android,windows,linux').strip()

    if not url or not url.startswith('http'):
        return jsonify({'error': 'Invalid URL'}), 400
    if not pkg:
        import re
        host = url.replace('https://','').replace('http://','').replace('www.','').split('/')[0]
        safe = re.sub(r'[^a-z0-9]', '', host.lower().split('.')[0])
        sname = re.sub(r'[^a-z0-9]', '', name.lower().replace(' ',''))
        pkg = f'com.{safe or "app"}.{sname or "app"}'

    job_id = str(uuid.uuid4())[:12]
    config = {'url': url, 'name': name, 'package': pkg,
              'version_name': data.get('version', '1.0.0')}
    jobs[job_id] = {'status':'pending','progress':0,'message':'Starting...','files':[],'error':None,'created':time.time()}
    threading.Thread(target=run_build, args=(job_id, config, platforms), daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/api/status/<job_id>')
def api_status(job_id):
    job = jobs.get(job_id)
    if not job: return jsonify({'error': 'Not found'}), 404
    return jsonify({'status':job['status'],'progress':job['progress'],
                    'message':job['message'],'files':job.get('files',[]),'error':job.get('error')})

@app.route('/api/download/<job_id>/<filename>')
def api_download(job_id, filename):
    p = BUILDS_DIR / job_id / Path(filename).name
    if not p.exists(): return jsonify({'error': 'Not found'}), 404
    mime = {'.apk': 'application/vnd.android.package-archive',
            '.exe': 'application/octet-stream',
            '.AppImage': 'application/octet-stream'}.get(p.suffix, 'application/octet-stream')
    return send_file(p, as_attachment=True, download_name=p.name, mimetype=mime)

@app.route('/health')
def health():
    return jsonify({'status':'ok','token_set':bool(GITHUB_TOKEN),
                    'repo':f'{GITHUB_OWNER}/{GITHUB_REPO}'})

def _cleanup():
    while True:
        time.sleep(300)
        cutoff = time.time() - 3600
        for d in BUILDS_DIR.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
threading.Thread(target=_cleanup, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f'AppForge — {GITHUB_OWNER}/{GITHUB_REPO} — token: {bool(GITHUB_TOKEN)}')
    app.run(host='0.0.0.0', port=port, debug=False)
