import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import urllib.request
import json
from db import get_engine, get_settings_repo
from web.auth import get_token

repo = get_settings_repo(get_engine())
token = get_token()
print('Testing with token:', token[:6] + '...')

data = json.dumps({'keys': ['tf2MlarE09Mi17GxvX22'], 'live_seconds': 120}).encode('utf-8')
req = urllib.request.Request(
    'http://127.0.0.1:8085/api/proxy/twoproxy/test',
    data=data,
    headers={
        'Content-Type': 'application/json',
        'X-API-Token': token,
    }
)
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        res = json.loads(resp.read().decode('utf-8'))
        print('[RESPONSE]', json.dumps(res, ensure_ascii=False, indent=2))
except Exception as e:
    print('[ERROR]', e)
