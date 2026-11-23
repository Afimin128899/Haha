import logging
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError, TelegramNetworkError
from aiogram.methods import GetUpdates, GetMe

from sessionbot import __main__ as app
from sessionbot.config import ConfigError, load_settings
from test_config import VALID


async def exercise(monkeypatch, tmp_path, *, failure=None, webhook=''):
    settings = load_settings({**VALID, 'DATA_DIR': str(tmp_path)})
    bot = NS(get_me=AsyncMock(return_value=NS(username='synthetic_test_bot'), side_effect=failure),
             get_webhook_info=AsyncMock(return_value=NS(url=webhook)),
             set_my_commands=AsyncMock(),
             get_updates=AsyncMock(side_effect=TelegramConflictError(method=GetUpdates(), message='synthetic-conflict')),
             session=NS(close=AsyncMock()))
    monkeypatch.setattr(app, 'load_settings', lambda: settings)
    monkeypatch.setattr(app, 'Bot', lambda token: bot)
    try:
        await app.main()
    finally:
        bot.session.close.assert_awaited_once()
    return bot


async def test_startup_identifies_bot_and_conflict_without_secret(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    bot = await exercise(monkeypatch, tmp_path)
    assert '@synthetic_test_bot' in caplog.text
    assert 'Polling начат' in caplog.text
    assert 'TelegramConflictError' in caplog.text
    assert 'synthetic_test_token' not in caplog.text
    bot.get_updates.assert_awaited_once()


@pytest.mark.parametrize('failure,expected', [
    (TelegramUnauthorizedError(method=GetMe(), message='do-not-log-secret'), 'BOT_TOKEN отклонён'),
    (TelegramNetworkError(method=GetMe(), message='do-not-log-secret'), 'Не удалось подключиться')])
async def test_startup_errors_are_actionable_without_secret(monkeypatch, tmp_path, failure, expected):
    with pytest.raises(ConfigError) as exc:
        await exercise(monkeypatch, tmp_path, failure=failure)
    assert expected in str(exc.value)
    assert 'do-not-log-secret' not in str(exc.value)


async def test_webhook_is_diagnosed_without_printing_url(monkeypatch, tmp_path):
    with pytest.raises(ConfigError, match='webhook') as exc:
        await exercise(monkeypatch, tmp_path, webhook='https://example.invalid/secret-token')
    assert 'secret-token' not in str(exc.value)


@pytest.mark.parametrize('sender,should_reply', [(123, True), (999, False)])
async def test_real_dispatcher_start_allowlist(monkeypatch, tmp_path, caplog, sender, should_reply):
    from datetime import datetime, timezone
    from aiogram import Bot
    from aiogram.types import Update, Message, Chat, User
    bot = Bot(VALID['BOT_TOKEN'])
    settings = load_settings({**VALID, 'DATA_DIR': str(tmp_path)})
    user = User(id=sender, is_bot=False, first_name='Synthetic')
    chat = Chat(id=sender, type='private')
    incoming = Message(message_id=1, date=datetime.now(timezone.utc), chat=chat, from_user=user, text='/start',
                       entities=[{'type': 'bot_command', 'offset': 0, 'length': 6}])
    monkeypatch.setattr(bot, 'get_me', AsyncMock(return_value=User(id=123456, is_bot=True, first_name='Synthetic', username='synthetic_test_bot')))
    monkeypatch.setattr(bot, 'get_webhook_info', AsyncMock(return_value=NS(url='')))
    monkeypatch.setattr(bot, 'set_my_commands', AsyncMock())
    monkeypatch.setattr(bot, 'get_updates', AsyncMock(side_effect=[
        [Update(update_id=1, message=incoming)],
        TelegramConflictError(method=GetUpdates(), message='stop synthetic test')]))
    response = Message(message_id=2, date=datetime.now(timezone.utc), chat=chat, text='response')
    request = AsyncMock(return_value=response)
    monkeypatch.setattr(bot.session, 'make_request', request)
    monkeypatch.setattr(app, 'load_settings', lambda: settings)
    monkeypatch.setattr(app, 'Bot', lambda token: bot)
    await app.main()
    assert request.await_count == int(should_reply)
    if should_reply:
        assert 'Загрузите' in request.await_args.args[1].text
    else:
        assert 'ID пользователя отсутствует в ALLOWED_IDS' in caplog.text
