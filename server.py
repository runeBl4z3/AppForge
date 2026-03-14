"""
AppForge Backend — GitHub Actions Build Engine
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

def trigger_workflow(job_id, app_name, package_name, website_url, version_name='1.0.0'):
    url = f'{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches'
    r = requests.post(url, headers=gh_headers(), json={
        'ref': 'main',
        'inputs': {'app_name': app_name, 'package_name': package_name,
                   'website_url': website_url, 'version_name': version_name,
                   'job_id': job_id}
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

def wait_for_run(run_id, job_id, max_minutes=12):
    url = f'{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs/{run_id}'
    deadline = time.time() + max_minutes * 60
    while time.time() < deadline:
        time.sleep(10)
        r = requests.get(url, headers=gh_headers(), timeout=30)
        if r.status_code != 200:
            continue
        run = r.json()
        status = run.get('status', '')
        conclusion = run.get('conclusion')
        pct = {'queued': 20, 'in_progress': 55, 'completed': 92}.get(status, 30)
        jobs[job_id].update({'progress': pct, 'message': f'GitHub Actions: {status}...'})
        if status == 'completed':
            return conclusion == 'success', conclusion
    return False, 'timeout'

def download_artifact(run_id, job_id, out_dir):
    url = f'{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/runs/{run_id}/artifacts'
    r = requests.get(url, headers=gh_headers(), timeout=30)
    if r.status_code != 200:
        return None
    for artifact in r.json().get('artifacts', []):
        if artifact['name'].startswith('apk-'):
            r2 = requests.get(artifact['archive_download_url'], headers=gh_headers(),
                              stream=True, timeout=120)
            if r2.status_code == 200:
                zp = out_dir / 'artifact.zip'
                with open(zp, 'wb') as f:
                    for chunk in r2.iter_content(8192):
                        f.write(chunk)
                with zipfile.ZipFile(zp) as zf:
                    zf.extractall(out_dir)
                zp.unlink()
                apks = list(out_dir.glob('*.apk'))
                return apks[0] if apks else None
    return None

def run_build(job_id, config):
    try:
        out_dir = BUILDS_DIR / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        jobs[job_id].update({'status': 'running', 'progress': 5, 'message': 'Triggering GitHub Actions build...'})

        if not GITHUB_TOKEN:
            raise RuntimeError('GITHUB_TOKEN not set. Add it in Railway → Variables.')

        import datetime
        before = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

        if not trigger_workflow(job_id, config['name'], config['package'], config['url'], config.get('version_name','1.0.0')):
            raise RuntimeError('Failed to trigger GitHub Actions. Check GITHUB_TOKEN has actions:write + repo scope.')

        jobs[job_id].update({'progress': 10, 'message': 'Queued on GitHub Actions, waiting to start...'})

        run_id = find_workflow_run(before)
        if not run_id:
            raise RuntimeError('Could not find GitHub Actions run — workflow may have failed to start.')

        jobs[job_id].update({'progress': 18, 'message': f'Run #{run_id} building with real Android SDK...'})

        success, conclusion = wait_for_run(run_id, job_id)
        if not success:
            raise RuntimeError(f'Build {conclusion}. See https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/actions')

        jobs[job_id].update({'progress': 93, 'message': 'Build done! Downloading APK...'})

        apk_path = download_artifact(run_id, job_id, out_dir)
        if not apk_path:
            raise RuntimeError('Could not download APK artifact from GitHub Actions.')

        final_apk = out_dir / f"{config['name'].replace(' ','_')}.apk"
        apk_path.rename(final_apk)
        kb = final_apk.stat().st_size // 1024

        jobs[job_id].update({
            'status': 'done', 'progress': 100,
            'message': f'APK ready! ({kb} KB) — built with real Android SDK',
            'files': [{'name': final_apk.name, 'label': f'Android APK ({kb} KB)',
                       'platform': 'android', 'url': f'/api/download/{job_id}/{final_apk.name}',
                       'icon': '🤖'}]
        })
    except Exception as e:
        jobs[job_id].update({'status': 'error', 'progress': 0, 'message': str(e), 'error': str(e)})

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
    if not url or not url.startswith('http'):
        return jsonify({'error': 'Invalid URL'}), 400
    if not pkg:
        import re
        host = url.replace('https://','').replace('http://','').replace('www.','').split('/')[0]
        safe = re.sub(r'[^a-z0-9]', '', host.lower().split('.')[0])
        sname = re.sub(r'[^a-z0-9]', '', name.lower().replace(' ',''))
        pkg = f'com.{safe or "app"}.{sname or "app"}'
    job_id = str(uuid.uuid4())[:12]
    config = {'url': url, 'name': name, 'package': pkg, 'version_name': data.get('version','1.0.0')}
    jobs[job_id] = {'status':'pending','progress':0,'message':'Starting...','files':[],'error':None,'created':time.time()}
    threading.Thread(target=run_build, args=(job_id, config), daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/api/status/<job_id>')
def api_status(job_id):
    job = jobs.get(job_id)
    if not job: return jsonify({'error': 'Not found'}), 404
    return jsonify({'status':job['status'],'progress':job['progress'],'message':job['message'],'files':job.get('files',[]),'error':job.get('error')})

@app.route('/api/download/<job_id>/<filename>')
def api_download(job_id, filename):
    p = BUILDS_DIR / job_id / Path(filename).name
    if not p.exists(): return jsonify({'error': 'Not found'}), 404
    return send_file(p, as_attachment=True, download_name=p.name, mimetype='application/vnd.android.package-archive')

@app.route('/health')
def health():
    return jsonify({'status':'ok','token_set':bool(GITHUB_TOKEN),'repo':f'{GITHUB_OWNER}/{GITHUB_REPO}'})

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
    print(f'AppForge server — {GITHUB_OWNER}/{GITHUB_REPO} — token: {bool(GITHUB_TOKEN)}')
    app.run(host='0.0.0.0', port=port, debug=False)
