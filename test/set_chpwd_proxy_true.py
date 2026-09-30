import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_engine, get_settings_repo
repo = get_settings_repo(get_engine())
repo.set('chpwd.use_proxy', True)
print('Updated chpwd.use_proxy:', repo.get('chpwd.use_proxy'))
