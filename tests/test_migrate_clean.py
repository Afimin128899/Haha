"""Offline checks of the Haha workflow; sessions are inert byte fixtures."""
import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock
import zipfile

import pytest
from telethon import errors, functions

from sessionbot import engine as module
from sessionbot.checkpoints import persist_new_session
from sessionbot.cleanup import CleanReport, deep_clean_account
from sessionbot.storage import Store


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path)
    upload = tmp_path / 'uploads' / 'fixture'
    upload.mkdir(parents=True)
    source = upload / 'account.session'
    source.write_bytes(b'original-source-fixture')
    ident = store.add(123, 'fingerprint', source,
                      {'device_model':'Fixture', 'password':'never-export', 'proxy':{'password':'never-export'}},
                      'migrate_clean')
    old, new = AsyncMock(return_value=True), AsyncMock(return_value=True)
    for client, key in ((old, b'o' * 256), (new, b'n' * 256)):
        client.session = NS(auth_key=NS(key=key))
        client.is_user_authorized.return_value = True
        client.get_me.return_value = NS(id=123, deleted=False, phone='10000000000', username='')
    events = []
    def factory(path, *args):
        if path.name == 'source.session':
            return old
        path.write_bytes(b'new-authorization-fixture')
        return new
    async def clean(client, reporter):
        assert client is new
        result = Path(store.get(ident)['result'])
        assert result.exists() and result.parent.name == 'before_cleanup'
        events.append('clean-new')
        return CleanReport()
    cleaner = AsyncMock(side_effect=clean)
    async def source_rpc(request):
        if isinstance(request, functions.auth.LogOutRequest):
            result = Path(store.get(ident)['result'])
            assert result.is_file() and result.parent.name == 'after_cleanup'
            assert events == ['clean-new']
            with zipfile.ZipFile(result) as archive:
                assert archive.read('account.session') == b'new-authorization-fixture'
            events.append('logout-source')
        return True
    old.side_effect = source_rpc
    monkeypatch.setattr(module, 'make_client', factory)
    monkeypatch.setattr(module, 'session_fingerprint', lambda path: 'fingerprint')
    monkeypatch.setattr(module, 'migrate', AsyncMock())
    monkeypatch.setattr(module, 'deep_clean_account', cleaner)
    bot = NS(send_message=AsyncMock(), send_document=AsyncMock())
    engine = module.Engine(store, bot, 123, 'offline_hash')
    return NS(root=tmp_path, store=store, ident=ident, source=source, old=old, new=new,
              events=events, cleaner=cleaner, bot=bot, engine=engine)


async def test_combined_order_and_new_sessions_survive(setup):
    s = setup
    await s.engine.run_job(s.store.claim())
    assert s.store.get(s.ident)['state'] == 'done'
    assert s.events == ['clean-new', 'logout-source']
    s.new.assert_not_awaited()  # No logout or ResetAuthorizations against the new client.
    assert s.source.read_bytes() == b'original-source-fixture'
    assert not (s.root / 'work' / s.ident).exists()
    assert len(list((s.root / 'new_sessions').rglob('account.session'))) == 2
    for path in (s.root / 'new_sessions').rglob('account.json'):
        assert 'never-export' not in path.read_text()
        assert json.loads(path.read_text())['device_model'] == 'Fixture'


async def test_delivery_failure_preserves_result_and_repeat_delivery(setup):
    s = setup
    s.bot.send_document.side_effect = OSError('synthetic error')
    await s.engine.run_job(s.store.claim())
    job = s.store.get(s.ident)
    assert job['state'] == 'done' and not job['delivered']
    assert Path(job['result']).exists()
    s.bot.send_document.side_effect = None
    await s.engine.deliver(job)
    assert s.store.get(s.ident)['delivered']


