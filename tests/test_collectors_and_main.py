import json
from datetime import datetime

import pytest

from app import config, main
from app.collectors.greenhouse import collect_greenhouse, load_boards
from app.collectors.remotive import collect_remotive, remaining_queries
from app.database.db import connect, init_db
from app.matching.scorer import apply_scores
from app.profile import DEFAULT_PROFILE
from app.report import build_report

NOW = datetime(2025, 3, 10, 10, 0).astimezone()


def remotive_payload():
    def item(id_, title, company, location, description):
        return {
            "id": id_, "url": f"https://remotive.com/remote-jobs/software-dev/{id_}",
            "title": title, "company_name": company,
            "candidate_required_location": location, "description": description,
        }

    return {"jobs": [
        item(1, "Estágio em Desenvolvimento Python", "Alfa", "Brazil", "<p>Python e SQL</p>"),
        item(2, "Junior Developer", "Beta", "Worldwide", "Python and SQL"),
        item(3, "Junior Developer", "Gama", "USA Only", "Python and SQL"),
        item(4, "Senior Engineer", "Delta", "Brazil", "10+ years"),
        {"id": 5, "title": "", "company_name": "X"},
    ]}


def test_remotive_ingests_with_attribution_and_original_url(conn):
    res = collect_remotive(conn, fetch=lambda url, params: remotive_payload(), now=NOW)
    assert res.status == "ok" and res.jobs_found == 5 and res.jobs_skipped == 1
    src = conn.execute("SELECT url, attribution FROM job_sources WHERE external_id='1'").fetchone()
    assert src["url"] == "https://remotive.com/remote-jobs/software-dev/1"
    assert "Remotive" in src["attribution"]


def test_remotive_uses_software_dev_category(conn):
    seen = {}
    collect_remotive(conn, fetch=lambda url, params: seen.update(url=url, params=params) or {"jobs": []}, now=NOW)
    assert seen["url"] == "https://remotive.com/api/remote-jobs"
    assert seen["params"] == {"category": "software-dev"}


def test_remotive_daily_limit_is_persistent(db_path):
    calls = []
    fetch = lambda url, params: calls.append(1) or {"jobs": []}  # noqa: E731
    for _ in range(4):
        conn = init_db(db_path)  # nova conexão = "reiniciar o programa"
        assert collect_remotive(conn, fetch=fetch, now=NOW).status == "ok"
        conn.close()
    conn = connect(db_path)
    res = collect_remotive(conn, fetch=fetch, now=NOW)
    assert res.status == "error" and "limite" in res.error
    assert len(calls) == 4 and remaining_queries(conn, NOW) == 0
    next_day = datetime(2025, 3, 11, 9, 0).astimezone()
    assert remaining_queries(conn, next_day) == 4
    row = conn.execute("SELECT query_date, queried_at FROM remotive_queries LIMIT 1").fetchone()
    assert row["query_date"] == "2025-03-10" and row["queried_at"].startswith("2025-03-10T")
    conn.close()


def test_remotive_failure_still_counts_the_query(conn):
    def boom(url, params):
        raise ConnectionError("sem rede")

    res = collect_remotive(conn, fetch=boom, now=NOW)
    assert res.status == "error"
    assert remaining_queries(conn, NOW) == 3


def test_greenhouse_failure_in_one_board_does_not_stop_others(conn):
    def fetch(url, params):
        if "quebrada" in url:
            raise TimeoutError("timeout")
        if "invalida" in url:
            return {"outra": []}
        return {"jobs": [{"id": 10, "title": "Software Engineer Intern", "absolute_url": url.replace("boards-api.greenhouse.io/v1/boards", "boards.greenhouse.io") + "?gh_jid=10",
                          "location": {"name": "São Paulo, Brazil"}, "content": "&lt;p&gt;Python&lt;/p&gt;"}]}

    boards = [
        {"company": "Um", "board_token": "boaum"},
        {"company": "Dois", "board_token": "quebrada"},
        {"company": "Tres", "board_token": "invalida"},
        {"company": "Quatro", "board_token": "boaquatro"},
    ]
    results = collect_greenhouse(conn, boards, fetch=fetch)
    assert [r.status for r in results] == ["ok", "error", "error", "ok"]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    runs = conn.execute("SELECT target, status, error FROM collector_runs ORDER BY id").fetchall()
    assert [r["status"] for r in runs] == ["ok", "error", "error", "ok"]
    assert "TimeoutError" in runs[1]["error"]


