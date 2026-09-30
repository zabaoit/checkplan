import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import urllib.request, json
from db import get_engine
from web.auth import get_token

token = get_token()
req = urllib.request.Request(
    'http://127.0.0.1:8085/api/change-password/config',
    headers={'X-API-Token': token}
)
with urllib.request.urlopen(req) as resp:
    print('Chpwd config:', json.loads(resp.read().decode('utf-8')))

req2 = urllib.request.Request(
    'http://127.0.0.1:8085/api/proxy/twoproxy',
    headers={'X-API-Token': token}
)
with urllib.request.urlopen(req2) as resp:
    print('2Proxy status:', json.loads(resp.read().decode('utf-8')))
