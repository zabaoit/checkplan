"""Offline contract: full source login must complete before Account UI opens."""
import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import email_change

SOURCE = 'fixture@example.com'
TARGET = 'fixture+abc@gmail.com'
PASSWORD = ' original password '
SECRET = 'JBSW Y3DP EHPK 3PXP'
SESSION = {'user': {'email': SOURCE, 'id': 'stable-id'}, '__mfa_verified': True,
           '__cookies': [{'name': '__Secure-next-auth.session-token', 'value': 'offline-cookie'}]}
FRESH = {'user': {'email': TARGET, 'id': 'stable-id'}, '__mfa_verified': True}
PHASES = ('[request] [0/9] Priming chatgpt.com session (GET /auth/login)...',
          '[request] [1/9] Fetching CSRF token...', '[request] [2/8] Getting authorize URL...',
          '[session-req] password verified', '[session-req] MFA verified!',
          '[request] [9/9] Following redirect chain...', '[session-req] GET /api/auth/session',
          '[session-req] done')


class Mailbox:
    entered = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        type(self).entered += 1
        return self

    async def __aexit__(self, *args):
        pass


async def scenario(*, source=SESSION, fresh=FRESH, stage='prepared', checkpoint_error='',
                   browser_error=False, login_error=False):
    stages, mutations, events, logs = [], [], [], []

    async def login_flow(**kwargs):
        account = kwargs['email']
        assert kwargs['password'] == PASSWORD and kwargs['secret'] == SECRET
        events.append('login:' + account)
        if login_error:
            raise RuntimeError('offline password/MFA failure')
        for phase in PHASES:
            kwargs['log'](phase)
        await asyncio.sleep(0)
        events.append('done:' + account)
        return source if account == SOURCE else fresh

    login = AsyncMock(side_effect=login_flow)

    async def checkpoint(next_stage, identity):
        stages.append((next_stage, identity))
        if next_stage == checkpoint_error:
            raise OSError('offline storage failure')

    async def browser(**kwargs):
        assert kwargs['session'] is source
        assert events[-1] == 'done:' + SOURCE, 'Account opened before source login completed'
        assert any('Get Session hoàn tất' in line for line in logs)
        events.append('browser')
        await kwargs['save']('submitted', 'stable-id')
        mutations.append('fake mutation')
        if browser_error:
            raise RuntimeError('offline browser failure')
        await kwargs['save']('changed_pending_verify', 'stable-id')

    browser_mock = AsyncMock(side_effect=browser)
    Mailbox.entered = 0
    with patch.dict(sys.modules, {
        'session_phase': types.SimpleNamespace(get_session_pure_request=login),
        'congas_mail': types.SimpleNamespace(CongasMailbox=Mailbox),
    }), patch.object(email_change, '_change_in_browser', browser_mock):
        try:
            result = await email_change.change_email(
                email=SOURCE, password=PASSWORD, secret=SECRET, target_email=TARGET,
                mailbox_email='fixture@gmail.com', mailbox_key='OFFLINEKEY', stage=stage,
                expected_user_id='stable-id' if stage != 'prepared' else '',
                checkpoint=checkpoint, log=logs.append)
            error = None
        except email_change.EmailChangeError as exc:
            result, error = None, exc
    return types.SimpleNamespace(result=result, error=error, login=login, browser=browser_mock,
        stages=stages, mutations=mutations, entered=Mailbox.entered, events=events, logs=logs)


async def main():
    case = await scenario()
    assert case.error is None and case.result == {
        'email': TARGET, 'secret': SECRET, 'login_verified': True, 'user_id': 'stable-id'}
    assert case.events == ['login:' + SOURCE, 'done:' + SOURCE, 'browser', 'login:' + TARGET, 'done:' + TARGET]
    assert case.login.await_count == 2 and case.browser.await_count == 1
    assert case.entered == 1 and len(case.mutations) == 1
    assert [stage for stage, _ in case.stages] == ['prepared','submitted','changed_pending_verify','verified']
    assert all(phase in case.logs for phase in PHASES)
    assert not any(PASSWORD in line or SECRET in line or 'offline-cookie' in line for line in case.logs)

    for source in (
        {**SESSION, 'user': {'email': TARGET, 'id': 'stable-id'}},
        {**SESSION, '__mfa_verified': False},
        {'user': SESSION['user'], '__cookies': SESSION['__cookies']},
        {'user': SESSION['user'], '__mfa_verified': True},
    ):
        case = await scenario(source=source)
        assert case.result is None and case.error.error_kind == 'email_change_failed'
        assert case.login.await_count == 1 and case.entered == 0 and not case.mutations
        case.browser.assert_not_awaited()
    case = await scenario(login_error=True)
    assert case.error and case.login.await_count == 1 and case.entered == 0
    case.browser.assert_not_awaited()

    for fresh in (
        {**FRESH, 'user': {'email': TARGET, 'id': 'wrong-id'}},
        {**FRESH, 'user': {'email': SOURCE, 'id': 'stable-id'}},
        {**FRESH, '__mfa_verified': False}, {'user': FRESH['user']},
    ):
        case = await scenario(fresh=fresh)
        assert case.result is None and case.error.error_kind == 'email_verification_pending'
        assert not any(stage == 'verified' for stage, _ in case.stages)

    for stage in ('submitted','changed_pending_verify','verified'):
        case = await scenario(stage=stage)
        assert case.error is None and case.result['login_verified']
        assert case.events == ['login:' + TARGET, 'done:' + TARGET]
        case.browser.assert_not_awaited()
        assert case.entered == 0 and not case.mutations and case.stages == [('verified','stable-id')]

    for checkpoint_error, kind in (('prepared','email_change_failed'),('submitted','email_verification_pending')):
        case = await scenario(checkpoint_error=checkpoint_error)
        assert case.result is None and case.error.error_kind == kind
        assert not case.mutations and case.login.await_count == 1
    case = await scenario(browser_error=True)
    assert case.error.error_kind == 'email_verification_pending' and len(case.mutations) == 1
    assert case.login.await_count == 1
    print('PASS login order: source password/TOTP/session before Account; logs forwarded; failures block UI; pending retry verify-only')


asyncio.run(main())
