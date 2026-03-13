"""
AppForge Flask Backend
======================
Connects the website frontend to build.py.
Handles build requests, runs the builder, and serves download links.

Run locally:   python server.py
Deploy:        Railway / Render / Heroku (see README)
"""

import os
import sys
import uuid
import json
import shutil
import threading
import time
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string, send_from_directory

# ── import our builder ──────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
import build as builder

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max

# ── directories ─────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
BUILDS_DIR = BASE_DIR / 'builds'
BUILDS_DIR.mkdir(exist_ok=True)

# ── in-memory job tracker ────────────────────────────────────────────
jobs = {}   # job_id -> { status, progress, message, files, error }

# ── auto-cleanup: delete builds older than 1 hour ───────────────────
def cleanup_old_builds():
    while True:
        time.sleep(300)  # check every 5 minutes
        now = time.time()
        for job_dir in BUILDS_DIR.iterdir():
            if job_dir.is_dir():
                age = now - job_dir.stat().st_mtime
                if age > 3600:  # 1 hour
                    shutil.rmtree(job_dir, ignore_errors=True)
                    job_id = job_dir.name
                    jobs.pop(job_id, None)

threading.Thread(target=cleanup_old_builds, daemon=True).start()


# ════════════════════════════════════════════════════════════════════
#  ROUTES
# ════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    """Serve the main website."""
    html_path = BASE_DIR / 'appbuilder.html'
    if html_path.exists():
        return html_path.read_text(encoding='utf-8')
    return '<h1>AppForge</h1><p>Place appbuilder.html next to server.py</p>', 200


@app.route('/api/build', methods=['POST'])
def start_build():
    """
    Start a new app build job.
    
    POST body (JSON):
        url          required   Website URL to wrap
        name         required   App name
        package      optional   Bundle ID (auto-generated if blank)
        version      optional   Version name  (default: 1.0.0)
        vcode        optional   Version code  (default: 1)
        minsdk       optional   Android min SDK (default: 21)
        targetsdk    optional   Android target SDK (default: 34)
        platforms    optional   Comma-separated: android,windows,ios,linux,all (default: all)
    
    Returns:
        { job_id, status }
    """
    data = request.get_json(force=True, silent=True) or {}

    # ── validate ────────────────────────────────────────────────────
    url  = (data.get('url') or '').strip()
    name = (data.get('name') or '').strip()

    if not url:
        return jsonify({'error': 'url is required'}), 400
    if not name:
        return jsonify({'error': 'name is required'}), 400
    if not url.startswith('http'):
        url = 'https://' + url

    # ── auto package name ────────────────────────────────────────────
    import re
    package = (data.get('package') or '').strip()
    if not package:
        host = url.replace('https://','').replace('http://','').replace('www.','').split('/')[0]
        safe_host = re.sub(r'[^a-z0-9]', '', host.lower().split('.')[0]) or 'app'
        safe_name = re.sub(r'[^a-z0-9]', '', name.lower().replace(' ','')) or 'myapp'
        package = f'com.{safe_host}.{safe_name}'

    platforms_raw = data.get('platforms', 'all')
    platforms = [p.strip().lower() for p in platforms_raw.split(',')]
    if 'all' in platforms:
        platforms = ['android', 'windows', 'ios', 'linux']

    config = {
        'url':          url,
        'name':         name,
        'package':      package,
        'version_name': data.get('version', '1.0.0'),
        'version_code': int(data.get('vcode', 1)),
        'min_sdk':      int(data.get('minsdk', 21)),
        'target_sdk':   int(data.get('targetsdk', 34)),
    }

    # ── create job ──────────────────────────────────────────────────
    job_id  = str(uuid.uuid4())
    out_dir = BUILDS_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs[job_id] = {
        'status':   'queued',
        'progress': 0,
        'message':  'Build queued...',
        'files':    [],
        'error':    None,
        'config':   config,
    }

    # ── run build in background thread ──────────────────────────────
    t = threading.Thread(
        target=run_build,
        args=(job_id, config, platforms, out_dir),
        daemon=True
    )
    t.start()

    return jsonify({'job_id': job_id, 'status': 'queued'}), 202


