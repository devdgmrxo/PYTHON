import copy
import json
from datetime import datetime, timezone

from app.importer.panel_importer import import_panel_data, import_panel_file, ms_to_iso
from app.profile import DEFAULT_PROFILE, load_profile, save_profile

PANEL = {
    "profile": {
        "nome": "Fulano de Tal",
        "email": "fulano@example.com",
        "tel": "",
        "cidade": "Recife",
        "linkedin": "",
        "github": "",
        "site": "",
        "kw": "python, javascript, react, sql, git",
        "cut": "80",
        "lim": "5",
        "remote": "remoto",
    },
    "applied": [
        {
            "title": "Estágio em Desenvolvimento",
            "company": "Empresa Exemplo",
            "url": "https://empresa.com/vagas/123?utm_source=x",
            "key": "empresa.com/vagas/123",
            "appliedAt": 1761800000000,
        },
        {"title": "Dev Júnior", "company": "Beta", "key": "beta|dev junior", "appliedAt": 1761900000000},
        {},
        {"title": "", "company": "", "url": ""},
    ],
    "queue": [
        {"title": "Dev Júnior", "company": "Outra Empresa", "url": "https://empresa.com/vagas/456"},
        {"title": None},
    ],
}


def counts(conn):
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("jobs", "job_sources", "applications", "review_queue", "duplicate_alerts")
    }


def test_import_creates_applied_and_pending(conn, tmp_path):
    res = import_panel_data(conn, copy.deepcopy(PANEL), tmp_path / "profile.json")
    assert (res.applied_imported, res.queue_imported, res.skipped_empty) == (2, 1, 3)
    apps = conn.execute("SELECT status, applied_at FROM applications ORDER BY id").fetchall()
    assert [a["status"] for a in apps] == ["already_applied", "already_applied"]
    expected = datetime.fromtimestamp(1761800000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert apps[0]["applied_at"] == expected
    queued = conn.execute(
        "SELECT j.status FROM review_queue q JOIN jobs j ON j.id=q.job_id"
    ).fetchall()
    assert [q["status"] for q in queued] == ["pending_review"]
    # fila nunca vira candidatura
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 2


def test_import_twice_is_idempotent(conn, tmp_path):
    profile_path = tmp_path / "profile.json"
    import_panel_data(conn, copy.deepcopy(PANEL), profile_path)
    first = counts(conn)
    profile_after_first = profile_path.read_text(encoding="utf-8")
    res = import_panel_data(conn, copy.deepcopy(PANEL), profile_path)
    assert counts(conn) == first
    assert res.profile_updated is False
    assert profile_path.read_text(encoding="utf-8") == profile_after_first


def test_profile_merge_preserves_manual_data(conn, tmp_path):
    profile_path = tmp_path / "profile.json"
    manual = copy.deepcopy(DEFAULT_PROFILE)
    manual["personal"]["email"] = "manual@example.com"
    manual["personal"]["full_name"] = "Nome Manual"
    manual["preferences"]["keywords"] = ["Python", "Django"]
    manual["preferences"]["minimum_score"] = 85
    manual["education"] = [{"course": "ADS", "institution": "Faculdade X"}]
    manual["skills"] = ["Python"]
    manual["experience"] = [{"company": "Y"}]
    manual["projects"] = [{"name": "P"}]
    manual["languages"] = [{"name": "Inglês"}]
    manual["documents"]["resume_pt"] = "cv.pdf"
    manual["additional_answers"] = {"pretensao": "x"}
    save_profile(profile_path, manual)

    import_panel_data(conn, copy.deepcopy(PANEL), profile_path)
    merged = load_profile(profile_path)
    assert merged["personal"]["email"] == "manual@example.com"
    assert merged["personal"]["full_name"] == "Nome Manual"
    assert merged["personal"]["city"] == "Recife"  # estava vazio: preenchido
    assert merged["preferences"]["minimum_score"] == 85  # alterado à mão: preservado
    assert merged["preferences"]["daily_application_limit"] == 5  # padrão: usa o painel
    assert merged["preferences"]["remote"] == "remoto"
    assert merged["preferences"]["keywords"] == ["Python", "Django", "javascript", "react", "sql", "git"]
    for field in ("education", "skills", "experience", "projects", "languages", "documents", "additional_answers"):
        assert merged[field] == manual[field]


def test_kw_string_and_cut_text_are_converted(conn, tmp_path):
    profile_path = tmp_path / "profile.json"
    import_panel_data(conn, copy.deepcopy(PANEL), profile_path)
    prefs = load_profile(profile_path)["preferences"]
    assert prefs["keywords"] == ["python", "javascript", "react", "sql", "git"]
    assert prefs["minimum_score"] == 80 and isinstance(prefs["minimum_score"], int)
    assert prefs["daily_application_limit"] == 5


def test_applied_at_milliseconds():
    assert ms_to_iso(1761800000000) == datetime.fromtimestamp(1761800000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert ms_to_iso("1761800000000") is not None
    assert ms_to_iso(None) is None and ms_to_iso("abc") is None and ms_to_iso(0) is None


def test_empty_records_are_ignored(conn, tmp_path):
    res = import_panel_data(conn, {"applied": [{}, {"title": ""}, None], "queue": [{}]}, tmp_path / "p.json")
    assert res.skipped_empty == 4
    assert counts(conn)["jobs"] == 0


def test_import_file(conn, tmp_path):
    path = tmp_path / "painel.json"
    path.write_text(json.dumps(PANEL), encoding="utf-8")
    res = import_panel_file(conn, path, tmp_path / "profile.json")
    assert res.applied_imported == 2
