import os
import subprocess
import sys

import pytest

from sessionbot.config import ConfigError, load_settings


# Deliberately synthetic values; these are not Telegram credentials.
VALID = {'ALLOWED_IDS': '123, 456', 'BOT_TOKEN': '123456:synthetic_test_token',
         'API_ID': '123', 'API_HASH': 'synthetic_hash'}


def test_settings_defaults_and_secret_repr():
    settings = load_settings(VALID)
    assert settings.allowed_ids == {123, 456}
    assert settings.retention_days == 7
    assert settings.concurrency == 2
    assert 'synthetic' not in repr(settings)


@pytest.mark.parametrize('missing', ['ALLOWED_IDS', 'BOT_TOKEN', 'API_ID', 'API_HASH'])
def test_required_variable_diagnostic_without_values(missing):
    env = VALID.copy()
    env.pop(missing)
    with pytest.raises(ConfigError) as exc:
        load_settings(env)
    assert missing in str(exc.value)
    assert 'Variables' in str(exc.value)
    assert 'synthetic' not in str(exc.value)


@pytest.mark.parametrize('value', ['', ' ', ',', '123,', '@someone', '-100', '0', '1 2', '१२३'])
def test_invalid_allowlist_never_opens_access(value):
    with pytest.raises(ConfigError, match='ALLOWED_IDS'):
        load_settings({**VALID, 'ALLOWED_IDS': value})


@pytest.mark.parametrize('key,value', [('BOT_TOKEN', 'synthetic_invalid_secret'), ('API_ID', 'synthetic_secret'),
                                       ('CONCURRENCY', '0'), ('CONCURRENCY', '17'), ('RETENTION_DAYS', '-1'),
                                       ('MAX_UPLOAD_MB', '21'), ('DATA_DIR', ' ')])
def test_invalid_settings_are_reported_by_name_only(key, value):
    with pytest.raises(ConfigError) as exc:
        load_settings({**VALID, key: value})
    assert key in str(exc.value)
    assert 'synthetic' not in str(exc.value)


def test_startup_missing_allowlist_fails_before_disk_or_network(tmp_path):
    env = os.environ.copy()
    for key in VALID:
        env.pop(key, None)
    env.update({key: value for key, value in VALID.items() if key != 'ALLOWED_IDS'})
    root = tmp_path / 'must_not_be_created'
    env['DATA_DIR'] = str(root)
    process = subprocess.run([sys.executable, '-m', 'sessionbot'], env=env, capture_output=True, text=True, timeout=15)
    assert process.returncode == 2
    assert 'ALLOWED_IDS' in process.stderr and 'Variables' in process.stderr
    assert 'Traceback' not in process.stderr
    assert 'synthetic' not in process.stderr
    assert not root.exists()
