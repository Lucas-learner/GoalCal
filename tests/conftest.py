from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 必须在导入 goalcal.config 之前设置：AppTest 会真正跑 app.py，
# 否则 smoke test 会写进开发用的 goalcal.db。
TEST_DB = Path(tempfile.gettempdir()) / f"goalcal-test-{os.getpid()}.db"
os.environ["GOALCAL_DB_URL"] = f"sqlite:///{TEST_DB}"
os.environ["GOALCAL_TZ"] = "Asia/Shanghai"
os.environ["LLM_ENABLED"] = "false"
os.environ["LLM_API_KEY"] = ""

from goalcal.models import Base  # noqa: E402


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    s = factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def user(session):
    from goalcal.domain.goals import ensure_user

    return ensure_user(session, "test")


@pytest.fixture(autouse=True)
def _reset_global_login_throttle():
    """每个测试后清掉全局登录节流计数：整套测试的登录次数远超 100 次/15 分钟的上限。

    只清 'global' 键，单账号 5 次的限制仍然生效；不影响被测代码。
    """
    yield
    try:
        from goalcal import storage

        with storage.connect() as con:
            con.execute("DELETE FROM login_limits WHERE key='global'")
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_db():
    yield
    from goalcal import db

    if db._engine is not None:
        db._engine.dispose()
    TEST_DB.unlink(missing_ok=True)
