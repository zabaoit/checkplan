"""Offline API and legacy-mode regression; no account requests."""
import ast
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for name in ('web/change_password_manager.py','web/server.py','web/change_email_jobs.py',
             'web/email_change_state.py','email_change.py','congas_mail.py','session_phase.py'):
    ast.parse((ROOT/name).read_text(encoding='utf-8-sig'), filename=name)
with tempfile.TemporaryDirectory(prefix='mail_api_', dir=ROOT/'test') as temp:
    os.environ['GSH_DB_PATH'] = str(Path(temp)/'check.db')
    from db.engine import DatabaseEngine
    from db.repositories import JobRepository
    from web.change_password_manager import ChangePasswordJobManager
    from web.auth import reset_token_for_tests
    from web.server import app
    from fastapi.testclient import TestClient
    engine = DatabaseEngine(db_path=os.environ['GSH_DB_PATH'])
    manager = ChangePasswordJobManager()
    manager._email_repo = JobRepository(engine)
    manager._ensure_workers = lambda: None
    reset_token_for_tests('mail-api-fixture')
    client = TestClient(app, headers={'X-API-Token':'mail-api-fixture'})
    secret='JBSWY3DPEHPK3PXP'
    try:
        with patch('web.server.get_change_password_manager', return_value=manager):
            for index, (mode, flag) in enumerate((('change_password', None), ('change_2fa','change_2fa_only'),
                ('session_only','skip_password_change'), ('check_plan','check_plan_only'), ('change_email','change_email_only'))):
                body={'combos':f'user{index}@example.com| old password |{secret}', 'new_password':'new-password'}
                if flag:
                    body[flag]=True
                if mode == 'change_email':
                    body['congas_mailboxes']='base@gmail.com|ABCD1234TOKEN'
                response=client.post('/api/change-password/jobs',json=body)
                assert response.status_code == 200, (mode,response.status_code)
                summary=response.json()['jobs'][0]
                assert summary['mode'] == mode
                assert 'ABCD1234TOKEN' not in response.text
                if mode == 'change_email':
                    job=manager.jobs[summary['id']]
                    assert job.new_password is None and job.current_password==' old password '
                    assert len(job.target_email.split('+')[1].split('@')[0]) == 3
                    full=client.get('/api/change-password/jobs/'+job.id).json()
                    assert full['secret']==secret and full['current_password']==' old password '
            before=len(manager.jobs)
            bad=client.post('/api/change-password/jobs',json={'combos':f'x@example.com|p|{secret}',
                'change_email_only':True,'change_2fa_only':True,'congas_mailboxes':'base@gmail.com|ABCD1234TOKEN'})
            assert bad.status_code == 400 and len(manager.jobs)==before
            bad=client.post('/api/change-password/jobs',json={'combos':f'x@example.com|p|{secret}',
                'change_email_only':True,'congas_mailboxes':''})
            assert bad.status_code == 400 and len(manager.jobs)==before
            bad=client.post('/api/change-password/jobs',json={'combos':'x@example.com|p|',
                'change_email_only':True,'congas_mailboxes':'base@gmail.com|ABCD1234TOKEN'})
            assert bad.status_code == 400 and len(manager.jobs)==before
        print('API regression OK: all 5 modes, atomic rejection, raw credentials, safe summary')
    finally:
        client.close()
        engine.close()
