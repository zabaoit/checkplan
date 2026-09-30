import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_engine, get_settings_repo
from web.twoproxy_pool import get_twoproxy_pool
from web.change_password_manager import get_change_password_manager

repo = get_settings_repo(get_engine())
print('DB proxy.twoproxy_enabled:', repo.get('proxy.twoproxy_enabled'))
print('DB proxy.twoproxy_keys:', repo.get('proxy.twoproxy_keys'))
print('DB chpwd.use_proxy:', repo.get('chpwd.use_proxy'))

cm = get_change_password_manager()
print('Manager cm.use_proxy:', cm.use_proxy)
print('TwoProxy is_active:', get_twoproxy_pool().is_active())
