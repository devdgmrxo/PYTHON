"""Coletor Remotive (https://remotive.com/api/remote-jobs).

Regras da API: no máximo 4 consultas por dia (recomendação oficial; mais de 2 por
minuto é bloqueado), category=software-dev é aceito, é obrigatório manter o link
da vaga na Remotive e citar a Remotive como fonte.
O limite diário é persistente (tabela remotive_queries).
"""

import logging
from datetime import datetime

from app import config
from app.collectors.common import CollectResult, http_get_json, record_run
from app.database.db import now_utc_iso, transaction
from app.database.jobs import JobData, ingest_job

log = logging.getLogger("jobhunter.remotive")
SOURCE = "remotive"
ATTRIBUTION = "Fonte: Remotive (https://remotive.com)"


class QuotaExceeded(Exception):
    pass


def _today(now: datetime | None) -> tuple[str, str]:
    moment = now or datetime.now().astimezone()
    return moment.date().isoformat(), moment.strftime("%H:%M:%S")


def queries_today(conn, now: datetime | None = None) -> int:
    day, _ = _today(now)
    return conn.execute("SELECT COUNT(*) FROM remotive_queries WHERE query_date=?", (day,)).fetchone()[0]


def remaining_queries(conn, now: datetime | None = None) -> int:
    return max(0, config.REMOTIVE_DAILY_LIMIT - queries_today(conn, now))


def _reserve_query(conn, now: datetime | None) -> int:
    """Confere o limite e registra a consulta na mesma transação (vale mesmo se reiniciar)."""
    day, clock = _today(now)
    with transaction(conn):
        used = conn.execute("SELECT COUNT(*) FROM remotive_queries WHERE query_date=?", (day,)).fetchone()[0]
        if used >= config.REMOTIVE_DAILY_LIMIT:
            raise QuotaExceeded(f"limite diario da Remotive atingido ({used}/{config.REMOTIVE_DAILY_LIMIT})")
        cur = conn.execute(
            "INSERT INTO remotive_queries(query_date, queried_at) VALUES (?,?)",
            (day, f"{day}T{clock}"),
        )
        return cur.lastrowid


def _finish_query(conn, query_id, status, returned=None, error=None) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE remotive_queries SET status=?, jobs_returned=?, error=? WHERE id=?",
            (status, returned, error, query_id),
        )


def collect_remotive(conn, fetch=None, now: datetime | None = None) -> CollectResult:
    fetch = fetch or http_get_json
    started = now_utc_iso()
    result = CollectResult(source=SOURCE, target=config.REMOTIVE_CATEGORY)
    try:
        query_id = _reserve_query(conn, now)
    except QuotaExceeded as exc:
        result.status, result.error = "error", str(exc)
        log.warning(str(exc))
        record_run(conn, result, started, now_utc_iso())
        return result

    try:
        payload = fetch(config.REMOTIVE_URL, {"category": config.REMOTIVE_CATEGORY})
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            raise ValueError("resposta da Remotive sem lista 'jobs'")
    except Exception as exc:  # falha de rede/JSON: a consulta continua contada
        _finish_query(conn, query_id, "error", error=str(exc))
        result.status, result.error = "error", str(exc)
        log.error("Remotive falhou: %s", exc)
        record_run(conn, result, started, now_utc_iso())
        return result

    result.jobs_found = len(jobs)
    for item in jobs:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            result.jobs_skipped += 1
            continue
        data = JobData(
            source=SOURCE,
            title=item["title"],
            company=item.get("company_name") or "",
            url=item.get("url"),  # URL original da Remotive preservada
            external_id=str(item["id"]) if item.get("id") is not None else None,
            description=item.get("description"),
            location=item.get("candidate_required_location"),
            is_remote=True,
            attribution=ATTRIBUTION,
        )
        res = ingest_job(conn, data)
        result.alerts_created += res.alerts_created
        if res.action != "seen":
            result.jobs_new += 1
    _finish_query(conn, query_id, "ok", returned=len(jobs))
    record_run(conn, result, started, now_utc_iso())
    return result
