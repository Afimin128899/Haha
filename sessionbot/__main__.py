import asyncio
import fcntl
import logging
import os
import shutil
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, BotCommand

from . import __version__
from .config import ConfigError, load_settings
from .engine import Engine, session_fingerprint
from .files import ingest
from .storage import Store

MODES = {'check': 'Проверить авторизацию', 'spam': 'Проверить через @SpamBot',
         'terminate': 'Завершить другие сеансы', 'clear_contacts': 'Очистка: все контакты',
         'clear_drafts': 'Очистка: все облачные черновики',
         'migrate': 'Миграция в новую авторизацию'}


STATES = {'draft': 'выбор режима', 'queued': 'в очереди', 'running': 'выполняется',
          'done': 'готово', 'failed': 'ошибка', 'blocked': 'ограничение Telegram',
          'cancelled': 'отменена', 'interrupted': 'прервана перезапуском', 'expired': 'срок хранения истёк'}

def keyboard(ident):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=f'mode:{ident}:{mode}')] for mode, label in MODES.items()])


async def main():
    os.umask(0o077)
    settings = load_settings()
    allowed = settings.allowed_ids
    root = settings.data_dir
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = open(root / 'polling.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('На этом диске уже запущен процесс. Локальная блокировка не защищает другие серверы.') from None
    store = Store(root)
    shutil.rmtree(root / 'work', ignore_errors=True)
    shutil.rmtree(root / 'staging', ignore_errors=True)
    for temporary in (root / 'results').glob('*.tmp'):
        temporary.unlink(missing_ok=True)
    referenced = {Path(r[0]).parent for r in store.db.execute('SELECT source FROM jobs')}
    for folder in (root / 'uploads').glob('*'):
        if folder.is_dir() and folder not in referenced:
            shutil.rmtree(folder, ignore_errors=True)
    bot = Bot(settings.bot_token)
    concurrency = settings.concurrency
    retention = settings.retention_days
    limit = settings.max_upload_mb * 1024 * 1024
    engine = Engine(store, bot, settings.api_id, settings.api_hash, concurrency, retention)
    dispatcher = Dispatcher()

    @dispatcher.message.outer_middleware()
    async def private_only(handler, event, data):
        if event.from_user and event.from_user.id in allowed and event.chat.type == 'private':
            return await handler(event, data)

    @dispatcher.callback_query.outer_middleware()
    async def private_callback(handler, event, data):
        if event.from_user.id in allowed and event.message and event.message.chat.type == 'private':
            return await handler(event, data)
        await event.answer('Доступ запрещён', show_alert=True)

    @dispatcher.message(Command('start', 'help'))
    async def help_message(message):
        await message.answer('Загрузите .session или ZIP с парами .session + .json. Затем выберите отдельный режим для каждой задачи.\n'
                             'Обычная проверка ничего не удаляет. Выберите удаление контактов или облачных черновиков отдельным действием.\n'
                             '/status — задачи; /pause — приостановить запуск очереди; /resume — продолжить; /cancel [ID] — отменить; '
                             '/archives — сохранённые результаты; /settings — настройки; /version — версия.\n'
                             'Пауза не прерывает уже выполняющиеся операции. Отмена не отменяет выполненные действия Telegram.\n'
                             'Коды и 2FA вводятся через /input ID значение. Передача сессии или пароля боту даёт серверу доступ к аккаунту. '
                             'Используйте только свои аккаунты. Доставку кода определяет Telegram.')

    @dispatcher.message(Command('version'))
    async def version(message):
        commit = os.getenv('RAILWAY_GIT_COMMIT_SHA', 'не задан')
        await message.answer(f'Версия {__version__}\nRailway commit: {commit}')

    @dispatcher.message(Command('settings'))
    async def settings(message):
        await message.answer(f'Параллельность: {concurrency}\nХранение: {retention} дней\nМаксимум загрузки: {limit // 1048576} МБ\n'
                             'Изменение через переменные Railway. API_ID/API_HASH берутся только из окружения. JSON: параметры устройства, proxy, password/twoFA/two_fa.')

    @dispatcher.message(Command('status'))
    async def status(message):
        rows = store.rows(message.from_user.id)[:30]
        controls = [[InlineKeyboardButton(text='Пауза', callback_data='control:pause'),
                     InlineKeyboardButton(text='Продолжить', callback_data='control:resume')]]
        controls.extend([[InlineKeyboardButton(text=f'Отменить {r["id"]}', callback_data=f'control:cancel:{r["id"]}')]
                         for r in rows if r['state'] in {'running', 'queued', 'draft'}])
        await message.answer('\n'.join(f'{r["id"]}: {STATES.get(r["state"], r["state"])} — {r["stage"]}' for r in rows)[:4000] or 'Задач нет',
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=controls))

    @dispatcher.message(Command('pause', 'resume'))
    async def pause(message):
        value = message.text.split()[0].split('@')[0] == '/pause'
        store.pause(message.from_user.id, value)
        await message.answer('Запуск новых задач приостановлен' if value else 'Очередь продолжена')

    @dispatcher.message(Command('cancel'))
    async def cancel(message):
        args = message.text.split(maxsplit=1)
        engine.cancel(message.from_user.id, args[1].strip() if len(args) > 1 else None)
        await message.answer('Отмена запрошена. Уже выполненные действия Telegram необратимы.')

    @dispatcher.message(Command('input'))
    async def manual(message):
        parts = message.text.split(maxsplit=2)
        accepted = len(parts) == 3 and engine.answer(message.from_user.id, parts[1], parts[2])
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer('Ответ передан задаче' if accepted else 'Нет ожидающего ввода для этой задачи')

    @dispatcher.message(Command('archives'))
    async def archives(message):
        rows = [r for r in store.rows(message.from_user.id) if r['result'] and Path(r['result']).is_file()]
        if not rows:
            await message.answer('Сохранённых результатов нет')
        for row in rows:
            await engine.deliver(row)

    @dispatcher.message(F.document)
    async def upload(message):
        doc = message.document
        if not doc.file_size or doc.file_size > limit:
            await message.answer('Файл пуст или превышает лимит')
            return
        name = doc.file_name or ''
        if Path(name).suffix.lower() not in {'.session', '.zip'}:
            await message.answer('Разрешены .session и .zip')
            return
        staging = root / 'staging' / uuid.uuid4().hex
        staging.mkdir(parents=True)
        try:
            source = staging / ('upload' + Path(name).suffix.lower())
            await bot.download(doc, destination=source)
            entries = ingest(source, staging / 'extracted', limit)
            for session, config in entries:
                folder = root / 'uploads' / uuid.uuid4().hex
                folder.mkdir(parents=True)
                dest = folder / 'account.session'
                shutil.copyfile(session, dest)
                ident = store.add(message.from_user.id, session_fingerprint(dest), dest, config, 'check')
                store.update(ident, state='draft', stage='Выберите режим')
                await message.answer(f'Задача {ident}. Выберите режим:', reply_markup=keyboard(ident))
        except Exception as exc:
            await message.answer(f'Загрузка отклонена ({type(exc).__name__}). Проверьте формат, пары файлов и лимит размера.')
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @dispatcher.callback_query(F.data.startswith('control:'))
    async def control(callback):
        parts = callback.data.split(':')
        if parts[1] == 'cancel' and len(parts) == 3:
            engine.cancel(callback.from_user.id, parts[2])
        elif parts[1] in {'pause', 'resume'}:
            store.pause(callback.from_user.id, parts[1] == 'pause')
        await callback.answer('Принято')

    @dispatcher.callback_query(F.data.startswith('mode:'))
    async def choose(callback):
        _, ident, mode = callback.data.split(':')
        job = store.get(ident, callback.from_user.id)
        if not job or job['state'] != 'draft' or mode not in MODES:
            await callback.answer('Задача недоступна')
            return
        await callback.answer()
        if mode in {'terminate', 'clear_contacts', 'clear_drafts'}:
            actions = {
                'terminate': 'Завершить все другие авторизации. Загруженную сохранить.',
                'clear_contacts': 'Удалить ВСЕ контакты аккаунта. Это необратимо.',
                'clear_drafts': 'Удалить ВСЕ облачные черновики сообщений аккаунта. Это необратимо.',
            }[mode]
            buttons = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text='Подтверждаю', callback_data=f'confirm:{ident}:{mode}'),
                InlineKeyboardButton(text='Назад', callback_data=f'back:{ident}')]])
            await callback.message.answer(f'Действия: {actions}', reply_markup=buttons)
        else:
            store.db.execute("UPDATE jobs SET mode=?,state='queued',stage='Ожидание' WHERE id=? AND state='draft'", (mode, ident))
            await callback.message.answer(f'Задача {ident} поставлена в очередь')

    @dispatcher.callback_query(F.data.startswith('back:'))
    async def back(callback):
        ident = callback.data.split(':')[1]
        if store.get(ident, callback.from_user.id):
            await callback.message.answer('Выберите режим', reply_markup=keyboard(ident))
        await callback.answer()

    @dispatcher.callback_query(F.data.startswith('confirm:'))
    async def confirm(callback):
        _, ident, mode = callback.data.split(':')
        job = store.get(ident, callback.from_user.id)
        if job and job['state'] == 'draft' and mode in {'terminate', 'clear_contacts', 'clear_drafts'}:
            store.db.execute("UPDATE jobs SET mode=?,state='queued',stage='Подтверждено пользователем' WHERE id=? AND state='draft'", (mode, ident))
            await callback.message.answer(f'Задача {ident} поставлена в очередь')
        await callback.answer()

    commands = {'start': 'Начало', 'help': 'Помощь', 'status': 'Статус', 'pause': 'Пауза очереди',
                'resume': 'Продолжить', 'cancel': 'Отменить', 'archives': 'Результаты', 'settings': 'Настройки', 'version': 'Версия'}
    worker = asyncio.create_task(engine.loop())
    try:
        await bot.set_my_commands([BotCommand(command=k, description=v) for k, v in commands.items()])
        # Explicit polling allows a conflict to stop the service with a clear diagnosis.
        offset = None
        while True:
            if worker.done():
                await worker  # Surface an unexpected queue failure instead of silent polling.
            try:
                updates = await bot.get_updates(offset=offset, timeout=30, allowed_updates=['message', 'callback_query'])
            except TelegramConflictError:
                logging.error('TelegramConflictError: один токен нельзя использовать одновременно в нескольких polling-процессах. Остановите копии на других серверах; локальная блокировка их не защищает.')
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
                continue
            except TelegramNetworkError:
                await asyncio.sleep(3)
                continue
            for update in updates:
                try:
                    await dispatcher.feed_update(bot, update)
                except Exception as exc:
                    logging.error('Ошибка обработчика: %s', type(exc).__name__)
                offset = update.update_id + 1
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await engine.close()
        await bot.session.close()
        store.db.close()
        lock.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
    logging.getLogger('telethon').disabled = True
    logging.getLogger('telethon').setLevel(logging.CRITICAL)
    try:
        asyncio.run(main())
    except ConfigError as exc:
        logging.error('Ошибка настройки: %s', exc)
        raise SystemExit(2) from None
