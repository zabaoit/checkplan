"""Offline manager/SQLite integration; never calls remote services."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from db.engine import DatabaseEngine
from db.repositories import JobRepository
from email_change import EmailChangeError
from web.change_password_manager import ChangePasswordJobManager
from web.change_email_jobs import recover_email_jobs, save_email_job

SECRET = 'JBSW Y3DP EHPK 3PXP'
BASES = '\n'.join(f'base{i}@gmail.com|ABCD1234' for i in range(50))
LINES = [f'source{i}@example.com| old password |{SECRET}' for i in range(100)]


def manager(repo):
    instance = ChangePasswordJobManager(max_concurrent=2)
    instance._email_repo = repo
    instance._ensure_workers = lambda: None
    async def no_proxy(job):
        return None
    instance._begin_job_proxy = no_proxy
    return instance


def state(repo, job):
    row = next(row for row in repo.list_all() if row['id'] == job.id)
    return row, json.loads(row['session_data'])


def check_atomic(repo):
    m = manager(repo)
    try:
        m.add_jobs([LINES[0], 'invalid'], change_email_only=True, congas_mailboxes=BASES)
    except ValueError:
        pass
    else:
        raise AssertionError('invalid accepted')
    assert not repo.list_all() and not m.jobs and not m.order
    original = repo.create
    calls = 0
    def fail_second(data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('fixture storage failure')
        return original(data)
    with patch.object(repo, 'create', fail_second):
        try:
            m.add_jobs(LINES[:2], change_email_only=True, congas_mailboxes=BASES)
        except RuntimeError:
            pass
        else:
            raise AssertionError('storage failure swallowed')
    assert not repo.list_all() and not m.jobs and not m.order
    jobs = m.add_jobs(LINES, change_email_only=True, congas_mailboxes=BASES)
    assert len(jobs) == len(repo.list_all()) == 100
    assert jobs[0].mailbox_email == jobs[50].mailbox_email
    assert len({job.target_email for job in jobs}) == 100
    for job in jobs:
        row, saved = state(repo, job)
        assert saved['target_email'] == job.target_email and saved['stage'] == 'prepared'
        assert row['combo'] == '[redacted]'
        assert 'mailbox_key' not in saved and 'initial_session' not in saved
        assert 'ABCD1234' not in row['session_data']
    return m, jobs


async def check_serialization(m, jobs):
    first, second = jobs[:2]
    second.mailbox_email = 'b.ase0@gmail.com'
    active = maximum = 0
    async def fake(**kw):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(.02)
        active -= 1
        return {'user': {'id':'fixture-id', 'email':kw['target_email']}, 'new_secret':'must-not-replace'}
    with patch('email_change.change_email', fake):
        await asyncio.gather(m._run_job(first), m._run_job(second))
    assert maximum == 1, 'same canonical mailbox flows overlapped'
    assert first.status == second.status == 'success'
    full = m.get_job(first.id)
    assert full['current_password'] == ' old password ' and full['secret'] == SECRET


async def check_pending_and_recovery(m, repo, job):
    job.initial_session = '{"sessionToken":"fixture-never-persist-source-token"}'
    alias = job.target_email
    async def mutate_then_fail(**kw):
        assert kw['stage'] == 'prepared'
        await kw['checkpoint']('submitted', 'fixture-user')
        row, saved = state(repo, job)
        assert saved['stage'] == 'submitted' and saved['user_id'] == 'fixture-user'
        assert 'fixture-never-persist-source-token' not in row['session_data']
        # Simulated remote mutation starts only after durable checkpoint.
        await kw['checkpoint']('changed_pending_verify', 'fixture-user')
        raise EmailChangeError('fixture verification unavailable', pending=True)
    with patch('email_change.change_email', mutate_then_fail):
        await m._run_job(job)
    assert job.status == 'error' and job.email_change_stage == 'changed_pending_verify'
    assert not m.remove_job(job.id)
    m.clear_finished()
    assert job.id in m.jobs
    restarted = manager(repo)
    recover_email_jobs(restarted)
    recovered = restarted.jobs[job.id]
    assert recovered.id == job.id and recovered.target_email == alias
    assert recovered.email_change_stage == 'changed_pending_verify'
    assert not recovered.mailbox_key and not recovered.initial_session
    assert await restarted.retry_job(job.id)
    assert recovered.target_email == alias and recovered.email_change_stage == 'changed_pending_verify'
    async def verify_only(**kw):
        assert kw['stage'] == 'changed_pending_verify'
        assert kw['target_email'] == alias and kw['expected_user_id'] == 'fixture-user'
        assert not kw['mailbox_key'] and kw.get('initial_session') is None
        assert kw['password'] == ' old password ' and kw['secret'] == SECRET
        await kw['checkpoint']('verified', 'fixture-user')
        return {'user': {'email':alias, 'id':'fixture-user'}}
    with patch('email_change.change_email', verify_only):
        await restarted._run_job(recovered)
    assert recovered.status == 'success' and recovered.email_change_stage == 'verified'
    return restarted


async def check_verified_crash(m, repo, job):
    job.email_change_stage = 'verified'
    job.original_user_id = 'fixture-user'
    job.status = 'error'
    save_email_job(m, job)
    restarted = manager(repo)
    recover_email_jobs(restarted)
    recovered = restarted.jobs[job.id]
    assert recovered.email_change_stage == 'verified' and recovered.status == 'error'
    assert not restarted.remove_job(job.id)
    restarted.clear_finished()
    assert job.id in restarted.jobs
    assert await restarted.retry_job(job.id)
    async def verify_again(**kw):
        assert kw['stage'] == 'verified' and kw['expected_user_id'] == 'fixture-user'
        return {'user': {'email':kw['target_email'], 'id':'fixture-user'}}
    with patch('email_change.change_email', verify_again):
        await restarted._run_job(recovered)
    assert recovered.status == 'success'


async def main():
    with tempfile.TemporaryDirectory(prefix='email-manager-', dir=ROOT/'test') as directory:
        engine = DatabaseEngine(Path(directory)/'fixture.db')
        try:
            repo = JobRepository(engine)
            m, jobs = check_atomic(repo)
            await check_serialization(m, jobs)
            await check_pending_and_recovery(m, repo, jobs[2])
            await check_verified_crash(m, repo, jobs[3])
        finally:
            engine.close()
    print('Email manager offline integration OK: atomic100/50, locks, checkpoints, recovery, verify-only retry')


if __name__ == '__main__':
    asyncio.run(main())
