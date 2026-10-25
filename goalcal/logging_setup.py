import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from .storage import database_path


class DiagnosticFormatter(logging.Formatter):
    def format(self, record):
        payload = {'time': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                   'level': record.levelname, 'logger': record.name,
                   'event': record.getMessage()}
        payload.update(getattr(record, 'diagnostic', {}))
        # Exception strings can contain prompts, URLs or SQL parameters; callers
        # explicitly emit error classes and code locations instead.
        return json.dumps(payload, ensure_ascii=False)


def configure_logging():
    log = logging.getLogger('goalcal')
    if log.handlers:
        return
    directory = database_path().parent / 'logs'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(directory / 'application.log', maxBytes=2_000_000, backupCount=5)
    handler.setFormatter(DiagnosticFormatter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    log.info('application_started')
