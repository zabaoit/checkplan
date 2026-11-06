"""Offline Congas contract check; run: python test/check_change_email_congas.py."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from congas_mail import CongasMailbox, CongasMailError, extract_otp


def check_extract():
    now = datetime.now(timezone.utc)
    alias = 'fixture+abc@gmail.com'
    message = dict(id='fresh', receivedAt=now.replace(microsecond=0).isoformat(),
                   subject='Verify your email address', htmlBody='OpenAI verification code: 012345')
    assert extract_otp(message, alias, now, set()) == '012345'
    assert extract_otp(message, alias, now, {'fresh'}) == ''
    assert extract_otp({**message, 'receivedAt': (now-timedelta(minutes=1)).isoformat()}, alias, now, set()) == ''
    assert extract_otp({**message, 'to': 'fixture+bad@gmail.com'}, alias, now, set()) == ''
    assert extract_otp({**message, 'from': 'noreply@evilopenai.com'}, alias, now, set()) == ''
    assert extract_otp({**message, 'from': 'OpenAI <noreply@tm.openai.com>', 'to': alias}, alias, now, set()) == '012345'
    assert extract_otp({**message, 'htmlBody': 'OpenAI invoice 012345'}, alias, now, set()) == ''
    assert extract_otp({**message, 'htmlBody': 'OpenAI verification code 012345 or 123456'}, alias, now, set()) == ''
    assert extract_otp({**message, 'htmlBody': message['htmlBody']+' other@gmail.com'}, alias, now, set()) == ''
    assert extract_otp({**message, 'htmlBody': message['htmlBody']+' '+alias+' other@gmail.com'}, alias, now, set()) == '012345'


async def check_api():
    now = datetime.now(timezone.utc)
    requests = []
    reads = 0
    async def handle(request):
        nonlocal reads
        requests.append(request)
        if request.url.path == '/api/rentals/validate':
            assert request.method == 'POST'
            assert b'fixture@gmail.com|ABCD1234' in request.content
            return httpx.Response(200, json={'success': True, 'accounts': [
                {'email': 'fixture@gmail.com', 'status': 'valid', 'rentalAccessToken': 'fixture-access'}]})
        assert request.url.path == '/api/rentals/inbox'
        assert request.url.params['email'] == 'fixture@gmail.com'
        assert request.url.params['rentalAccessToken'] == 'fixture-access'
        reads += 1
        messages = [dict(id='old', receivedAt=(now-timedelta(minutes=1)).isoformat(),
                         subject='Verify your email address', htmlBody='OpenAI verification code: 111111')]
        if reads > 1:
            messages.append(dict(id='new', receivedAt=now.isoformat(),
                                 subject='Verify your email address', htmlBody='OpenAI verification code: 012345'))
        return httpx.Response(200, json={'success': True, 'email': 'fixture@gmail.com', 'messages': messages})
    box = CongasMailbox('fixture@gmail.com', 'abcd1234')
    async with box:
        await box.client.aclose()
        box.client = httpx.AsyncClient(base_url='https://mail.congas.uk', transport=httpx.MockTransport(handle))
        await box.prepare()
        assert reads == 1 and box.baseline == {'old'}
        assert await box.poll_otp(recipient='fixture+abc@gmail.com', started_at=now, timeout_seconds=1) == '012345'
        try:
            await box.poll_otp(recipient='other@gmail.com', started_at=now, timeout_seconds=1)
        except ValueError:
            pass
        else:
            raise AssertionError('unrelated mailbox accepted')
        box.baseline.add('new')
        try:
            await box.poll_otp(recipient='fixture+abc@gmail.com', started_at=now, timeout_seconds=.02)
        except TimeoutError:
            pass
        else:
            raise AssertionError('poll exceeded shared timeout')
        task = asyncio.create_task(box.poll_otp(recipient='fixture+abc@gmail.com', started_at=now))
        await asyncio.sleep(.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('poll swallowed cancellation')
    assert box.client is None and not box.key and not box.access_token
    assert len(requests) >= 4


async def check_rejected_token():
    box = CongasMailbox('fixture@gmail.com', 'ABCD1234')
    async with box:
        await box.client.aclose()
        box.client = httpx.AsyncClient(base_url='https://mail.congas.uk', transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={'msg': 'secret must never be printed'})))
        try:
            await box.prepare()
        except CongasMailError as exc:
            assert 'secret' not in str(exc)
        else:
            raise AssertionError('rejected token accepted')


async def main():
    check_extract()
    await check_api()
    await check_rejected_token()
    print('Congas offline checks OK')


if __name__ == '__main__':
    asyncio.run(main())
