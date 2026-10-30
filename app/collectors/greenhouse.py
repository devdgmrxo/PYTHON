"""Coletor Greenhouse (API pública de boards).

Cada empresa de data/greenhouse_boards.json é processada de forma independente:
uma falha em um board não interrompe os demais. Tokens nunca são inventados.
"""

import json
import logging
from pathlib import Path

from app import config
from app.collectors.common import CollectResult, http_get_json, record_run
from app.database.db import now_utc_iso
from app.database.jobs import JobData, ingest_job

log = logging.getLogger("jobhunter.greenhouse")
SOURCE = "greenhouse"


def load_boards(path: Path) -> list[dict]:
    """Lê os boards configurados; ignora entradas sem company/board_token."""
    path = Path(path)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    boards = data.get("boards") if isinstance(data, dict) else data
    valid = []
    for entry in boards or []:
        if not isinstance(entry, dict):
            continue
        token = str(entry.get("board_token") or "").strip()
        company = str(entry.get("company") or "").strip()
        if token and company:
            valid.append({"company": company, "board_token": token})
    return valid


def collect_greenhouse(conn, boards: list[dict], fetch=None) -> list[CollectResult]:
    fetch = fetch or http_get_json
    results = []
    for board in boards:
        started = now_utc_iso()
        company, token = board["company"], board["board_token"]
        result = CollectResult(source=SOURCE, target=token)
        try:
            payload = fetch(config.GREENHOUSE_URL.format(token=token), {"content": "true"})
            jobs = payload.get("jobs") if isinstance(payload, dict) else None
            if not isinstance(jobs, list):
                raise ValueError("resposta sem lista 'jobs'")
            result.jobs_found = len(jobs)
            for item in jobs:
                if not isinstance(item, dict) or not str(item.get("title") or "").strip():
                    result.jobs_skipped += 1
                    continue
                location = item.get("location")
                data = JobData(
                    source=SOURCE,
                    title=item["title"],
                    company=company,
                    url=item.get("absolute_url"),
                    external_id=f"{token}:{item['id']}" if item.get("id") is not None else None,
                    description=item.get("content"),
                    location=location.get("name") if isinstance(location, dict) else location,
                )
                res = ingest_job(conn, data)
                result.alerts_created += res.alerts_created
                if res.action != "seen":
                    result.jobs_new += 1
        except Exception as exc:  # isola a falha desta empresa
            result.status, result.error = "error", f"{type(exc).__name__}: {exc}"
            log.error("Greenhouse %s falhou: %s", token, exc)
        record_run(conn, result, started, now_utc_iso())
        results.append(result)
    return results
