import io
import json
import logging
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from goalcal import auth, db, feedback, tenancy, telemetry, diagnostics
from goalcal.models import Interaction
from goalcal.logging_setup import DiagnosticFormatter


@pytest.fixture
def trial_accounts():
    db.init_db()
    password = 'trial-feedback-password'
    names = ['feedback_' + uuid.uuid4().hex[:10] for _ in range(2)]
    ids = [auth.create_user(name, password, admin=True) for name in names]
    tokens = [auth.login(name, password) for name in names]
    return names, ids, tokens


def test_feedback_is_persistent_and_scoped(trial_accounts):
    names, ids, tokens = trial_accounts
    with tenancy.session_scope(tokens[0]) as s:
        fid = feedback.submit(s, feedback.CATEGORIES[0], 'Calendar drag did not update the time', 'a'*16)
    with tenancy.session_scope(tokens[1]) as s:
        assert feedback.list_feedback(s, feedback_id=fid) == []
        with pytest.raises(ValueError, match='not found'):
            feedback.resolve(s, fid)
    auth.logout(tokens[0])
    replacement = auth.login(names[0], 'trial-feedback-password')
    with tenancy.session_scope(replacement) as s:
        row = feedback.list_feedback(s, feedback_id=fid)[0]
        assert row['message'] == 'Calendar drag did not update the time'
        assert row['operation_id'] == 'a'*16 and row['status'] == 'open'
        feedback.resolve(s, fid)
    with tenancy.session_scope(replacement) as s:
        assert feedback.list_feedback(s, feedback_id=fid)[0]['status'] == 'resolved'


def test_feedback_validates_and_does_not_log_body(trial_accounts):
    capture = io.StringIO()
    handler = logging.StreamHandler(capture)
    handler.setFormatter(DiagnosticFormatter())
    log = logging.getLogger('goalcal.diagnostics')
    log.addHandler(handler)
    old_level = log.level; log.setLevel(logging.INFO)
    try:
        with tenancy.session_scope(trial_accounts[2][0]) as s:
            for category, text, reference in [('bad', 'valid message', None),
                                              (feedback.CATEGORIES[0], 'abc', None),
                                              (feedback.CATEGORIES[0], 'x'*2001, None),
                                              (feedback.CATEGORIES[0], 'valid message', 'https://secret.example/token')]:
                with pytest.raises(ValueError):
                    feedback.submit(s, category, text, reference)
            fid = feedback.submit(s, feedback.CATEGORIES[0], 'Private calendar details that must not appear in a log')
        output = capture.getvalue()
        assert 'feedback_submitted' in output
        assert 'Private calendar' not in output
        assert f'"feedback_id": {fid}' in output
    finally:
        log.removeHandler(handler); log.setLevel(old_level)


def test_operation_errors_are_correlated_without_exception_contents():
    capture = io.StringIO(); handler = logging.StreamHandler(capture)
    handler.setFormatter(DiagnosticFormatter())
    log = logging.getLogger('goalcal.diagnostics')
    old_level = log.level; log.setLevel(logging.INFO); log.addHandler(handler)
    try:
        with pytest.raises(ValueError):
            with telemetry.operation('calendar.move', 42) as operation_id:
                raise ValueError('secret-password and calendar contents')
        row = json.loads(capture.getvalue())
        assert row['operation_id'] == operation_id
        assert row['user_id'] == 42 and row['error_type'] == 'ValueError'
        assert row['duration_ms'] >= 0 and row['frames']
        assert 'secret-password' not in capture.getvalue()
        assert 'calendar contents' not in capture.getvalue()
    finally:
        log.removeHandler(handler); log.setLevel(old_level)


def test_diagnostics_summary_excludes_feedback_body(trial_accounts, tmp_path, monkeypatch):
    from goalcal.storage import database_path
    with tenancy.session_scope(trial_accounts[2][0]) as s:
        fid = feedback.submit(s, feedback.CATEGORIES[1], 'Private feedback body for maintainers only')
    logdir = tmp_path / 'logs'; logdir.mkdir()
    from datetime import datetime, timezone
    (logdir / 'application.log').write_text(json.dumps({
        'time': datetime.now(timezone.utc).isoformat(), 'event': 'operation_failed',
        'operation': 'calendar.move', 'operation_id': 'b'*16, 'user_id': trial_accounts[1][0],
        'duration_ms': 50, 'error_type': 'ValueError', 'frames': []})+'\n')
    monkeypatch.setattr(diagnostics, 'database_path', lambda: tmp_path/'db.sqlite')
    with db.session_scope() as s:
        report = diagnostics.report(s)
    assert report['operations']['calendar.move']['failed'] == 1
    assert report['operations']['calendar.move']['p95_ms'] == 50
    assert any(row['id'] == fid for row in report['feedback'])
    assert 'Private feedback body' not in json.dumps(report)


def test_invite_accepts_copy_paste_whitespace():
    db.init_db()
    invitation = auth.invite()
    uid = auth.create_user('copypaste_'+uuid.uuid4().hex[:8], 'copypaste-password', invitation=' '+invitation+'\n')
    assert uid
