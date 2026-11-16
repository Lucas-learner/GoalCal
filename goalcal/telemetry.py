"""Content-free diagnostics for local trial maintenance."""
from contextlib import contextmanager
import logging
import time
import traceback
from pathlib import Path
from uuid import uuid4

log = logging.getLogger('goalcal.diagnostics')


def emit(event, **fields):
    log.info(event, extra={'diagnostic': fields})


@contextmanager
def operation(name, user_id):
    operation_id = uuid4().hex[:16]
    started = time.perf_counter()
    try:
        yield operation_id
    except Exception as exc:
        # Keep code locations, never exception text, local variables or SQL values.
        frames = [{'file': Path(frame.filename).name, 'line': frame.lineno,
                   'function': frame.name} for frame in traceback.extract_tb(exc.__traceback__)[-6:]]
        emit('operation_failed', operation=name, operation_id=operation_id,
             user_id=user_id, duration_ms=round((time.perf_counter()-started)*1000, 1),
             error_type=type(exc).__name__, frames=frames)
        raise
    else:
        emit('operation_completed', operation=name, operation_id=operation_id,
             user_id=user_id, duration_ms=round((time.perf_counter()-started)*1000, 1))
