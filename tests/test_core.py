import asyncio
import json
import sqlite3
import zipfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telethon import errors, types

from sessionbot.storage import Store
from sessionbot.files import ingest, persist_zip
from sessionbot.telegram import authorization, code_sources, read_code, migrate, Source, UserFacingError
from sessionbot.engine import Engine


def client():
    class FakeClient(NS):
        async def __call__(self, request):
            return None
    return FakeClient(is_user_authorized=AsyncMock(return_value=True), get_me=AsyncMock(return_value=NS(id=123, phone='10000000000', deleted=False)),
              get_entity=AsyncMock(side_effect=lambda address: NS(id=777000 if address == 777000 else 427770)),
              get_messages=AsyncMock(return_value=[NS(id=80)]), send_code_request=AsyncMock(), sign_in=AsyncMock())


def sent(kind=None, **kwargs):
    return types.auth.SentCode(type=kind or types.auth.SentCodeTypeApp(length=5), phone_code_hash='secret', **kwargs)


def messages(items):
    async def iterator(*args, **kwargs):
        for item in items:
            yield item
    return iterator


@pytest.mark.parametrize('authorized,deleted,expected', [(True, False, 'Активна'), (False, False, 'Не авторизована'), (True, True, 'Аккаунт удалён')])
async def test_authorization(authorized, deleted, expected):
    c = client()
    c.is_user_authorized.return_value = authorized
    c.get_me.return_value.deleted = deleted
    assert await authorization(c) == expected


@pytest.mark.parametrize('exception,expected', [(errors.SessionRevokedError(None), 'Отозвана'), (errors.UserDeactivatedError(None), 'Аккаунт удалён'), (OSError(), 'Ошибка подключения')])
async def test_authorization_errors(exception, expected):
    c = client()
    c.is_user_authorized.side_effect = exception
    assert expected in await authorization(c)


async def test_sources_independent_boundaries_and_phone_resolution():
    c = client()
    c.get_messages.side_effect = [[NS(id=900)], [NS(id=12)]]
    sources, diagnostics = await code_sources(c)
    assert [s.boundary for s in sources] == [900, 12]
    assert [s.entity.id for s in sources] == [777000, 427770]
    assert c.get_entity.call_args_list[1].args == ('+42777',)
    assert not diagnostics


async def test_duplicate_peer_is_read_once():
    c = client()
    c.get_entity.side_effect = None
    c.get_entity.return_value = NS(id=777000)
    sources, _ = await code_sources(c)
    assert len(sources) == 1
    assert c.get_messages.await_count == 1


@pytest.mark.parametrize('peer', [777000, 427770])
async def test_code_from_either_peer_ignores_clock(peer):
    c = client()
    seen = []
    async def iterator(entity, min_id, limit):
        seen.append((entity.id, min_id))
        if entity.id == peer:
            yield NS(id=min_id + 1, out=False, raw_text='Login code: 12345', date='1900-01-01')
    c.iter_messages = iterator
    code, _ = await read_code(c, [Source(NS(id=777000), 100, 'a'), Source(NS(id=427770), 2, 'b')], 5)
    assert code == '12345'
    if peer == 427770:
        assert seen == [(777000, 100), (427770, 2)]


@pytest.mark.parametrize('items,reason', [([], 'Нет нового'), ([NS(id=80, out=False, raw_text='12345')], 'Нет нового'), ([NS(id=81, out=True, raw_text='12345')], 'Нет нового'), ([NS(id=81, out=False, raw_text='не код')], 'Ошибка распознавания')])
async def test_new_incoming_only_and_diagnostics(items, reason):
    c = client()
    c.iter_messages = messages(items)
    code, diagnostic = await read_code(c, [Source(777000, 80, 'a')], 5)
    assert code is None and reason in diagnostic


async def test_unavailable_distinct_from_no_message():
    c = client()
    async def iterator(*args, **kwargs):
        raise ValueError()
        yield
    c.iter_messages = iterator
    assert (await read_code(c, [Source(1, 0, 'a')], 5))[1] == 'Служебные чаты недоступны'


