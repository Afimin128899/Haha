"""Validate environment before opening storage or making network requests."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from aiogram.utils.token import TokenValidationError, validate_token


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    allowed_ids: frozenset[int]
    bot_token: str = field(repr=False)
    api_id: int
    api_hash: str = field(repr=False)
    data_dir: Path
    concurrency: int
    retention_days: int
    max_upload_mb: int


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    required = ('ALLOWED_IDS', 'BOT_TOKEN', 'API_ID', 'API_HASH')
    missing = [key for key in required if not env.get(key, '').strip()]
    if missing:
        raise ConfigError('Не заданы переменные: ' + ', '.join(missing) +
                          '. В Railway откройте сервис → Variables, добавьте значения и примените изменения. '
                          'Файл .env в GitHub не задаёт переменные Railway. Секреты храните только в Variables.')
    raw_ids = [value.strip() for value in env['ALLOWED_IDS'].split(',')]
    if any(not value.isascii() or not value.isdecimal() or int(value) <= 0 for value in raw_ids):
        raise ConfigError('ALLOWED_IDS: укажите положительные числовые Telegram ID через запятую, без @username и номера телефона.')
    token = env['BOT_TOKEN'].strip()
    try:
        validate_token(token)
    except TokenValidationError:
        raise ConfigError('BOT_TOKEN: неверный формат токена. Получите токен через BotFather и задайте его в Railway Variables.') from None

    def integer(name, default=None, minimum=1, maximum=None):
        raw = env.get(name, str(default) if default is not None else '').strip()
        if not raw.isascii() or not raw.isdecimal():
            raise ConfigError(f'{name}: требуется целое положительное число.')
        value = int(raw)
        if value < minimum or (maximum is not None and value > maximum):
            raise ConfigError(f'{name}: допустимый диапазон {minimum}..{maximum or "без верхней границы"}.')
        return value

    data_dir = env.get('DATA_DIR', '/data').strip()
    if not data_dir:
        raise ConfigError('DATA_DIR: укажите путь подключения постоянного Volume, обычно /data.')
    return Settings(frozenset(map(int, raw_ids)), token, integer('API_ID'), env['API_HASH'].strip(),
                    Path(data_dir).resolve(), integer('CONCURRENCY', 2, maximum=16),
                    integer('RETENTION_DAYS', 7, maximum=365), integer('MAX_UPLOAD_MB', 20, maximum=20))
