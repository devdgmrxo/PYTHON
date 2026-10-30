import sqlite3

import pytest

from app.database.db import (
    SCHEMA_VERSION,
    DatabaseError,
    TransactionError,
    connect,
    init_db,
    transaction,
)
from app.database.jobs import JobData, ingest_job

TS = "2025-01-01T00:00:00Z"


def _insert_job(conn, title="Dev", url_key=None, text_key=None):
    cur = conn.execute(
        "INSERT INTO jobs(title, company, url_key, text_key, first_seen_at, last_seen_at)"
        " VALUES (?,?,?,?,?,?)",
        (title, "Acme", url_key, text_key or title.lower(), TS, TS),
    )
    return cur.lastrowid


def test_pragmas_on_every_connection(db_path):
    for _ in range(2):
        conn = connect(db_path)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        conn.close()


def test_schema_version_and_idempotent_init(db_path):
    init_db(db_path).close()
    conn = init_db(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_future_schema_version_is_rejected(db_path):
    conn = connect(db_path)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(DatabaseError):
        init_db(db_path)


def test_unversioned_db_with_tables_is_not_touched(db_path):
    conn = connect(db_path)
    conn.execute("CREATE TABLE outra(x)")
    conn.close()
    with pytest.raises(DatabaseError):
        init_db(db_path)


def test_unique_url_key(conn):
    _insert_job(conn, "A", url_key="x.com/1")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_job(conn, "B", url_key="x.com/1")


def test_unique_source_external_id(conn):
    job = _insert_job(conn)
    sql = "INSERT INTO job_sources(job_id, source, external_id, found_at, last_seen_at) VALUES (?,?,?,?,?)"
    conn.execute(sql, (job, "remotive", "42", TS, TS))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (job, "remotive", "42", TS, TS))
    conn.execute(sql, (job, "greenhouse", "42", TS, TS))  # outra fonte pode repetir o id


def test_unique_duplicate_alert(conn):
    job = _insert_job(conn)
    sql = (
        "INSERT INTO duplicate_alerts(job_original_id, source_found, url_found, match_type,"
        " confidence, detected_at) VALUES (?,?,?,?,?,?)"
    )
    conn.execute(sql, (job, "s", "u", "url", "confirmed", TS))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (job, "s", "u", "url", "confirmed", TS))
    conn.execute(sql, (job, "s", "u", "text_key", "possible", TS))  # outro match_type é outro alerta


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job_sources(job_id, source, found_at, last_seen_at) VALUES (999,'s',?,?)",
            (TS, TS),
        )


def test_empty_strings_rejected_for_missing_identifiers(conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_job(conn, "A", url_key="")
    job = _insert_job(conn, "B")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job_sources(job_id, source, external_id, found_at, last_seen_at)"
            " VALUES (?,?,?,?,?)",
            (job, "s", "", TS, TS),
        )


def test_two_jobs_without_url_and_external_id(conn):
    a = ingest_job(conn, JobData(source="x", title="Vaga A", company="Acme"))
    b = ingest_job(conn, JobData(source="x", title="Vaga B", company="Acme"))
    assert a.job_id != b.job_id
    row = conn.execute("SELECT COUNT(*) FROM jobs WHERE url_key IS NULL").fetchone()[0]
    assert row == 2
    row = conn.execute("SELECT COUNT(*) FROM job_sources WHERE external_id IS NULL").fetchone()[0]
    assert row == 2


def test_transaction_commit(conn):
    with transaction(conn):
        _insert_job(conn)
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_transaction_rollback_on_exception(conn):
    with pytest.raises(RuntimeError):
        with transaction(conn):
            _insert_job(conn)
            raise RuntimeError("falha")
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_nested_transaction_rejected(conn):
    with pytest.raises(TransactionError):
        with transaction(conn):
            with transaction(conn):
                pass
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_history_cannot_be_deleted(conn):
    job = _insert_job(conn)
    conn.execute(
        "INSERT INTO applications(job_id, created_at) VALUES (?,?)", (job, TS)
    )
    conn.execute(
        "INSERT INTO duplicate_alerts(job_original_id, source_found, match_type, confidence, detected_at)"
        " VALUES (?,?,?,?,?)",
        (job, "s", "url", "confirmed", TS),
    )
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM applications")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM duplicate_alerts")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM jobs")  # FK RESTRICT protege vagas com histórico