def test_load_boards_ignores_unconfigured_template(tmp_path):
    path = tmp_path / "boards.json"
    path.write_text(json.dumps({"_status": "NAO CONFIGURADO", "boards": [{"company": "A"}, {"company": "B", "board_token": "b"}]}))
    assert load_boards(path) == [{"company": "B", "board_token": "b"}]
    assert load_boards(tmp_path / "inexistente.json") == []


def test_report_sections_and_eligibility(conn):
    collect_remotive(conn, fetch=lambda url, params: remotive_payload(), now=NOW)
    apply_scores(conn, ["python", "sql"])
    report = build_report(conn, DEFAULT_PROFILE)
    part1, rest = report.split("3. VERIFICAR LOCALIZACAO")
    assert "Estágio em Desenvolvimento Python" in part1
    assert "Gama" not in report and "USA Only" not in report  # restrita fora da seleção
    assert "Junior Developer - Beta" in rest  # desconhecida em seção própria
    assert "Junior Developer - Beta" not in part1
    assert "Senior Engineer" not in report  # abaixo do corte
    assert "NOTA MAXIMA EFETIVA: 90" in report


def test_report_shows_applied_and_keeps_alerts_separate(conn, tmp_path):
    from app.database.jobs import JobData, ingest_job

    ingest_job(conn, JobData(source="panel", title="Dev Junior", company="Alfa", external_id="alfa|dev junior"),
               mark_applied=True, applied_at="2025-01-01T00:00:00Z")
    ingest_job(conn, JobData(source="remotive", title="Dev Junior", company="Alfa", url="https://r.com/1",
                             location="Brazil", is_remote=True, description="Python"))
    apply_scores(conn, ["python"])
    report = build_report(conn, DEFAULT_PROFILE)
    assert "2. JA INSCRITAS ENCONTRADAS (1)" in report
    assert "Alertas ligados a vagas ja inscritas" in report


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    for name, value in {
        "DB_PATH": tmp_path / "jh.db", "PROFILE_PATH": tmp_path / "profile.json",
        "BOARDS_PATH": tmp_path / "boards.json", "LOCK_PATH": tmp_path / "jh.lock",
    }.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "setup_logging", lambda *a, **k: None)
    return tmp_path


def test_cli_import_report_alerts(cli_env, capsys):
    panel = cli_env / "painel.json"
    panel.write_text(json.dumps({
        "profile": {"kw": "python, sql"},
        "applied": [{"title": "Dev", "company": "Alfa", "key": "alfa|dev", "appliedAt": 1761800000000}],
        "queue": [{"title": "Estagio", "company": "Beta", "url": "https://beta.com/v/1"}],
    }))
    assert main.main(["import", str(panel)]) == 0
    assert main.main(["run", "--source", "none"]) == 0
    out = capsys.readouterr().out
    assert "1 ja inscritas, 1 na fila" in out and "JA INSCRITAS ENCONTRADAS (1)" in out
    assert main.main(["alerts"]) == 0
    assert "Nenhum alerta" in capsys.readouterr().out
    assert not (cli_env / "jh.lock").exists()


def test_cli_blocked_by_active_lock(cli_env, capsys):
    from app.lock import RunLock

    with RunLock(config.LOCK_PATH):
        assert main.main(["run", "--source", "none"]) == 2
    assert "ERRO" in capsys.readouterr().err
