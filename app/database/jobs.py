"""Inserção de vagas com deduplicação, vínculo de fontes e alertas.

Cada vaga é processada em uma única transação (BEGIN IMMEDIATE ... COMMIT).

Regras de correspondência (da mais forte para a mais fraca):
1. Mesma fonte já vista (source + external_id, ou source + url_key quando não há
   external_id, ou source + text_key quando não há nenhum identificador): é a
   mesma vaga reaparecendo; só atualiza last_seen_at.
2. url_key igual a de outra vaga: a fonte é vinculada à vaga existente.
   Alerta 'url'/confirmed, ou 'generic_url'/possible se a URL depende só do
   parâmetro genérico 'id'.
3. text_key igual (empresa + título): pode ser outra vaga com o mesmo título.
   Nada é fundido: cria-se uma nova vaga e um alerta 'text_key'/possible.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from app.collectors.common import make_text_key, normalize_url
from app.database.db import now_utc_iso, transaction


@dataclass
class JobData:
    source: str
    title: str
    company: str = ""
    url: str | None = None
    external_id: str | None = None
    description: str | None = None
    location: str | None = None
    is_remote: bool | None = None
    status: str = "new"
    attribution: str | None = None


@dataclass
class IngestResult:
    job_id: int
    action: str  # created | linked | seen
    alerts_created: int = 0
    matched_job_ids: list[int] = field(default_factory=list)


def _clean(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _add_alert(conn, original_id, found_id, source, url, match_type, confidence, ts) -> int:
    cur = conn.execute(
        "INSERT OR IGNORE INTO duplicate_alerts(job_original_id, job_found_id, source_found,"
        " url_found, match_type, confidence, detected_at) VALUES (?,?,?,?,?,?,?)",
        (original_id, found_id, source, url or "", match_type, confidence, ts),
    )
    return cur.rowcount


def _fill_blanks(conn, job_id, data: JobData, ts) -> None:
    """Atualiza last_seen_at e só preenche campos vazios (nunca sobrescreve)."""
    is_remote = None if data.is_remote is None else int(bool(data.is_remote))
    conn.execute(
        "UPDATE jobs SET last_seen_at=?,"
        " description=CASE WHEN description IS NULL OR description='' THEN ? ELSE description END,"
        " location=CASE WHEN location IS NULL OR location='' THEN ? ELSE location END,"
        " is_remote=COALESCE(is_remote, ?),"
        " company=CASE WHEN company='' THEN ? ELSE company END"
        " WHERE id=?",
        (ts, _clean(data.description), _clean(data.location), is_remote, (data.company or "").strip(), job_id),
    )


def _insert_source(conn, job_id, data, ext, url, url_key, ts) -> None:
    conn.execute(
        "INSERT INTO job_sources(job_id, source, external_id, url, url_key, attribution,"
        " found_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
        (job_id, data.source, ext, url, url_key, data.attribution, ts, ts),
    )


def _find_same_source(conn, source, ext, url_key, text_key):
    if ext:
        return conn.execute(
            "SELECT id, job_id, url_key FROM job_sources WHERE source=? AND external_id=?",
            (source, ext),
        ).fetchone()
    if url_key:
        return conn.execute(
            "SELECT id, job_id, url_key FROM job_sources"
            " WHERE source=? AND external_id IS NULL AND url_key=?",
            (source, url_key),
        ).fetchone()
    return conn.execute(
        "SELECT js.id, js.job_id, js.url_key FROM job_sources js JOIN jobs j ON j.id=js.job_id"
        " WHERE js.source=? AND js.external_id IS NULL AND js.url_key IS NULL AND j.text_key=?",
        (source, text_key),
    ).fetchone()


def _ingest(conn, data: JobData, title, company, url, ext, url_key, generic, text_key, ts) -> IngestResult:
    source = data.source

    # 1. Mesma fonte: a vaga apenas reapareceu.
    row = _find_same_source(conn, source, ext, url_key, text_key)
    if row:
        alerts = 0
        if ext and url_key and row["url_key"] and row["url_key"] != url_key:
            # Mesmo identificador externo com URL diferente: registra para revisão.
            alerts = _add_alert(conn, row["job_id"], None, source, url, "external_id", "confirmed", ts)
        conn.execute(
            "UPDATE job_sources SET last_seen_at=?, url=COALESCE(url, ?), url_key=COALESCE(url_key, ?)"
            " WHERE id=?",
            (ts, url, url_key, row["id"]),
        )
        _fill_blanks(conn, row["job_id"], data, ts)
        return IngestResult(row["job_id"], "seen", alerts, [row["job_id"]])

    # 2. URL já conhecida: vincula a fonte à vaga existente.
    if url_key:
        match = conn.execute(
            "SELECT id, url_key_generic FROM jobs WHERE url_key=?", (url_key,)
        ).fetchone()
        if match:
            job_id = match["id"]
            same_source = conn.execute(
                "SELECT 1 FROM job_sources WHERE job_id=? AND source=?", (job_id, source)
            ).fetchone()
            _insert_source(conn, job_id, data, ext, url, url_key, ts)
            _fill_blanks(conn, job_id, data, ts)
            alerts = 0
            if not same_source:
                possible = bool(generic or match["url_key_generic"])
                alerts = _add_alert(
                    conn, job_id, None, source, url,
                    "generic_url" if possible else "url",
                    "possible" if possible else "confirmed", ts,
                )
            return IngestResult(job_id, "linked", alerts, [job_id])

    # 3. Nova vaga; mesma empresa+título gera apenas alerta 'possible'.
    candidates = [
        r["id"] for r in conn.execute("SELECT id FROM jobs WHERE text_key=? ORDER BY id", (text_key,))
    ]
    is_remote = None if data.is_remote is None else int(bool(data.is_remote))
    cur = conn.execute(
        "INSERT INTO jobs(title, company, url, url_key, url_key_generic, text_key, description,"
        " location, is_remote, status, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            title, company, url, url_key, int(generic), text_key,
            _clean(data.description), _clean(data.location), is_remote, data.status, ts, ts,
        ),
    )
    job_id = cur.lastrowid
    _insert_source(conn, job_id, data, ext, url, url_key, ts)
    alerts = 0
    for original in candidates:
        alerts += _add_alert(conn, original, job_id, source, url, "text_key", "possible", ts)
    return IngestResult(job_id, "created", alerts, candidates)


def ingest_job(
    conn: sqlite3.Connection,
    data: JobData,
    *,
    mark_applied: bool = False,
    applied_at: str | None = None,
    queue_review: bool = False,
    origin: str | None = None,
    now: datetime | None = None,
) -> IngestResult:
    """Insere ou vincula uma vaga em uma transação; opcionalmente registra
    candidatura já realizada (already_applied) ou item da fila de revisão."""
    title = _clean(data.title)
    if not title:
        raise ValueError("vaga sem titulo")
    company = (data.company or "").strip()
    url = _clean(data.url)
    ext = _clean(data.external_id)
    url_key, generic = normalize_url(url)
    text_key = make_text_key(company, title)
    ts = now_utc_iso(now)

    with transaction(conn):
        result = _ingest(conn, data, title, company, url, ext, url_key, generic, text_key, ts)
        job_id = result.job_id
        if mark_applied:
            conn.execute(
                "INSERT OR IGNORE INTO applications(job_id, status, applied_at, origin, created_at)"
                " VALUES (?, 'already_applied', ?, ?, ?)",
                (job_id, applied_at, origin or data.source, ts),
            )
            conn.execute(
                "UPDATE applications SET applied_at=COALESCE(applied_at, ?) WHERE job_id=?",
                (applied_at, job_id),
            )
            conn.execute("UPDATE jobs SET status='already_applied' WHERE id=?", (job_id,))
        if queue_review:
            applied = conn.execute("SELECT 1 FROM applications WHERE job_id=?", (job_id,)).fetchone()
            if not applied:
                conn.execute(
                    "INSERT OR IGNORE INTO review_queue(job_id, origin, added_at) VALUES (?,?,?)",
                    (job_id, origin or data.source, ts),
                )
                conn.execute("UPDATE jobs SET status='pending_review' WHERE id=? AND status='new'", (job_id,))
    return result


def list_alerts(conn: sqlite3.Connection, include_acknowledged: bool = False) -> list[sqlite3.Row]:
    """Alertas com as duas vagas e a indicação de candidatura anterior.

    applied_flag = 1 apenas se uma das vagas tem candidatura already_applied;
    um alerta sozinho nunca significa candidatura realizada.
    """
    where = "" if include_acknowledged else "WHERE a.acknowledged=0"
    return conn.execute(
        f"""
        SELECT a.*, o.title AS original_title, o.company AS original_company,
               o.url AS original_url, f.title AS found_title,
               EXISTS (SELECT 1 FROM applications ap
                       WHERE ap.status='already_applied'
                         AND ap.job_id IN (a.job_original_id, a.job_found_id)) AS applied_flag
        FROM duplicate_alerts a
        JOIN jobs o ON o.id = a.job_original_id
        LEFT JOIN jobs f ON f.id = a.job_found_id
        {where}
        ORDER BY a.id
        """
    ).fetchall()


def acknowledge_alert(conn: sqlite3.Connection, alert_id: int) -> bool:
    """Marca como revisado (o alerta é mantido)."""
    with transaction(conn):
        cur = conn.execute("UPDATE duplicate_alerts SET acknowledged=1 WHERE id=?", (alert_id,))
    return cur.rowcount > 0
