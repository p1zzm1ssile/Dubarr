import os
import stat

import utils.config as config
from utils.logger import logger, redact


def test_keys_and_passwords_never_reach_the_log():
    key = sorted(config.SECRET_KEYS)[0]
    old = config.values.get(key)
    config.values[key] = "s3cret-value-123"
    try:
        record = {"message": "Can't reach http://sab:8080/api?mode=queue&apikey=abcdef&output=json, key s3cret-value-123"}
        redact(record)
        assert "abcdef" not in record["message"] and "s3cret-value-123" not in record["message"]
        assert "apikey=***&output=json" in record["message"]
    finally:
        config.values[key] = old


def test_the_log_file_can_only_be_read_by_the_app():
    logger.info("hello")
    logger.complete()
    assert stat.S_IMODE(os.stat(config.log_file).st_mode) == 0o600
