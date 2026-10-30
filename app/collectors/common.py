"""Utilitários compartilhados: normalização de texto/URL, HTTP e registro de execuções."""

import html
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

from app import config

# Parâmetros removidos por serem de rastreamento (utm_* tratado à parte).
TRACKING_PARAMS = {"ref", "source", "fbclid", "gclid"}
# Parâmetros que identificam uma vaga específica (sempre preservados).
SPECIFIC_ID_PARAMS = {"gh_jid", "currentjobid", "jobid", "job", "jid"}

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


def strip_html(value: str | None) -> str:
    """Remove HTML e decodifica entidades (inclusive HTML escapado, como o da Greenhouse)."""
    if not value:
        return ""
    text = html.unescape(str(value))
    text = _SCRIPT_STYLE_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return html.unescape(text)


def normalize_text(value: str | None) -> str:
    """HTML limpo, sem acentos, minúsculo e com espaços normalizados."""
    text = strip_html(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _WS_RE.sub(" ", text.lower()).strip()


def make_text_key(company: str | None, title: str | None) -> str | None:
    """Chave 'empresa|título' normalizada; None se não houver título."""
    raw_title = (title or "").strip()
    if not raw_title:
        return None

    def squash(value: str | None) -> str:
        return _WS_RE.sub(" ", re.sub(r"[^a-z0-9+#]+", " ", normalize_text(value))).strip()

    return f"{squash(company)}|{squash(title) or raw_title.lower()}"


def normalize_url(url: str | None) -> tuple[str | None, bool]:
    """Devolve (url_key, url_key_generic).

    - Com ou sem protocolo, com ou sem www, domínio e caminho em minúsculas.
    - Remove utm_*, ref, source, fbclid e gclid; preserva os demais parâmetros
      (gh_jid, currentJobId, jobId, job, jid, id, ...).
    - url_key_generic é True quando a identificação depende só do parâmetro
      genérico 'id' (nenhum parâmetro específico e caminho sem dígitos).
    - URL sem caminho nem parâmetros (página inicial) não identifica vaga: (None, False).
    """
    if url is None:
        return None, False
    raw = str(url).strip()
    if not raw:
        return None, False
    if "://" not in raw:
        raw = "http://" + raw.lstrip("/")
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None, False
    if not host:
        return None, False
    if host.startswith("www."):
        host = host[4:]

    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/").lower()
    params = []
    for name, value in parse_qsl(parts.query, keep_blank_values=False):
        name = name.strip().lower()
        if not name or name.startswith("utm_") or name in TRACKING_PARAMS:
            continue
        params.append((name, value.strip()))
    params.sort()

    if not path and not params:
        return None, False

    names = {name for name, _ in params}
    generic = "id" in names and not (names & SPECIFIC_ID_PARAMS) and not re.search(r"\d", path)

    key = host + path
    if params:
        key += "?" + urlencode(params)
    return key, generic


def http_get_json(url: str, params: dict | None = None):
    """GET que devolve JSON; levanta exceção em erro HTTP ou JSON inválido."""
    import requests

    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
        timeout=config.HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


@dataclass
class CollectResult:
    source: str
    target: str
    status: str = "ok"  # ok | error
    jobs_found: int = 0
    jobs_new: int = 0
    jobs_skipped: int = 0
    alerts_created: int = 0
    error: str | None = None


def record_run(conn, result: CollectResult, started_at: str, finished_at: str) -> None:
    """Grava o resultado de uma coleta (por fonte/empresa) em collector_runs."""
    from app.database.db import transaction

    with transaction(conn):
        conn.execute(
            "INSERT INTO collector_runs(source, target, started_at, finished_at, status,"
            " jobs_found, jobs_new, error) VALUES (?,?,?,?,?,?,?,?)",
            (
                result.source,
                result.target,
                started_at,
                finished_at,
                result.status,
                result.jobs_found,
                result.jobs_new,
                result.error,
            ),
        )
