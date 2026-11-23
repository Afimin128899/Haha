import asyncio
import hashlib
import json
import shutil
import time
from pathlib import Path

from telethon import errors, functions
from .files import persist_zip
from .telegram import make_client, authorization, migrate, UserFacingError


class Engine:
    def __init__(self, store, bot, api_id, api_hash, concurrency=2, retention_days=7):
        self.store, self.bot = store, bot
        self.api_id, self.api_hash = api_id, api_hash
        self.concurrency = concurrency
        self.retention = retention_days * 86400
        self.tasks, self.inputs = {}, {}
        self.stopping = False

    async def report(self, job, text):
        self.store.update(job['id'], stage=text)
        try:
            await self.bot.send_message(job['owner'], f"Задача {job['id']}: {text}")
        except Exception:
            pass  # Diagnostic delivery must not invalidate a durable result.

    async def ask(self, job, kind, prompt):
        future = asyncio.get_running_loop().create_future()
        key = (job['owner'], job['id'])
        self.inputs[key] = (kind, future)
        await self.report(job, f'Ожидание {"кода" if kind == "code" else "пароля 2FA"}. {prompt}\nОтветьте: /input {job["id"]} значение\nСообщение будет удалено по возможности.')
        try:
            return await asyncio.wait_for(future, timeout=600)
        except asyncio.TimeoutError:
            raise UserFacingError(f'Таймаут ручного ввода {kind}') from None
        finally:
            self.inputs.pop(key, None)

    def answer(self, owner, ident, text):
        pending = self.inputs.get((owner, ident))
        if pending and not pending[1].done():
            pending[1].set_result(text)
            return True
        return False

    async def deliver(self, job):
        from aiogram.types import FSInputFile
        try:
            await self.bot.send_document(job['owner'], FSInputFile(job['result']), caption=f"Результат {job['id']}")
            self.store.update(job['id'], delivered=1)
        except Exception:
            self.store.update(job['id'], stage='Результат сохранён. Ошибка отправки; используйте /archives')

    async def run_job(self, job):
        ident = job['id']
        work = self.store.root / 'work' / ident
        clients = []
        try:
            work.mkdir(parents=True, exist_ok=True, mode=0o700)
            original = Path(job['source'])
            copied = work / 'source.session'
            shutil.copyfile(original, copied)
            config = json.loads(job['config'])
            old = make_client(copied, config, self.api_id, self.api_hash)
            clients.append(old)
            await self.report(job, 'Подключение')
            await old.connect()
            status = await authorization(old)
            await self.report(job, status)
            if status != 'Активна':
                raise UserFacingError(status)
            # Resolve real account ID before any operation; aliases of a session share a lock.
            me = await old.get_me()
            account = f'tg:{me.id}'
            conflict = self.store.db.execute("SELECT id FROM jobs WHERE account=? AND state='running' AND id<>?", (account, ident)).fetchone()
            if conflict:
                self.store.update(ident, state='queued', stage='Ожидание блокировки аккаунта')
                return
            self.store.db.execute('INSERT INTO aliases VALUES(?,?) ON CONFLICT(fingerprint) DO UPDATE SET account=excluded.account',
                                  (session_fingerprint(original), account))
            self.store.db.execute('UPDATE jobs SET account=? WHERE id=?', (account, ident))
            remaining = self.store.remaining(account)
            if remaining:
                self.store.update(ident, state='queued', stage=f'Ожидание FloodWait: {remaining} с')
                return
            output = copied
            mode = job['mode']
            if mode == 'migrate':
                output = work / 'new.session'
                new = make_client(output, config, self.api_id, self.api_hash)
                clients.append(new)
                await new.connect()
                await migrate(old, new, lambda kind, text: self.ask(job, kind, text), lambda text: self.report(job, text), config)
            elif mode == 'terminate':
                await old(functions.auth.ResetAuthorizationsRequest())
                await self.report(job, 'Другие авторизации завершены; текущая сохранена')
            elif mode == 'spam':
                async with old.conversation('@SpamBot', timeout=30, exclusive=True) as conversation:
                    await conversation.send_message('/start')
                    reply = await conversation.get_response()
                    # Send privately to owner; never log server message contents.
                    await self.bot.send_message(job['owner'], 'Ответ официального @SpamBot:\n' + (reply.raw_text or '')[:3500])
            elif mode == 'clear_drafts':
                await old(functions.messages.ClearAllDraftsRequest())
                await self.report(job, 'Облачные черновики сообщений удалены')
            elif mode == 'clear_contacts':
                contacts = await old(functions.contacts.GetContactsRequest(hash=0))
                if contacts.users:
                    await old(functions.contacts.DeleteContactsRequest(id=contacts.users))
                await self.report(job, 'Контакты удалены')
            elif mode != 'check':
                raise UserFacingError('Неизвестный режим')
            # Flush SQLite and disconnect before archiving, especially the new auth key.
            for client in reversed(clients):
                await client.disconnect()
            clients.clear()
            result = persist_zip(self.store.root / 'results', ident, output, config)
            self.store.update(ident, state='done', stage='Готово', result=result)
            await self.deliver(self.store.get(ident))
        except asyncio.CancelledError:
            if not self.store.get(ident)['result']:
                self.store.update(ident, state='cancelled', stage='Отменена; исходный файл сохранён')
            raise
        except errors.FloodWaitError as exc:
            current_account = self.store.get(ident)['account']
            self.store.cooldown(current_account, exc.seconds)
            self.store.cooldown(job['account'], exc.seconds)
            self.store.update(ident, state='blocked', stage=f'Telegram FloodWait: {exc.seconds} с; автоматического повтора нет')
        except UserFacingError as exc:
            self.store.update(ident, state='failed', stage=str(exc))
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError):
            self.store.update(ident, state='failed', stage='Код неверен или истёк; исходный файл сохранён')
        except errors.PasswordHashInvalidError:
            self.store.update(ident, state='failed', stage='Пароль 2FA неверен; исходный файл сохранён')
        except asyncio.TimeoutError:
            self.store.update(ident, state='failed', stage='Таймаут подключения или ответа Telegram')
        except OSError:
            self.store.update(ident, state='failed', stage='Ошибка подключения или постоянного диска; исходный файл сохранён')
        except Exception as exc:
            # Only class name; exception strings may contain secrets or service text.
            self.store.update(ident, state='failed', stage=f'Ошибка {type(exc).__name__}; исходный файл сохранён')
        finally:
            for client in reversed(clients):
                try:
                    await client.disconnect()
                except Exception:
                    pass
            shutil.rmtree(work, ignore_errors=True)

    def cancel(self, owner, ident=None):
        for job in self.store.rows(owner):
            if (ident is None or ident == job['id']) and job['state'] in {'running', 'queued', 'draft'}:
                self.store.update(job['id'], cancelled=1)
                task = self.tasks.get(job['id'])
                if task:
                    task.cancel()
                else:
                    self.store.update(job['id'], state='cancelled', stage='Отменена')

    def expire(self):
        cutoff = time.time() - self.retention
        for row in self.store.db.execute("SELECT * FROM jobs WHERE created<? AND state NOT IN ('running','queued','expired')", (cutoff,)).fetchall():
            if row['result']:
                Path(row['result']).unlink(missing_ok=True)
            shutil.rmtree(Path(row['source']).parent, ignore_errors=True)
            self.store.db.execute('UPDATE jobs SET config=\'{}\',result=NULL,state=\'expired\',stage=\'Срок хранения истёк\' WHERE id=?', (row['id'],))

    async def loop(self):
        while not self.stopping:
            self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}
            while len(self.tasks) < self.concurrency and (job := self.store.claim()):
                self.tasks[job['id']] = asyncio.create_task(self.run_job(job))
            self.expire()
            await asyncio.sleep(1)

    async def close(self):
        self.stopping = True
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


def session_fingerprint(path):
    import sqlite3
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        key = db.execute('SELECT auth_key FROM sessions').fetchone()[0]
    return hashlib.sha256(key).hexdigest()