@pytest.mark.parametrize('stage', ['initial_file', 'final_file', 'initial_index', 'final_index'])
async def test_storage_failure_never_revokes_source_and_keeps_new_files(setup, monkeypatch, stage):
    s = setup
    if stage.endswith('file'):
        def persist(*args):
            if (stage == 'initial_file' and args[3] == 'before_cleanup') or (stage == 'final_file' and args[3] == 'after_cleanup'):
                raise OSError('synthetic disk full')
            return persist_new_session(*args)
        monkeypatch.setattr(module, 'persist_new_session', persist)
    else:
        update = s.store.update
        def fail_result(ident, **values):
            if 'result' in values and ('before_cleanup' if stage == 'initial_index' else 'after_cleanup') in values['result']:
                raise OSError('synthetic index error')
            return update(ident, **values)
        monkeypatch.setattr(s.store, 'update', fail_result)
    await s.engine.run_job(s.store.claim())
    assert s.store.get(s.ident)['state'] == 'failed'
    assert not any(isinstance(call.args[0], functions.auth.LogOutRequest) for call in s.old.await_args_list)
    assert any(path.read_bytes() == b'new-authorization-fixture' for path in s.root.rglob('*.session'))
    if stage.startswith('initial'):
        s.cleaner.assert_not_awaited()


async def test_partial_cleanup_keeps_source_and_saved_new_result(setup):
    s = setup
    s.cleaner.side_effect = None
    s.cleaner.return_value = CleanReport(errors=['SyntheticError'], dialogs_failed=1)
    await s.engine.run_job(s.store.claim())
    assert s.store.get(s.ident)['state'] == 'failed'
    assert not any(isinstance(call.args[0], functions.auth.LogOutRequest) for call in s.old.await_args_list)
    assert Path(s.store.get(s.ident)['result']).exists()


@pytest.mark.parametrize('stage', ['cleanup', 'source_logout'])
async def test_floodwait_does_not_retry_and_sets_persistent_cooldown(setup, stage):
    s = setup
    if stage == 'cleanup':
        s.cleaner.side_effect = errors.FloodWaitError(None, capture=90)
    else:
        async def rpc(request):
            if isinstance(request, functions.auth.LogOutRequest):
                raise errors.FloodWaitError(None, capture=90)
            return True
        s.old.side_effect = rpc
    await s.engine.run_job(s.store.claim())
    job = s.store.get(s.ident)
    assert job['state'] == 'blocked'
    assert s.store.remaining(job['account']) > 0
    assert Path(job['result']).exists()
    assert sum(isinstance(c.args[0], functions.auth.LogOutRequest) for c in s.old.await_args_list) == int(stage == 'source_logout')