async def test_alternative_sms_uses_manual_never_reads():
    old, new = client(), client()
    old.iter_messages = AsyncMock(side_effect=AssertionError('Must not read SMS'))
    new.send_code_request.return_value = sent(types.auth.SentCodeTypeSms(length=5))
    ask = AsyncMock(return_value='45678')
    await migrate(old, new, ask, AsyncMock(), {})
    assert ask.await_args.args[0] == 'code'
    assert new.sign_in.await_args.kwargs['code'] == '45678'
    old.iter_messages.assert_not_called()


async def test_boundaries_before_request_and_app_code():
    old, new = client(), client()
    old.iter_messages = messages([NS(id=81, out=False, raw_text='Code 12345')])
    async def request(phone):
        assert old.get_messages.await_count == 2
        return sent()
    new.send_code_request.side_effect = request
    ask = AsyncMock(side_effect=AssertionError('automatic app delivery'))
    await migrate(old, new, ask, AsyncMock(), {})
    assert new.sign_in.await_args.kwargs['code'] == '12345'


@pytest.mark.parametrize('saved', [True, False])
async def test_2fa(saved):
    old, new = client(), client()
    new.send_code_request.return_value = sent(types.auth.SentCodeTypeSms(length=5))
    new.sign_in.side_effect = [errors.SessionPasswordNeededError(None), None]
    ask = AsyncMock(side_effect=['12345', 'manual-password'])
    await migrate(old, new, ask, AsyncMock(), {'password': 'stored-password'} if saved else {})
    assert new.sign_in.await_args.kwargs['password'] == ('stored-password' if saved else 'manual-password')


async def test_floodwait_propagates_without_retry():
    old, new = client(), client()
    new.send_code_request.side_effect = errors.FloodWaitError(None, capture=40)
    with pytest.raises(errors.FloodWaitError):
        await migrate(old, new, AsyncMock(), AsyncMock(), {})
    assert new.send_code_request.await_count == 1


async def test_one_resend_waits_and_refreshes_boundaries():
    old, new = client(), client()
    new.send_code_request.return_value = sent(types.auth.SentCodeTypeSms(5), next_type=types.auth.CodeTypeSms(), timeout=19)
    class New:
        send_code_request = new.send_code_request
        sign_in = new.sign_in
        is_user_authorized = new.is_user_authorized
        get_me = new.get_me
        async def __call__(self, request):
            assert old.get_messages.await_count == 4
            return sent(types.auth.SentCodeTypeSms(5))
    sleep = AsyncMock()
    await migrate(old, New(), AsyncMock(side_effect=['resend', '12345']), AsyncMock(), {}, sleep=sleep)
    sleep.assert_awaited_once_with(19)


async def test_resend_not_allowed():
    old, new = client(), client()
    new.send_code_request.return_value = sent(types.auth.SentCodeTypeSms(5))
    with pytest.raises(UserFacingError, match='не разрешил'):
        await migrate(old, new, AsyncMock(return_value='resend'), AsyncMock(), {})
    new.sign_in.assert_not_called()


async def test_nonstandard_confirmation_preserves_no_signin():
    old, new = client(), client()
    new.send_code_request.return_value = sent(types.auth.SentCodeTypeEmailCode(email_pattern='a***', length=5))
    with pytest.raises(UserFacingError, match='требует подтверждения'):
        await migrate(old, new, AsyncMock(), AsyncMock(), {})
    new.sign_in.assert_not_called()


def test_queue_recovery_and_no_replay(tmp_path):
    s = Store(tmp_path)
    a = s.add(1, 'a', '/x', {}, 'check')
    b = s.add(1, 'b', '/y', {}, 'check')
    c = s.add(1, 'c', '/z', {}, 'check')
    s.update(c, state='done')
    assert s.claim()['id'] == a
    s.db.close()
    s = Store(tmp_path)
    assert s.get(a)['state'] == 'interrupted'
    assert s.claim()['id'] == b
    assert s.claim() is None


def test_queue_account_lock_pause_and_ownership(tmp_path):
    s = Store(tmp_path)
    a = s.add(1, 'a', '/x', {}, 'check')
    s.add(2, 'a', '/y', {}, 'check')
    s.pause(1, True)
    assert s.claim()['owner'] == 2
    s.pause(1, False)
    assert s.claim() is None
    assert s.get(a, 2) is None


