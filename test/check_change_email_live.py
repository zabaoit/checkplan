"""Inspect live GSH jobs without printing account data; verify deployed mail UI."""
import argparse
import collections
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from web.auth import get_token

parser = argparse.ArgumentParser()
parser.add_argument('--verify', action='store_true')
args = parser.parse_args()
base = 'http://127.0.0.1:8085'
headers = {'X-API-Token': get_token()}
active = 0
for path in ('/api/jobs', '/api/session/jobs', '/api/link/jobs', '/api/upi/jobs', '/api/change-password/jobs'):
    request = urllib.request.Request(base + path, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.load(response)
    jobs = data.get('jobs', [])
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    counts = collections.Counter(job.get('status') for job in jobs)
    active += sum(counts.get(status, 0) for status in ('queued', 'running', 'pending'))
    print(path, dict(counts))
    # Preserve existing in-memory results before a server reload, without stdout secrets.
    if jobs and path in ('/api/upi/jobs', '/api/change-password/jobs'):
        details = []
        for job in jobs:
            req = urllib.request.Request(base + path + '/' + job['id'], headers=headers)
            with urllib.request.urlopen(req, timeout=15) as response:
                details.append(json.load(response))
        backup = ROOT / 'test' / ('saved_before_mail_reload_' + path.split('/')[2] + '.json')
        if not backup.exists():
            backup.write_text(json.dumps(details, ensure_ascii=False), encoding='utf-8')
        print('Saved existing results:', len(details))
print('ACTIVE_JOBS', active)
if args.verify:
    with urllib.request.urlopen(base + '/', timeout=15) as response:
        html = response.read().decode()
    assert 'value="change_email"' in html
    assert 'chpwd-congas' in html
    with urllib.request.urlopen(base + '/openapi.json', timeout=15) as response:
        schema = json.load(response)
    fields = schema['components']['schemas']['AddChangePasswordJobsRequest']['properties']
    assert 'change_email_only' in fields and 'congas_mailboxes' in fields
    print('LIVE_MAIL_UI_AND_API_OK')