@pytest.mark.parametrize('stage', ['migration', 'cleanup'])
async def test_cancel_keeps_new_session_and_releases_account_lock(setup, monkeypatch, stage):
    s = setup
    started = asyncio.Event()
    async def block(*args):
        started.set()
        await asyncio.Event().wait()
    if stage == 'migration':
        monkeypatch.setattr(module, 'migrate', block)
    else:
        s.cleaner.side_effect = block
    task = asyncio.create_task(s.engine.run_job(s.store.claim()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert s.store.get(s.ident)['state'] == 'cancelled'
    assert not any(isinstance(c.args[0], functions.auth.LogOutRequest) for c in s.old.await_args_list)
    assert any(p.read_bytes() == b'new-authorization-fixture' for p in s.root.rglob('*.session'))
    assert s.new.disconnect.await_count >= 1 and s.old.disconnect.await_count >= 1
    assert not s.store.db.execute("SELECT 1 FROM jobs WHERE state='running'").fetchone()


async def test_retention_keeps_new_sessions_but_expires_uploaded_secrets(setup):
    s = setup
    await s.engine.run_job(s.store.claim())
    result = Path(s.store.get(s.ident)['result'])
    s.store.db.execute('UPDATE jobs SET created=0')
    s.engine.expire()
    assert result.exists()
    assert len(list((s.root / 'new_sessions').rglob('account.session'))) == 2
    assert s.store.get(s.ident)['config'] == '{}'
    assert not s.source.exists()


async def test_restart_recovers_unindexed_checkpoint_without_replay(setup, monkeypatch):
    s = setup
    update = s.store.update
    def fail_result(ident, **values):
        if 'result' in values:
            raise OSError('synthetic disk failure')
        return update(ident, **values)
    monkeypatch.setattr(s.store, 'update', fail_result)
    await s.engine.run_job(s.store.claim())
    s.store.db.close()
    recovered = Store(s.root)
    assert recovered.get(s.ident)['result']
    assert recovered.get(s.ident, 999) is None
    assert recovered.claim() is None


async def test_real_cleanup_function_never_resets_app_authorizations(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(module, 'deep_clean_account', deep_clean_account)
    requested = []
    async def new_rpc(request, **kwargs):
        name = type(request).__name__
        requested.append(name)
        assert name not in {'LogOutRequest', 'ResetAuthorizationsRequest'}
        return {'GetUserPhotosRequest':NS(photos=[]), 'GetContactsRequest':NS(users=[]),
                'GetDialogFiltersRequest':NS(filters=[]), 'GetAllStickersRequest':NS(sets=[])}.get(name, True)
    async def dialogs():
        if False:
            yield
    s.new.side_effect = new_rpc
    s.new.iter_dialogs = dialogs
    async def source_rpc(request):
        if isinstance(request, functions.auth.LogOutRequest):
            assert 'DeleteHistoryRequest' in requested
            assert 'ResetWebAuthorizationsRequest' in requested
            assert Path(s.store.get(s.ident)['result']).parent.name == 'after_cleanup'
        return True
    s.old.side_effect = source_rpc
    await s.engine.run_job(s.store.claim())
    assert s.store.get(s.ident)['state'] == 'done'
    assert 'UpdateProfileRequest' in requested


async def test_wrong_account_or_reused_key_cannot_trigger_cleanup(setup):
    s = setup
    s.new.session.auth_key.key = s.old.session.auth_key.key
    await s.engine.run_job(s.store.claim())
    assert s.store.get(s.ident)['state'] == 'failed'
    s.cleaner.assert_not_awaited()
    assert not any(isinstance(c.args[0], functions.auth.LogOutRequest) for c in s.old.await_args_list)


async def test_combined_requires_explicit_confirmation_in_real_dispatcher(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from aiogram import Bot
    from aiogram.types import Update, CallbackQuery, Message, Chat, User
    from aiogram.exceptions import TelegramConflictError
    from aiogram.methods import GetUpdates
    from sessionbot import __main__ as app
    from sessionbot.config import load_settings
    settings = load_settings({'ALLOWED_IDS':'123', 'BOT_TOKEN':'123456:synthetic_test_token',
                              'API_ID':'123', 'API_HASH':'offline', 'DATA_DIR':str(tmp_path)})
    store = Store(tmp_path)
    ident = store.add(123, 'fixture', tmp_path / 'fixture.session', {}, 'check')
    store.update(ident, state='draft')
    bot = Bot(settings.bot_token)
    user = User(id=123, is_bot=False, first_name='Fixture')
    message = Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=123, type='private'), text='Выберите режим')
    data = ['mode:' + ident + ':migrate_clean', 'confirm:' + ident + ':migrate_clean']
    counter = 0
    async def updates(**kwargs):
        nonlocal counter
        if counter == 1:
            assert store.get(ident)['state'] == 'draft'
        if counter == 2:
            assert store.get(ident)['state'] == 'queued'
            assert store.get(ident)['mode'] == 'migrate_clean'
            raise TelegramConflictError(method=GetUpdates(), message='stop offline test')
        index = counter
        counter += 1
        return [Update(update_id=index+1, callback_query=CallbackQuery(
            id=str(index), from_user=user, chat_instance='fixture', message=message, data=data[index]))]
    # The filter is pure/synchronous. Keep this handler test independent of
    # executor scheduling; the actual aiogram filter and handlers still run.
    async def inline_filter(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', inline_filter)
    requests = []
    async def request(bot, method, **kwargs):
        requests.append(method)
        return message if type(method).__name__ == 'SendMessage' else True
    async def idle():
        await asyncio.Event().wait()
    monkeypatch.setattr(app, 'load_settings', lambda: settings)
    monkeypatch.setattr(app, 'Bot', lambda token: bot)
    monkeypatch.setattr(app, 'Engine', lambda *args: NS(loop=idle, close=AsyncMock()))
    monkeypatch.setattr(bot, 'get_me', AsyncMock(return_value=NS(username='fixture_bot')))
    monkeypatch.setattr(bot, 'get_webhook_info', AsyncMock(return_value=NS(url='')))
    monkeypatch.setattr(bot, 'set_my_commands', AsyncMock())
    monkeypatch.setattr(bot, 'get_updates', updates)
    monkeypatch.setattr(bot.session, 'make_request', request)
    await asyncio.wait_for(app.main(), timeout=3)
    plans = [r.text for r in requests if type(r).__name__ == 'SendMessage']
    assert any('ТОЛЬКО загруженная старая' in text and '30 дней' in text for text in plans)
    store.db.close()
