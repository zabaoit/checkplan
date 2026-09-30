import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_engine, get_settings_repo
from web.twoproxy_pool import get_twoproxy_pool

repo = get_settings_repo(get_engine())
repo.set('proxy.twoproxy_enabled', True)
repo.set('chpwd.use_proxy', True)
get_twoproxy_pool().configure(enabled=True, keys=repo.get('proxy.twoproxy_keys'), live_seconds=repo.get('proxy.twoproxy_live_seconds'))
print('Updated proxy.twoproxy_enabled:', repo.get('proxy.twoproxy_enabled'))
print('TwoProxy is_active:', get_twoproxy_pool().is_active())
