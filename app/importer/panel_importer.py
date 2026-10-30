"""Importa o JSON exportado pelo painel HTML/JS.

- profile: mesclado em data/profile.json sem apagar nem sobrescrever dados manuais.
- applied: cada item vira candidatura already_applied (o painel não guarda o status original).
- queue: cada item vira pending_review (pendência de revisão, NÃO candidatura enviada).
A importação é idempotente: a chave do painel (ou a URL/empresa|título) é usada
como external_id da fonte 'panel'.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.collectors.common import make_text_key, normalize_url
from app.database.jobs import JobData, ingest_job
from app.profile import load_profile, merge_panel_profile, save_profile

SOURCE = "panel"


@dataclass
class ImportResult:
    applied_imported: int = 0
    queue_imported: int = 0
    skipped_empty: int = 0
    alerts_created: int = 0
    profile_updated: bool = False


def ms_to_iso(value) -> str | None:
    """appliedAt em milissegundos -> ISO 8601 UTC; inválido -> None."""
    try:
        millis = float(value)
        if millis <= 0:
            return None
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _text(value) -> str:
    return str(value).strip() if value is not None else ""


def _job_from_item(item) -> tuple[JobData | None, str | None]:
    """Converte um item do painel em JobData; None se o registro estiver vazio."""
    if not isinstance(item, dict):
        return None, None
    title = _text(item.get("title"))
    company = _text(item.get("company"))
    url = _text(item.get("url"))
    key = _text(item.get("key"))

    # A chave pode ser 'empresa|titulo'; usa-a para completar campos ausentes.
    if key and "|" in key and not url:
        key_company, _, key_title = key.partition("|")
        company = company or key_company.strip()
        title = title or key_title.strip()
    # A chave pode ser uma URL sem protocolo.
    if not url and key and "|" not in key and normalize_url(key)[0]:
        url = key
    if not title and not url:
        return None, None
    if not title:
        title = url  # título provisório; permanece visível para revisão

    url_key = normalize_url(url)[0] if url else None
    external_id = key or url_key or make_text_key(company, title)
    job = JobData(source=SOURCE, title=title, company=company, url=url or None, external_id=external_id)
    return job, ms_to_iso(item.get("appliedAt"))


def import_panel_data(conn, data: dict, profile_path: Path) -> ImportResult:
    result = ImportResult()
    if not isinstance(data, dict):
        raise ValueError("JSON do painel invalido: esperado um objeto")

    profile = load_profile(profile_path)
    merged = merge_panel_profile(profile, data.get("profile"))
    if merged != profile or not Path(profile_path).exists():
        save_profile(profile_path, merged)
        result.profile_updated = True

    for item in data.get("applied") or []:
        job, applied_at = _job_from_item(item)
        if job is None:
            result.skipped_empty += 1
            continue
        res = ingest_job(conn, job, mark_applied=True, applied_at=applied_at, origin=SOURCE)
        result.applied_imported += 1
        result.alerts_created += res.alerts_created

    for item in data.get("queue") or []:
        job, _ = _job_from_item(item)
        if job is None:
            result.skipped_empty += 1
            continue
        res = ingest_job(conn, job, queue_review=True, origin=SOURCE)
        result.queue_imported += 1
        result.alerts_created += res.alerts_created
    return result


def import_panel_file(conn, json_path: Path, profile_path: Path) -> ImportResult:
    with open(json_path, encoding="utf-8") as fh:
        data = json.load(fh)
    return import_panel_data(conn, data, profile_path)
