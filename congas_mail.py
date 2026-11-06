"""Congas rental mailbox API, observed in its public /src/lib/mailApi.ts.

InboxMessage's public type lacks sender/recipient; callers serialize the full
change-email flow per canonical mailbox to correlate fresh messages safely.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
import math
import re

import httpx

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
_CLOCK_SKEW_GRACE = timedelta(seconds=30)
_URL_RE = re.compile(r"https?://\S+", re.I)
_EMAIL_TOKEN_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ADJACENT_MIME_RE = re.compile(r"(=\?[^?]+\?[bqBQ]\?[^?]*\?=)\s+(?==\?[^?]+\?[bqBQ]\?[^?]*\?=)")
_STATIC_ASSET_EXTS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".css", ".js", ".woff", ".woff2", ".ttf",
)
_BRAND_RE = re.compile(
    r"(?:OpenAI|ChatGPT|Dharma\s*Mail|openai\.com|chatgpt\.com|oaistatic\.com|oaiusercontent\.com)",
    re.I,
)
_INTENT_KW_RE = re.compile(
    r"\b(?:code|verification|verify|verifying|confirm|confirmation|confirming|otp|passcode|one[-\s]*time)\b"
    r"|mã\s*(?:xác\s*(?:minh|nhận|thực)|bảo\s*mật|chatgpt|openai|dùng\s*một\s*lần|của\s*bạn)"
    r"|xác\s*(?:minh|nhận)\s*(?:địa\s*chỉ\s*)?email",
    re.I,
)
_SUBJECT_OTP_RE = re.compile(
    r"\b(?:verification\s+code|login\s+code|security\s+code|one[-\s]*time\s+(?:code|password)|passcode|otp)\b"
    r"|mã\s*(?:xác\s*(?:minh|nhận|thực)|bảo\s*mật|dùng\s*một\s*lần)",
    re.I,
)
_NON_OTP_BODY_RE = re.compile(r"\b(?:invoice|receipt|billing|statement)\b", re.I)
_PROXIMITY_ANCHOR_RE = re.compile(
    r"(?:verification\s+code|login\s+code|security\s+code|one[-\s]*time\s+(?:code|password)|passcode|otp"
    r"|mã\s*xác\s*(?:minh|nhận|thực)|code\s*(?:is|:))",
    re.I,
)


class CongasMailError(RuntimeError):
    pass


class _TransientError(CongasMailError):
    def __init__(self, delay=4):
        super().__init__("Congas tạm thời không sẵn sàng")
        self.delay = delay


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1
            return
        if not self.hidden and tag == "img":
            attr_map = dict(attrs)
            for key in ("alt", "title"):
                val = attr_map.get(key)
                if isinstance(val, str) and val.strip():
                    self.parts.append(val.strip())

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _decode_mime_words(value: str) -> str:
    if not value or "=?" not in value:
        return value
    normalized = _ADJACENT_MIME_RE.sub(r"\1", value)
    chunks: list[str] = []
    try:
        for part, charset in decode_header(normalized):
            if isinstance(part, bytes):
                enc = (charset or "utf-8").strip()
                if enc.lower() == "utf8":
                    enc = "utf-8"
                try:
                    chunks.append(part.decode(enc, errors="replace"))
                except LookupError:
                    chunks.append(part.decode("utf-8", errors="replace"))
            else:
                chunks.append(str(part))
        return "".join(chunks)
    except Exception:
        return value


def _is_openai_domain(domain: str) -> bool:
    d = domain.casefold().strip(".")
    return (
        d in {"openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com"}
        or d.endswith((".openai.com", ".chatgpt.com", ".oaistatic.com", ".oaiusercontent.com"))
    )


def _addresses(value):
    if isinstance(value, list):
        return set().union(*(_addresses(item) for item in value)) if value else set()
    if isinstance(value, dict):
        value = value.get("address") or value.get("email") or ""
    return {address.casefold() for _, address in getaddresses([value]) if address} if isinstance(value, str) else set()


def _metadata(message):
    sender = _addresses(message.get("from") or message.get("sender"))
    recipients = _addresses(message.get("to") or message.get("recipient") or message.get("recipients"))
    return sender, recipients


def _message_key(message) -> str:
    msg_id = str(message.get("id") or "").strip()
    if msg_id:
        return msg_id
    subj = str(message.get("subject") or "").strip().casefold()
    dt = str(message.get("date") or "").strip()
    rec = str(message.get("receivedAt") or "").strip()
    html = str(message.get("htmlBody") or message.get("body") or "").strip()
    if not (subj or dt or rec or html):
        return ""
    return f"{subj}|{dt}|{rec}|{len(html)}"


def _parse_single_datetime(value) -> datetime | None:
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _received_at(message) -> datetime | None:
    for field in ("receivedAt", "date", "created_at"):
        parsed = _parse_single_datetime(message.get(field))
        if parsed is not None:
            return parsed
    return None


def _base_mailbox(address: str) -> str:
    local, sep, domain = address.casefold().partition("@")
    if not sep:
        return address.casefold()
    base_local, _, _ = local.partition("+")
    if domain in {"gmail.com", "googlemail.com"}:
        base_local = base_local.replace(".", "")
        domain = "gmail.com"
    return f"{base_local}@{domain}"


def _extract_body_emails(text: str) -> set[str]:
    found: set[str] = set()
    for match in _EMAIL_TOKEN_RE.findall(text.casefold()):
        if match.endswith(_STATIC_ASSET_EXTS):
            continue
        found.add(match)
    return found


def _mask_subject_for_log(subject: str) -> str:
    decoded = _decode_mime_words(str(subject or "")).strip()
    masked = _EMAIL_TOKEN_RE.sub("[email]", decoded)
    masked = re.sub(r"[0-9]", "X", masked)
    masked = re.sub(r"\s+", " ", masked)
    return masked[:64] or "(empty)"


def extract_otp_with_reason(
    message,
    recipient: str,
    started_at: datetime,
    baseline: set[str],
    *,
    source_email: str = "",
) -> tuple[str, str]:
    key = _message_key(message)
    if not key or key in baseline:
        return "", "baseline"

    recipient_cf = recipient.strip().casefold()
    recipient_base = _base_mailbox(recipient_cf)
    source_cf = source_email.strip().casefold()

    sender, recipients = _metadata(message)
    if recipients and recipient_cf not in recipients:
        if not (
            any(_base_mailbox(addr) == recipient_base for addr in recipients)
            and all("+" not in addr for addr in recipients)
        ):
            return "", "recipient_header"
    if sender and (
        len(sender) != 1 or not all(_is_openai_domain(address.rpartition("@")[2]) for address in sender)
    ):
        return "", "sender_header"

    received = _received_at(message)
    has_date_field = any(
        isinstance(message.get(k), (str, int, float)) and str(message.get(k)).strip()
        for k in ("receivedAt", "date", "created_at")
    )
    if has_date_field and received is None:
        return "", "bad_date"
    if received is not None and received < started_at.replace(microsecond=0) - _CLOCK_SKEW_GRACE:
        return "", "too_old"

    raw_html = str(
        message.get("htmlBody")
        or message.get("body")
        or message.get("textBody")
        or message.get("text")
        or message.get("html")
        or ""
    )
    parser = _Text()
    parser.feed(raw_html)
    raw_body_text = " ".join(parser.parts)
    raw_subject = _decode_mime_words(str(message.get("subject") or ""))

    if not _BRAND_RE.search(f"{raw_subject} {raw_body_text} {raw_html}"):
        return "", "no_brand"

    body = _URL_RE.sub(" ", raw_body_text)
    subject = _URL_RE.sub(" ", raw_subject)

    addresses = _extract_body_emails(body)
    recipient_matched_in_body = recipient_cf in addresses or (
        any(_base_mailbox(a) == recipient_base for a in addresses)
        and all("+" not in a for a in addresses if _base_mailbox(a) == recipient_base)
    )
    if not recipient_matched_in_body:
        for addr in addresses:
            if source_cf and addr == source_cf:
                continue
            if _is_openai_domain(addr.rpartition("@")[2]):
                continue
            return "", "foreign_email"

    body_scan = _EMAIL_TOKEN_RE.sub(" ", body)
    subject_scan = _EMAIL_TOKEN_RE.sub(" ", subject)
    body_codes = set(re.findall(r"(?<![0-9])[0-9]{6}(?![0-9])", body_scan))
    if not body_codes:
        body_codes = {
            re.sub(r"\s+", "", m)
            for m in re.findall(r"(?<![0-9])(?:[0-9]\s+){5}[0-9](?![0-9])", body_scan)
        }
    subject_codes = set(re.findall(r"(?<![0-9])[0-9]{6}(?![0-9])", subject_scan))

    if body_codes:
        has_intent = bool(_INTENT_KW_RE.search(body)) or (
            bool(_SUBJECT_OTP_RE.search(subject)) and not _NON_OTP_BODY_RE.search(body)
        )
        if not has_intent:
            return "", "no_body_intent"
        if len(body_codes) == 1:
            return next(iter(body_codes)), "ok"
        if len(subject_codes) == 1 and _INTENT_KW_RE.search(subject):
            common = body_codes & subject_codes
            if len(common) == 1:
                return next(iter(common)), "ok"
        near_codes: set[str] = set()
        for anchor in _PROXIMITY_ANCHOR_RE.finditer(body_scan):
            window = body_scan[anchor.end() : anchor.end() + 100]
            near_codes.update(re.findall(r"(?<![0-9])[0-9]{6}(?![0-9])", window))
        if len(near_codes) == 1:
            return next(iter(near_codes)), "ok"
        return "", f"ambiguous_codes({len(body_codes)})"

    if len(subject_codes) == 1 and _INTENT_KW_RE.search(subject):
        return next(iter(subject_codes)), "ok"

    return "", "no_code"


def extract_otp(message, recipient, started_at, baseline, *, source_email: str = ""):
    code, _ = extract_otp_with_reason(
        message,
        recipient,
        started_at,
        baseline,
        source_email=source_email,
    )
    return code


class CongasMailbox:
    def __init__(self, email, key, log=lambda _: None):
        self.email = email.strip().casefold()
        self.key = key.strip().upper()
        if not re.fullmatch(r"[^\s@|]+@(?:gmail\.com|googlemail\.com|icloud\.com)", self.email):
            raise ValueError("Email Congas không hợp lệ")
        if not re.fullmatch(r"[A-Z0-9]{8,64}", self.key):
            raise ValueError("Token Congas phải gồm 8–64 chữ/số")
        self.log = log
        self.client = None
        self.access_token = ""
        self.baseline = set()
        self.prepared = False

    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            base_url="https://mail.congas.uk",
            headers={"User-Agent": _BROWSER_UA, "Accept": "application/json"},
            timeout=15,
            follow_redirects=False,
        )
        return self

    async def __aexit__(self, *exc):
        if self.client is not None:
            await self.client.aclose()
        self.client = None
        self.access_token = ""
        self.key = ""
        self.prepared = False

    async def _request(self, method, path, **kwargs):
        for attempt in range(3):
            try:
                return await self._request_once(method, path, **kwargs)
            except _TransientError as exc:
                if attempt == 2:
                    raise CongasMailError("Congas tạm thời không sẵn sàng sau 3 lần thử") from None
                await asyncio.sleep(exc.delay)

    async def _request_once(self, method, path, **kwargs):
        if self.client is None:
            raise CongasMailError("CongasMailbox cần async with")
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.TransportError:
            raise _TransientError() from None
        if response.status_code in {401, 403}:
            raise CongasMailError("Congas từ chối token hoặc quyền đọc mailbox")
        if response.status_code == 429 or response.status_code >= 500:
            delay = 4.0
            retry_after = response.headers.get("Retry-After", "")
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    pass
            if not math.isfinite(delay):
                delay = 4.0
            # Never retry earlier than requested; shared deadline bounds waits.
            raise _TransientError(max(4.0, delay))
        if not response.is_success:
            raise CongasMailError(f"Congas HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise CongasMailError("Congas trả dữ liệu không phải JSON") from None
        if not isinstance(payload, dict) or payload.get("success") is False:
            raise CongasMailError("Congas từ chối yêu cầu")
        return payload

    async def _inbox(self):
        payload = await self._request("GET", "/api/rentals/inbox", params={
            "email": self.email, "rentalAccessToken": self.access_token,
        })
        if str(payload.get("email", "")).casefold() != self.email:
            raise CongasMailError("Congas trả mailbox không đúng email gốc")
        messages = payload.get("messages")
        if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
            raise CongasMailError("Danh sách thư Congas không hợp lệ")
        return messages

    async def prepare(self):
        self.prepared = False
        async with asyncio.timeout(30):
            payload = await self._request("POST", "/api/rentals/validate", json={"accounts": [f"{self.email}|{self.key}"]})
            accounts = payload.get("accounts")
            account = next((item for item in accounts if isinstance(item, dict) and str(item.get("email", "")).casefold() == self.email), None) if isinstance(accounts, list) else None
            if not account or account.get("status", "valid") != "valid" or not isinstance(account.get("rentalAccessToken"), str) or not account["rentalAccessToken"]:
                raise CongasMailError("Congas không xác thực được email/token")
            self.access_token = account["rentalAccessToken"]
            messages = await self._inbox()
            self.baseline = {_message_key(message) for message in messages if _message_key(message)}
            self.prepared = True

    async def poll_otp(
        self,
        *,
        recipient,
        started_at,
        timeout_seconds=180,
        poll_interval_seconds=4,
        log=None,
        source_email: str = "",
    ):
        if not self.prepared:
            raise CongasMailError("Phải prepare mailbox trước khi gửi OTP")
        if not isinstance(started_at, datetime) or started_at.tzinfo is None:
            raise ValueError("started_at cần datetime có timezone")
        if not all(math.isfinite(value) and value > 0 for value in (timeout_seconds, poll_interval_seconds)):
            raise ValueError("OTP timeout và poll interval phải dương, hữu hạn")
        local, _, domain = self.email.partition("@")
        if recipient.casefold() != self.email and not re.fullmatch(re.escape(local) + r"\+[a-z0-9]{3}@" + re.escape(domain), recipient.casefold()):
            raise ValueError("Recipient phải thuộc email gốc Congas")
        emit = log or self.log or (lambda _: None)
        polls = 0
        async with asyncio.timeout(timeout_seconds):
            while True:
                polls += 1
                messages = await self._inbox()
                new_messages = [m for m in messages if _message_key(m) and _message_key(m) not in self.baseline]
                matches: list[tuple[datetime, str]] = []
                skip_notes: list[str] = []
                for message in new_messages:
                    code, reason = extract_otp_with_reason(
                        message,
                        recipient,
                        started_at,
                        self.baseline,
                        source_email=source_email,
                    )
                    if code:
                        matches.append((_received_at(message) or started_at, code))
                    else:
                        skip_notes.append(f"{reason}:{_mask_subject_for_log(message.get('subject', ''))}")
                if matches:
                    latest = max(date for date, _ in matches)
                    codes = {code for date, code in matches if date == latest}
                    if len(codes) == 1:
                        return codes.pop()
                if polls == 1 or polls % 5 == 0:
                    extra = f" (skip={'; '.join(skip_notes[:2])})" if skip_notes else ""
                    emit(f"[congas] poll #{polls}: tổng {len(messages)} thư, {len(new_messages)} thư mới{extra}")
                await asyncio.sleep(max(4, poll_interval_seconds))