def run_build(job_id, config, platforms, out_dir):
    """Background worker: runs the builder and updates job status."""
    job = jobs[job_id]

    try:
        built_files = []
        total = len(platforms) + 1  # +1 for master zip
        step  = 0

        def update(msg, pct=None):
            job['message']  = msg
            job['progress'] = pct if pct is not None else job['progress']

        job['status'] = 'building'
        update('Starting build...', 5)

        if 'android' in platforms:
            update('Building Android APK...', 15)
            apk, proj = builder.build_apk(config, out_dir)
            built_files.append(('Android APK',              apk))
            built_files.append(('Android Studio Project',   proj))
            step += 1
            update('Android ✓', 15 + step * 18)

        if 'windows' in platforms:
            update('Building Windows package...', 15 + step * 18)
            win = builder.build_windows(config, out_dir)
            built_files.append(('Windows Electron Project', win))
            step += 1
            update('Windows ✓', 15 + step * 18)

        if 'ios' in platforms:
            update('Building iOS package...', 15 + step * 18)
            ios = builder.build_ios(config, out_dir)
            built_files.append(('iOS Xcode Project',        ios))
            step += 1
            update('iOS ✓', 15 + step * 18)

        if 'linux' in platforms:
            update('Building Linux package...', 15 + step * 18)
            lnx = builder.build_linux(config, out_dir)
            built_files.append(('Linux Project',            lnx))
            step += 1
            update('Linux ✓', 15 + step * 18)

        update('Bundling all platforms...', 90)
        master = builder.build_master_zip(config, out_dir, built_files)
        built_files.append(('All Platforms ZIP', master))

        # Build file list for frontend
        file_list = []
        for label, path in built_files:
            if path and path.exists():
                file_list.append({
                    'label':    label,
                    'filename': path.name,
                    'size_kb':  round(path.stat().st_size / 1024, 1),
                    'url':      f'/api/download/{job_id}/{path.name}',
                })

        job['status']   = 'done'
        job['progress'] = 100
        job['message']  = 'Build complete!'
        job['files']    = file_list

    except Exception as e:
        import traceback
        job['status']  = 'error'
        job['error']   = str(e)
        job['message'] = f'Build failed: {e}'
        traceback.print_exc()


@app.route('/api/status/<job_id>')
def job_status(job_id):
    """Poll this endpoint to track build progress."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({
        'job_id':   job_id,
        'status':   job['status'],
        'progress': job['progress'],
        'message':  job['message'],
        'files':    job['files'],
        'error':    job['error'],
    })


@app.route('/api/download/<job_id>/<filename>')
def download_file(job_id, filename):
    """Download a built file."""
    # Security: only allow alphanumeric job IDs and safe filenames
    import re
    if not re.match(r'^[a-f0-9\-]{36}$', job_id):
        return jsonify({'error': 'Invalid job ID'}), 400
    if not re.match(r'^[\w\-. ]+$', filename):
        return jsonify({'error': 'Invalid filename'}), 400

    file_path = BUILDS_DIR / job_id / filename
    if not file_path.exists():
        return jsonify({'error': 'File not found'}), 404

    return send_file(
        file_path,
        as_attachment=True,
        download_name=filename
    )


@app.route('/api/jobs')
def list_jobs():
    """List all active jobs (for debugging)."""
    return jsonify({
        jid: {
            'status':   j['status'],
            'progress': j['progress'],
            'message':  j['message'],
            'app':      j.get('config', {}).get('name', '?'),
        }
        for jid, j in jobs.items()
    })


@app.route('/health')
def health():
    """Health check endpoint for Railway/Render."""
    return jsonify({'status': 'ok', 'service': 'AppForge Builder API'})


# ════════════════════════════════════════════════════════════════════
#  START
# ════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'false').lower() == 'true'
    print(f'''
╔══════════════════════════════════════════════════╗
║        AppForge Backend Server                   ║
╠══════════════════════════════════════════════════╣
║  Local:   http://localhost:{port:<22}║
║  Health:  http://localhost:{port}/health         ║
║                                                  ║
║  Endpoints:                                      ║
║    POST /api/build       Start a build           ║
║    GET  /api/status/:id  Check build progress    ║
║    GET  /api/download/:id/:file  Download file   ║
╚══════════════════════════════════════════════════╝
''')
    app.run(host='0.0.0.0', port=port, debug=debug, threaded=True)
