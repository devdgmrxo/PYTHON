import pytest

from app.database.db import init_db


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def conn(db_path):
    connection = init_db(db_path)
    yield connection
    connection.close()