async def test_cancel_manual_inputs_are_per_task(tmp_path):
    s = Store(tmp_path)
    bot = NS(send_message=AsyncMock())
    e = Engine(s, bot, 1, 'x')
    a = s.add(1, 'a', '/x', {}, 'migrate')
    b = s.add(1, 'b', '/y', {}, 'migrate')
    ta = asyncio.create_task(e.ask(s.get(a), 'code', 'prompt'))
    tb = asyncio.create_task(e.ask(s.get(b), 'password', 'prompt'))
    await asyncio.sleep(0)
    assert not e.answer(2, a, 'secret')
    assert e.answer(1, b, 'pass')
    assert await tb == 'pass'
    ta.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ta
    assert not e.inputs


async def test_delivery_failure_keeps_durable_zip(tmp_path):
    session = tmp_path / 'a.session'
    session.write_bytes(b'example')
    result = persist_zip(tmp_path / 'results', 'abc', session, {'password': 'secret', 'proxy': {'password': 'secret'}})
    s = Store(tmp_path)
    ident = s.add(1, 'a', str(session), {}, 'check')
    s.update(ident, state='done', result=result)
    e = Engine(s, NS(send_document=AsyncMock(side_effect=OSError())), 1, 'x')
    await e.deliver(s.get(ident))
    assert s.get(ident)['state'] == 'done'
    assert s.get(ident)['delivered'] == 0
    with zipfile.ZipFile(result) as archive:
        assert json.loads(archive.read('account.json')) == {}
    e.bot.send_document.side_effect = None
    await e.deliver(s.get(ident))
    assert s.get(ident)['delivered'] == 1


@pytest.mark.parametrize('name', ['../x.session', '/x.session', 'a\\x.session', 'C:x.session'])
def test_zip_traversal(tmp_path, name):
    archive = tmp_path / 'upload.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr(name, b'x')
    with pytest.raises(ValueError):
        ingest(archive, tmp_path / 'out', 10000)


def test_valid_session_and_zip_pair(tmp_path):
    source = tmp_path / 'original.session'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE sessions(dc_id,server_address,port,auth_key)')
        db.execute('INSERT INTO sessions VALUES(2,\'example\',443,?)', (b'x' * 256,))
    archive = tmp_path / 'upload.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.write(source, 'account.session')
        z.writestr('account.json', '{"device_model":"Test"}')
    entries = ingest(archive, tmp_path / 'out', 100000)
    assert entries[0][1]['device_model'] == 'Test'


