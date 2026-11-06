"""Read live concurrency and anonymous mailbox groups; no credentials printed."""
import collections
import json
import sys
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web.auth import get_token
headers={'X-API-Token':get_token()}
def fetch(path):
    request=urllib.request.Request('http://127.0.0.1:8085'+path,headers=headers)
    with urllib.request.urlopen(request,timeout=15) as response:
        return json.load(response)
print('CHPWD_CONFIG',fetch('/api/change-password/config'))
print('GLOBAL_MODE',fetch('/api/config').get('mode'))
data=fetch('/api/change-password/jobs')
jobs=data['jobs']
print('STATUS_COUNTS',dict(collections.Counter(job['status'] for job in jobs)))
groups={}
for job in jobs[-5:]:
    target=job.get('target_email') or ''
    if '@' not in target:
        continue
    local,domain=target.casefold().rsplit('@',1)
    base=local.split('+',1)[0].replace('.','')+'@'+domain
    group=groups.setdefault(base,len(groups)+1)
    print('RECENT_JOB',{'mailbox_group':group,'status':job['status'],
        'stage':job.get('email_change_stage'),'duration':round(job.get('duration') or 0,1),
        'created_at':job.get('created_at'),'started_at':job.get('started_at')})
