"""Offline atomic batch allocation check."""
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web.email_change_state import prepare_email_jobs


def rejected(lines, bases, jobs=()):
    try:
        prepare_email_jobs(lines, bases, jobs)
    except ValueError:
        return
    raise AssertionError('invalid batch accepted')


def main():
    lines = [f'user{i}@example.com| password |JBSW Y3DP EHPK 3PXP' for i in range(100)]
    bases = '\n'.join(f'base{i}@gmail.com|ABCD1234' for i in range(50))
    allocated = prepare_email_jobs(lines, bases, {})
    assert len(allocated) == 100 and len({j['target_email'] for j in allocated}) == 100
    assert allocated[0]['mailbox_email'] == allocated[50]['mailbox_email']
    assert allocated[0]['current_password'] == ' password '
    assert allocated[0]['secret'] == 'JBSW Y3DP EHPK 3PXP'
    assert all(j['change_email_only'] for j in allocated)
    assert len(prepare_email_jobs(lines[:50], 'one@gmail.com|ABCD1234', {})) == 50
    with patch('web.email_change_state.secrets.randbelow', return_value=0):
        allocated = prepare_email_jobs(lines[:2], 'one@gmail.com|ABCD1234',
            [{'email':'old@example.com','target_email':'one+aaa@gmail.com','status':'success','change_email_only':True}])
    assert [j['target_email'] for j in allocated] == ['one+aab@gmail.com','one+aac@gmail.com']
    base = 'one@gmail.com|ABCD1234'
    rejected(lines[:1]*2, base)
    rejected(lines*6, base)
    rejected(['bad|password'], base)
    rejected(['valid@example.com|pass|BAD!'], base)
    rejected(['valid@example.com|pass||{bad json}'], base)
    rejected(['valid@example.com|pass||{"accessToken":"ignored"}'], base)
    rejected(lines[:1], 'one+foo@gmail.com|ABCD1234')
    rejected(lines[:1], 'one@gmail.com|short')
    rejected(lines[:1], base+'\no.ne@gmail.com|ABCD1234')
    jobs = {'x': {'email':'user0@example.com','status':'running','check_plan_only':True}}
    rejected(lines[:1], base, jobs)
    assert len(jobs) == 1
    rejected(lines[:1], base, [{'email':'user0@example.com','status':'success','change_email_only':True}])
    session='{"sessionToken":"fixture-session-token"}'
    assert prepare_email_jobs(['valid@example.com|pass|JBSWY3DPEHPK3PXP|' + session], base, {})[0]['initial_session'] == session
    rejected(['valid@example.com||JBSWY3DPEHPK3PXP|' + session], base)
    rejected(['valid@example.com|pass||' + session], base)
    for stage in ('submitted', 'changed_pending_verify', 'verified'):
        for status in ('error', 'cancelled'):
            rejected(lines[:1], base, [{'email':'user0@example.com','status':status,'email_change_stage':stage}])
    with patch('web.email_change_state.secrets.randbelow', return_value=0):
        allocated = prepare_email_jobs(lines[:1], 'one@gmail.com|ABCD1234',
            [{'email':'old@example.com','target_email':'o.ne+aaa@gmail.com','status':'error'}])
    assert allocated[0]['target_email'] == 'one+aab@gmail.com'
    print('Email allocator checks OK: 100/50, 50/1, collision, atomic validation, session, raw fields')


if __name__ == '__main__':
    main()

