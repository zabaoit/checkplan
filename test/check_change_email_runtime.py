"""Read-only deployment check: counts and browser mode, never credentials."""
import collections
import json
import sys
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web.auth import get_token
from config import load_settings
base='http://127.0.0.1:8085'
headers={'X-API-Token':get_token()}
active=0
for path in ('/api/jobs','/api/session/jobs','/api/link/jobs','/api/upi/jobs','/api/change-password/jobs'):
    request=urllib.request.Request(base+path,headers=headers)
    with urllib.request.urlopen(request,timeout=15) as response:
        jobs=json.load(response).get('jobs',[])
    if isinstance(jobs,dict):
        jobs=list(jobs.values())
    counts=collections.Counter(job.get('status') for job in jobs)
    active+=sum(counts.get(status,0) for status in ('queued','running','pending'))
    print(path,dict(counts))
    if path.endswith('change-password/jobs'):
        print('Mail stages',dict(collections.Counter(job.get('email_change_stage') for job in jobs if job.get('mode')=='change_email')))
print('ACTIVE_JOBS',active)
print('EMAIL_BROWSER_HEADLESS',load_settings().browser_headless)
if active: raise SystemExit(2)
