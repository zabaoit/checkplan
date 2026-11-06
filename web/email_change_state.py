"""Atomic validation and alias allocation for change-email batches."""
from __future__ import annotations

import base64
import binascii
import re
import secrets
import string
from collections.abc import Mapping

_EMAIL = re.compile(r"[^\s@|]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}\Z")
_BASE = re.compile(r"[a-z0-9.]+@gmail\.com\Z", re.I)
_ALPHABET = string.ascii_lowercase + string.digits


def _get(job, name, default=None):
    return job.get(name, default) if isinstance(job, Mapping) else getattr(job, name, default)


def _canonical_alias(email):
    local, separator, domain = str(email).casefold().partition('@')
    if domain in {'gmail.com', 'googlemail.com'}:
        base, plus, suffix = local.partition('+')
        return base.replace('.', '') + (plus + suffix if plus else '') + '@gmail.com'
    return local + separator + domain


def prepare_email_jobs(lines, mailbox_text, jobs):
    """Return constructor kwargs only after the whole batch validates; no writes."""
    from change_password import cookies_from_initial_session

    if not isinstance(lines, (list, tuple)) or not 1 <= len(lines) <= 500:
        raise ValueError('Danh sách cần 1–500 dòng tài khoản')
    if not isinstance(mailbox_text, str):
        raise ValueError('Danh sách mailbox không hợp lệ')
    bases = []
    seen_bases = set()
    for number, raw in enumerate(mailbox_text.splitlines(), 1):
        if not raw.strip():
            continue
        parts = raw.split('|')
        if len(parts) != 2:
            raise ValueError(f'Mailbox dòng {number}: cần email|token')
        email, key = (part.strip() for part in parts)
        email = email.casefold()
        if not _BASE.fullmatch(email) or '..' in email or email.startswith('.') or email.split('@')[0].endswith('.'):
            raise ValueError(f'Mailbox dòng {number}: cần Gmail gốc không có dấu +')
        if not re.fullmatch(r'[A-Za-z0-9]{8,64}', key):
            raise ValueError(f'Mailbox dòng {number}: token cần 8–64 chữ/số')
        canonical = email.split('@')[0].replace('.', '')+'@gmail.com'
        if canonical in seen_bases:
            raise ValueError(f'Mailbox dòng {number}: trùng Gmail gốc')
        seen_bases.add(canonical)
        bases.append((email, key))
    if not bases:
        raise ValueError('Cần ít nhất một mailbox Congas')

    existing = list(jobs.values()) if isinstance(jobs, Mapping) else list(jobs)
    blocked = {str(_get(job, 'email', '')).casefold() for job in existing
               if _get(job, 'status') in {'queued', 'running'} or
               _get(job, 'email_change_stage') in {'submitted', 'changed_pending_verify', 'verified'} or
               ((_get(job, 'change_email_only', False) or _get(job, 'mode') == 'change_email')
                and _get(job, 'status') in {'success', 'pending'})}
    reserved = {_canonical_alias(_get(job, field, '')) for job in existing for field in ('email', 'target_email')}
    parsed = []
    sources = set()
    for number, raw in enumerate(lines, 1):
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f'Tài khoản dòng {number}: dòng rỗng/không hợp lệ')
        parts = raw.split('|', 3)
        if len(parts) < 2:
            raise ValueError(f'Tài khoản dòng {number}: cần email|password|2FA|session')
        email = parts[0].strip().casefold()
        password = parts[1]
        secret = parts[2] if len(parts) > 2 else ''
        session = parts[3].strip() if len(parts) > 3 else ''
        if not _EMAIL.fullmatch(email) or email in sources or email in blocked:
            raise ValueError(f'Tài khoản dòng {number}: email không hợp lệ, trùng hoặc đang xử lý/đã đổi mail')
        if not password.strip():
            raise ValueError(f'Tài khoản dòng {number}: cần password cũ để hậu kiểm')
        normalized_secret = secret.replace(' ', '').upper()
        if not normalized_secret:
            raise ValueError(f'Tài khoản dòng {number}: cần 2FA Base32 để hậu kiểm')
        if normalized_secret:
            try:
                base64.b32decode(normalized_secret+'='*((-len(normalized_secret))%8))
            except (ValueError, binascii.Error):
                raise ValueError(f'Tài khoản dòng {number}: 2FA không phải Base32') from None
        if session:
            if not session.startswith('{') and (len(session) < 16 or any(char.isspace() for char in session)):
                raise ValueError(f'Tài khoản dòng {number}: session token không hợp lệ')
            try:
                cookies_from_initial_session(session)
            except Exception:
                raise ValueError(f'Tài khoản dòng {number}: session thiếu cookie/token hợp lệ') from None
        sources.add(email)
        parsed.append(dict(email=email, current_password=password, secret=secret or None,
                           initial_session=session or None, change_email_only=True))
    reserved.update(_canonical_alias(source) for source in sources)
    # ponytail: three-character alias space is finite; use longer aliases if exhausted.
    for index, job in enumerate(parsed):
        email, key = bases[index % len(bases)]
        local, domain = email.split('@')
        start = secrets.randbelow(36**3)
        for offset in range(36**3):
            value = (start+offset) % (36**3)
            suffix = ''.join(_ALPHABET[(value//(36**power)) % 36] for power in (2, 1, 0))
            alias = f'{local}+{suffix}@{domain}'
            if _canonical_alias(alias) not in reserved:
                break
        else:
            raise ValueError('Mailbox hết alias ba ký tự khả dụng')
        reserved.add(_canonical_alias(alias))
        job.update(target_email=alias, mailbox_email=email, mailbox_key=key)
    return parsed