async def test_worker_cancellation_disconnects_and_removes_work(tmp_path, monkeypatch):
    from sessionbot import engine as module
    s = Store(tmp_path)
    src = tmp_path / 'source.session'
    src.write_bytes(b'original')
    ident = s.add(1, 'a', src, {}, 'check')
    started = asyncio.Event()
    async def connect():
        started.set()
        await asyncio.Event().wait()
    c = NS(connect=connect, disconnect=AsyncMock())
    monkeypatch.setattr(module, 'make_client', lambda *args: c)
    monkeypatch.setattr(module, 'session_fingerprint', lambda *args: 'fingerprint')
    e = Engine(s, NS(send_message=AsyncMock()), 1, 'x')
    task = asyncio.create_task(e.run_job(s.claim()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    c.disconnect.assert_awaited_once()
    assert not (tmp_path / 'work' / ident).exists()
    assert src.read_bytes() == b'original'
    assert s.get(ident)['state'] == 'cancelled'


async def test_sent_code_success_verifies_identity():
    old, new = client(), client()
    new.send_code_request.return_value = types.auth.SentCodeSuccess(authorization=types.auth.Authorization(user=types.User(id=123)))
    await migrate(old, new, AsyncMock(), AsyncMock(), {})
    new.sign_in.assert_not_called()
    new.get_me.return_value.id = 999
    with pytest.raises(UserFacingError, match='не соответствует'):
        await migrate(old, new, AsyncMock(), AsyncMock(), {})


async def test_source_floodwait_is_not_treated_as_unavailable():
    c = client()
    c.get_entity.side_effect = errors.FloodWaitError(None, capture=50)
    with pytest.raises(errors.FloodWaitError):
        await code_sources(c)
    assert c.get_entity.await_count == 1


async def test_reader_floodwait_is_not_treated_as_no_message():
    c = client()
    async def iterator(*args, **kwargs):
        raise errors.FloodWaitError(None, capture=20)
        yield
    c.iter_messages = iterator
    with pytest.raises(errors.FloodWaitError):
        await read_code(c, [Source(1, 0, 'a')], 5)


async def test_app_timeout_reason_reported_before_manual():
    old, new = client(), client()
    old.iter_messages = messages([])
    new.send_code_request.return_value = sent()
    report = AsyncMock()
    ask = AsyncMock(return_value='12345')
    await migrate(old, new, ask, report, {}, poll_seconds=3, sleep=AsyncMock())
    assert any('Нет нового входящего' in call.args[0] for call in report.await_args_list)
    assert ask.await_count == 1


async def test_plain_check_has_no_cleanup_or_login(tmp_path, monkeypatch):
    from sessionbot import engine as module
    s = Store(tmp_path)
    src = tmp_path / 'source.session'
    src.write_bytes(b'original')
    ident = s.add(1, 'a', src, {}, 'check')
    c = client()
    c.connect, c.disconnect = AsyncMock(), AsyncMock()
    c.sign_in.side_effect = AssertionError('No new login')
    c.send_code_request.side_effect = AssertionError('No code request')
    monkeypatch.setattr(module, 'make_client', lambda *args: c)
    monkeypatch.setattr(module, 'session_fingerprint', lambda *args: 'fingerprint')
    e = Engine(s, NS(send_message=AsyncMock(), send_document=AsyncMock()), 1, 'x')
    await e.run_job(s.claim())
    assert s.get(ident)['state'] == 'done'
    c.sign_in.assert_not_called()
    c.send_code_request.assert_not_called()
    assert src.read_bytes() == b'original'


async def test_cancellation_during_delivery_preserves_completed_result(tmp_path, monkeypatch):
    from sessionbot import engine as module
    s = Store(tmp_path)
    src = tmp_path / 'source.session'
    src.write_bytes(b'original')
    ident = s.add(1, 'a', src, {}, 'check')
    c = client()
    c.connect, c.disconnect = AsyncMock(), AsyncMock()
    monkeypatch.setattr(module, 'make_client', lambda *args: c)
    monkeypatch.setattr(module, 'session_fingerprint', lambda *args: 'fingerprint')
    started = asyncio.Event()
    async def deliver(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()
    e = Engine(s, NS(send_message=AsyncMock(), send_document=deliver), 1, 'x')
    task = asyncio.create_task(e.run_job(s.claim()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert s.get(ident)['state'] == 'done'
    assert __import__('pathlib').Path(s.get(ident)['result']).exists()


@pytest.mark.parametrize('mode,request_name', [('terminate', 'ResetAuthorizationsRequest'), ('clear_contacts', 'DeleteContactsRequest'), ('clear_drafts', 'ClearAllDraftsRequest')])
async def test_modes_do_only_the_selected_mutation(tmp_path, monkeypatch, mode, request_name):
    from sessionbot import engine as module
    s = Store(tmp_path)
    src = tmp_path / 'source.session'
    src.write_bytes(b'original')
    ident = s.add(1, 'a', src, {}, mode)
    requests = []
    class Fake(type(client())):
        async def __call__(self, request):
            name = type(request).__name__
            requests.append(name)
            if name == 'GetContactsRequest':
                return NS(users=[types.User(id=42, access_hash=123)])
    c = Fake(**vars(client()))
    c.connect, c.disconnect = AsyncMock(), AsyncMock()
    monkeypatch.setattr(module, 'make_client', lambda *args: c)
    monkeypatch.setattr(module, 'session_fingerprint', lambda *args: 'fingerprint')
    e = Engine(s, NS(send_message=AsyncMock(), send_document=AsyncMock()), 1, 'x')
    await e.run_job(s.claim())
    assert s.get(ident)['state'] == 'done'
    mutations = set(requests) - {'GetStateRequest', 'GetContactsRequest'}
    assert mutations == {request_name}
    c.send_code_request.assert_not_called()


async def test_failed_new_session_verification_never_returns_success():
    old, new = client(), client()
    new.send_code_request.return_value = sent(types.auth.SentCodeTypeSms(5))
    new.is_user_authorized.return_value = False
    with pytest.raises(UserFacingError, match='не подтверждён'):
        await migrate(old, new, AsyncMock(return_value='12345'), AsyncMock(), {})


async def test_expiry_cleans_results_source_and_password_but_not_active(tmp_path):
    import time
    from pathlib import Path
    s = Store(tmp_path)
    folder = tmp_path / 'uploads' / 'a'
    folder.mkdir(parents=True)
    src = folder / 'source.session'
    src.write_bytes(b'original')
    result = persist_zip(tmp_path / 'results', 'a', src, {})
    a = s.add(1, 'a', src, {'password': 'secret'}, 'check')
    b = s.add(1, 'b', '/active/source.session', {'password': 'secret'}, 'check')
    s.update(a, state='done', result=result)
    s.db.execute('UPDATE jobs SET created=?', (time.time() - 9 * 86400,))
    Engine(s, None, 1, 'x').expire()
    assert not src.exists() and not Path(result).exists()
    assert s.get(a)['config'] == '{}'
    assert 'secret' in s.get(b)['config']


def test_zip_symlink_and_expansion_limit(tmp_path):
    z = tmp_path / 'upload.zip'
    entry = zipfile.ZipInfo('x.session')
    entry.external_attr = 0o120777 << 16
    with zipfile.ZipFile(z, 'w') as archive:
        archive.writestr(entry, 'target')
    with pytest.raises(ValueError, match='Небезопасный'):
        ingest(z, tmp_path / 'one', 10000)
    with zipfile.ZipFile(z, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('x.session', b'x' * 100000)
    with pytest.raises(ValueError, match='большой архив'):
        ingest(z, tmp_path / 'two', 10000)


async def test_controlled_send_preserves_actual_success_without_wrapper_retry(monkeypatch):
    from sessionbot.telegram import ControlledClient
    result = types.auth.SentCodeSuccess(authorization=types.auth.Authorization(user=types.User(id=123)))
    rpc = AsyncMock(return_value=result)
    async def call(self, request):
        return await rpc(request)
    monkeypatch.setattr(ControlledClient, '__call__', call)
    c = ControlledClient(None, 123, 'test-api-hash', request_retries=0, flood_sleep_threshold=0)
    assert await c.send_code_request('10000000000') is result
    assert type(rpc.await_args.args[0]).__name__ == 'SendCodeRequest'
    rpc.side_effect = errors.AuthRestartError(None)
    with pytest.raises(errors.AuthRestartError):
        await c.send_code_request('10000000000')
    assert rpc.await_count == 2  # One request per explicit invocation, no hidden retry.


async def test_explicit_auth_probe_does_not_swallow_revocation():
    class Revoked(type(client())):
        async def __call__(self, request):
            raise errors.SessionRevokedError(request)
    c = Revoked(**vars(client()))
    assert await authorization(c) == 'Отозвана'
    c.is_user_authorized.assert_not_called()


def test_persistent_floodwait_blocks_following_account_jobs(tmp_path):
    s = Store(tmp_path)
    s.db.execute('INSERT INTO aliases VALUES(?,?)', ('fingerprint', 'tg:123'))
    ident = s.add(1, 'fingerprint', '/x', {}, 'check')
    s.cooldown('tg:123', 300)
    assert s.claim() is None
    s.db.close()
    s = Store(tmp_path)
    assert s.remaining('tg:123') > 0
    assert s.claim() is None
    s.db.execute('UPDATE cooldowns SET until=0')
    assert s.claim()['id'] == ident


async def test_authorization_floodwait_propagates_for_persistent_cooldown():
    c = client()
    c.is_user_authorized.side_effect = errors.FloodWaitError(None, capture=33)
    with pytest.raises(errors.FloodWaitError):
        await authorization(c)


async def test_accessible_service_phone_resolved_from_dialog_not_numeric_id():
    c = client()
    async def get_entity(address):
        if address == '+42777':
            raise ValueError('not a contact')
        return NS(id=777000)
    c.get_entity.side_effect = get_entity
    async def dialogs():
        yield NS(entity=types.User(id=888222, phone='42777'))
    c.iter_dialogs = dialogs
    sources, diagnostics = await code_sources(c)
    assert not diagnostics
    assert sources[1].entity.id == 888222
    assert sources[1].entity.id != 42777
