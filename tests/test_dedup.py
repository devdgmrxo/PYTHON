import threading

from app.database.db import connect
from app.database.jobs import JobData, acknowledge_alert, ingest_job, list_alerts


def job(source="remotive", title="Estágio em Desenvolvimento", company="Acme", **kw):
    return JobData(source=source, title=title, company=company, **kw)


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_same_job_in_two_sources_same_url(conn):
    a = ingest_job(conn, job("remotive", url="https://www.acme.com/vagas/1?utm_source=remotive"))
    b = ingest_job(conn, job("greenhouse", url="http://acme.com/vagas/1/"))
    assert a.action == "created" and b.action == "linked"
    assert a.job_id == b.job_id
    assert count(conn, "jobs") == 1 and count(conn, "job_sources") == 2
    alert = conn.execute("SELECT * FROM duplicate_alerts").fetchone()
    assert (alert["match_type"], alert["confidence"]) == ("url", "confirmed")


def test_external_id_same_source_is_the_same_job_without_alert(conn):
    a = ingest_job(conn, job(external_id="77", url="https://acme.com/v/77"))
    b = ingest_job(conn, job(external_id="77", url="https://acme.com/v/77"))
    assert b.action == "seen" and a.job_id == b.job_id
    assert count(conn, "duplicate_alerts") == 0


def test_external_id_match_with_changed_url_creates_confirmed_alert_once(conn):
    ingest_job(conn, job(external_id="77", url="https://acme.com/v/77"))
    for _ in range(3):
        ingest_job(conn, job(external_id="77", url="https://acme.com/novo-caminho/77"))
    rows = conn.execute("SELECT * FROM duplicate_alerts").fetchall()
    assert len(rows) == 1
    assert (rows[0]["match_type"], rows[0]["confidence"]) == ("external_id", "confirmed")
    assert count(conn, "jobs") == 1


def test_text_key_match_is_only_possible_and_not_merged(conn):
    a = ingest_job(conn, job("remotive", url="https://remotive.com/job/1"))
    b = ingest_job(conn, job("greenhouse", url="https://boards.greenhouse.io/acme/jobs/9"))
    assert b.action == "created" and a.job_id != b.job_id
    alert = conn.execute("SELECT * FROM duplicate_alerts").fetchone()
    assert (alert["match_type"], alert["confidence"]) == ("text_key", "possible")
    assert alert["job_original_id"] == a.job_id and alert["job_found_id"] == b.job_id


def test_two_different_jobs_same_title_same_company(conn):
    a = ingest_job(conn, job("greenhouse", external_id="t:1", url="https://acme.com/careers?gh_jid=1"))
    b = ingest_job(conn, job("greenhouse", external_id="t:2", url="https://acme.com/careers?gh_jid=2"))
    assert a.job_id != b.job_id
    assert count(conn, "jobs") == 2
    assert conn.execute("SELECT confidence FROM duplicate_alerts").fetchone()[0] == "possible"


def test_generic_id_url_is_only_possible(conn):
    ingest_job(conn, job("remotive", url="https://acme.com/vagas?id=5"))
    res = ingest_job(conn, job("greenhouse", url="https://www.acme.com/vagas?id=5"))
    assert res.action == "linked"
    alert = conn.execute("SELECT * FROM duplicate_alerts").fetchone()
    assert (alert["match_type"], alert["confidence"]) == ("generic_url", "possible")


def test_previous_application_found_in_another_source(conn):
    ingest_job(
        conn, job("panel", external_id="acme.com/vagas/1", url="https://acme.com/vagas/1"),
        mark_applied=True, applied_at="2025-01-01T00:00:00Z",
    )
    ingest_job(conn, job("remotive", url="https://www.acme.com/vagas/1?utm_source=rem"))
    alerts = list_alerts(conn)
    assert len(alerts) == 1 and alerts[0]["applied_flag"] == 1
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "already_applied"


def test_possible_alert_to_applied_job_in_other_source_flags_it(conn):
    ingest_job(conn, job("panel", external_id="acme|estagio"), mark_applied=True)
    ingest_job(conn, job("remotive", url="https://remotive.com/job/9"))
    alerts = list_alerts(conn)
    assert len(alerts) == 1 and alerts[0]["confidence"] == "possible"
    assert alerts[0]["applied_flag"] == 1


def test_duplicate_without_previous_application_is_not_flagged(conn):
    ingest_job(conn, job("remotive", url="https://acme.com/vagas/1"))
    ingest_job(conn, job("greenhouse", url="https://acme.com/vagas/1"))
    alerts = list_alerts(conn)
    assert len(alerts) == 1 and alerts[0]["applied_flag"] == 0
    assert count(conn, "applications") == 0


def test_repeated_runs_do_not_duplicate_alerts_or_jobs(conn):
    items = [
        job("remotive", url="https://remotive.com/job/1", external_id="1"),
        job("greenhouse", url="https://boards.greenhouse.io/acme/jobs/9", external_id="a:9"),
        job("other", title="Sem identificadores", company="Beta"),
        job("third", title="Sem identificadores", company="Beta"),
    ]
    for _ in range(4):
        for item in items:
            ingest_job(conn, item)
    assert count(conn, "jobs") == 4  # cada fonte mantém a sua; text_key só alerta
    assert count(conn, "duplicate_alerts") == 2


def test_acknowledge_keeps_alert(conn):
    ingest_job(conn, job("remotive", url="https://acme.com/vagas/1"))
    ingest_job(conn, job("greenhouse", url="https://acme.com/vagas/1"))
    alert_id = list_alerts(conn)[0]["id"]
    assert acknowledge_alert(conn, alert_id)
    assert list_alerts(conn) == []
    assert len(list_alerts(conn, include_acknowledged=True)) == 1


def test_manual_fields_are_not_overwritten(conn):
    first = ingest_job(conn, job(external_id="1", description="original", location="Brasil"))
    conn.execute("UPDATE jobs SET description='editado a mao' WHERE id=?", (first.job_id,))
    ingest_job(conn, job(external_id="1", description="outro texto", location="USA"))
    row = conn.execute("SELECT description, location FROM jobs").fetchone()
    assert (row["description"], row["location"]) == ("editado a mao", "Brasil")


def test_concurrent_ingest_of_same_job(db_path):
    from app.database.db import init_db

    init_db(db_path).close()
    errors = []

    def worker(n):
        conn = connect(db_path)
        try:
            for i in range(5):
                ingest_job(conn, job(f"src{n % 3}", url=f"https://acme.com/vagas/{i}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    conn = connect(db_path)
    assert count(conn, "jobs") == 5
    assert count(conn, "job_sources") == 5 * 3  # 3 fontes distintas por vaga
    conn.close()
