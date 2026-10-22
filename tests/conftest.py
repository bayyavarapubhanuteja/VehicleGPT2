import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("VEHICLEGPT_LLM", "none")
os.environ.setdefault("VEHICLEGPT_NET_PROBE", "0")
os.environ.setdefault("VEHICLEGPT_ENV", "development")
PW = "Demo-Pass-2026"


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("db") / "test.db"
    from vehiclegpt import config
    config.DB_PATH = p
    config.GROUND_TRUTH_PATH = p.parent / "ground_truth.json"
    from vehiclegpt import runtime
    runtime.ensure_database(p)
    return p


@pytest.fixture()
def conn(db_path):
    from vehiclegpt import db
    c = db.connect(db_path)
    yield c
    c.close()


def login(conn, name, portal):
    from vehiclegpt import auth
    return auth.authenticate(conn, name, PW, portal)


@pytest.fixture()
def alice(conn):
    return login(conn, "alice", "driver")


@pytest.fixture()
def bob(conn):
    return login(conn, "bob", "driver")


@pytest.fixture()
def chen(conn):
    return login(conn, "eng.chen", "engineer")


@pytest.fixture()
def rao(conn):
    return login(conn, "eng.rao", "engineer")


@pytest.fixture()
def admin(conn):
    return login(conn, "admin", "engineer")
