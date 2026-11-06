"""Durable email-change checkpoints on the existing jobs repository."""
from __future__ import annotations

import asyncio
import json
import time
import uuid

from db import get_engine
from db.repositories import JobRepository
from .email_change_state import prepare_email_jobs

PENDING = {'submitted', 'changed_pending_verify', 'verified'}


def repository(manager):
    if getattr(manager, '_email_repo', None) is None:
        manager._email_repo = JobRepository(get_engine())
    return manager._email_repo


def save_email_job(manager, job):
    if not job.change_email_only:
        return
    state = json.dumps({'target_email': job.target_email, 'mailbox_email': job.mailbox_email,
                        'stage': job.email_change_stage, 'user_id': job.original_user_id,
                        'result': job.session_data}, ensure_ascii=False)
    repository(manager).update_status(job.id, job.status, error=job.error,
                                     password=job.current_password, secret=job.secret,
                                     user_id=job.original_user_id, session_data=state)


def add_email_jobs(manager, lines, mailbox_text):
    from .change_password_manager import ChangePasswordJob
    prepared = prepare_email_jobs(lines, mailbox_text, manager.jobs)
    jobs = [ChangePasswordJob(id=uuid.uuid4().hex[:12], **data) for data in prepared]
    repo = repository(manager)
    with repo.engine.transaction():
        for job in jobs:
            repo.create({'id': job.id, 'email': job.email, 'combo': '[redacted]',
                         'mail_mode': 'congas', 'status': 'queued', 'password': job.current_password,
                         'secret': job.secret, 'created_at': job.created_at, 'job_type': 'change_email'})
            save_email_job(manager, job)
    for job in jobs:
        manager.jobs[job.id] = job
        manager.order.append(job.id)
        manager._broadcast_job(job)
    manager._ensure_workers()
    for job in jobs:
        manager._job_queue.put_nowait(job.id)
    return jobs


def recover_email_jobs(manager):
    from .change_password_manager import ChangePasswordJob
    for row in repository(manager).list_all():
        if row.get('job_type') != 'change_email':
            continue
        state = json.loads(row.get('session_data') or '{}')
        job = ChangePasswordJob(id=row['id'], email=row['email'], current_password=row['password'] or '',
            secret=row['secret'], change_email_only=True, target_email=state['target_email'],
            mailbox_email=state['mailbox_email'], email_change_stage=state['stage'],
            original_user_id=state.get('user_id', ''), status=row['status'], error=row['error'],
            session_data=state.get('result'), created_at=row['created_at'],
            started_at=row.get('started_at'), finished_at=row.get('finished_at'))
        if job.status in {'queued', 'running'}:
            job.status, job.finished_at = 'error', time.time()
            job.error = ('Tiến trình bị ngắt; Retry chỉ xác minh email mới'
                         if job.email_change_stage in PENDING else
                         'Tiến trình bị ngắt trước gửi; nhập lại tài khoản và mailbox')
            save_email_job(manager, job)
        # ponytail: mailbox keys and source sessions stay in memory; prepared jobs need reimport after restart.
        manager.jobs[job.id] = job
        manager.order.append(job.id)


async def run_email_job(manager, job, proxy):
    from email_change import change_email
    if job.email_change_stage not in PENDING and not job.mailbox_key:
        raise ValueError('Cần nhập lại tài khoản và mailbox sau khi khởi động lại')
    if not hasattr(manager, '_email_locks'):
        manager._email_locks = {}
    local, domain = job.mailbox_email.casefold().split('@')
    lock = manager._email_locks.setdefault(local.replace('.', '')+'@'+domain, asyncio.Lock())
    async with lock:
        async def checkpoint(stage, user_id):
            job.email_change_stage, job.original_user_id = stage, user_id
            # Must commit before any remote mutation; storage errors stop the flow.
            save_email_job(manager, job)
            manager._broadcast_job(job)

        return await asyncio.wait_for(change_email(email=job.email, password=job.current_password,
            secret=job.secret, target_email=job.target_email, mailbox_email=job.mailbox_email,
            mailbox_key=job.mailbox_key, proxy=proxy,
            stage=job.email_change_stage, expected_user_id=job.original_user_id,
            checkpoint=checkpoint, log=lambda message: manager._job_log(job, message)),
            timeout=manager.job_timeout)


def delete_email_job(manager, job):
    if job.change_email_only:
        repository(manager).delete(job.id)
