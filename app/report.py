"""Relatório de texto das oportunidades."""

import json

from app.database.jobs import list_alerts
from app.matching.scorer import MAX_EFFECTIVE_SCORE


def _reasons(row) -> list[str]:
    try:
        return json.loads(row["score_details"] or "{}").get("reasons", [])
    except ValueError:
        return []


def _job_lines(row, warnings: dict[int, list[str]]) -> list[str]:
    where = f" | {row['location']}" if row["location"] else ""
    lines = [f"  [{row['score']}/{MAX_EFFECTIVE_SCORE}] {row['title']} - {row['company'] or 'empresa nao informada'}{where}"]
    if row["url"]:
        lines.append(f"      {row['url']}")
    for warning in warnings.get(row["id"], []):
        lines.append(f"      ! {warning}")
    for reason in _reasons(row):
        lines.append(f"      - {reason}")
    return lines


def _duplicate_warnings(conn) -> dict[int, list[str]]:
    """Avisos 'possível duplicata' por vaga (alerta não é candidatura)."""
    warnings: dict[int, list[str]] = {}
    for alert in list_alerts(conn):
        for job_id in (alert["job_original_id"], alert["job_found_id"]):
            if job_id is None:
                continue
            text = f"alerta de duplicidade #{alert['id']} ({alert['match_type']}, {alert['confidence']})"
            if alert["applied_flag"]:
                text += " - ligado a vaga ja inscrita"
            warnings.setdefault(job_id, []).append(text)
    return warnings


def build_report(conn, profile: dict) -> str:
    prefs = profile.get("preferences", {})
    cutoff = int(prefs.get("minimum_score", 70))
    limit = int(prefs.get("daily_application_limit", 10))
    warnings = _duplicate_warnings(conn)
    out: list[str] = ["RELATORIO JOBHUNTER", "=" * 19]

    best = conn.execute(
        "SELECT * FROM jobs WHERE status='new' AND location_status='allowed_brazil' AND score>=?"
        " ORDER BY score DESC, id LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    out += ["", f"1. MELHORES OPORTUNIDADES NOVAS (nota >= {cutoff}, ate {limit})"]
    out += [line for row in best for line in _job_lines(row, warnings)] or ["  nenhuma"]

    applied = conn.execute(
        "SELECT j.title, j.company, j.url, a.applied_at FROM applications a JOIN jobs j ON j.id=a.job_id"
        " WHERE a.status='already_applied' ORDER BY a.applied_at DESC, a.id DESC"
    ).fetchall()
    out += ["", f"2. JA INSCRITAS ENCONTRADAS ({len(applied)})"]
    for row in applied:
        out.append(f"  {row['title']} - {row['company'] or 'empresa nao informada'} (em {row['applied_at'] or 'data desconhecida'})")
    if not applied:
        out.append("  nenhuma")
    linked = [a for a in list_alerts(conn) if a["applied_flag"]]
    if linked:
        out.append("  Alertas ligados a vagas ja inscritas (revisar com `alerts`):")
        for a in linked:
            out.append(f"    #{a['id']} {a['match_type']}/{a['confidence']}: {a['original_title']} - {a['original_company']}")

    unknown = conn.execute(
        "SELECT * FROM jobs WHERE status='new' AND location_status='unknown' AND score>=?"
        " ORDER BY score DESC, id LIMIT 50",
        (cutoff,),
    ).fetchall()
    out += ["", f"3. VERIFICAR LOCALIZACAO ({len(unknown)})"]
    out += [line for row in unknown for line in _job_lines(row, warnings)] or ["  nenhuma"]

    pending = conn.execute("SELECT COUNT(*) FROM review_queue WHERE status='pending_review'").fetchone()[0]
    open_alerts = len(list_alerts(conn))
    restricted = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='new' AND location_status='restricted'"
    ).fetchone()[0]
    out += [
        "",
        "4. RESUMO",
        f"  fila de revisao pendente: {pending}",
        f"  alertas de duplicidade em aberto: {open_alerts} (nao significam candidatura anterior)",
        f"  vagas restritas excluidas da selecao: {restricted}",
        "",
        f"5. NOTA MAXIMA EFETIVA: {MAX_EFFECTIVE_SCORE}/100 - a comparacao de formacao ainda nao existe (0 de 10).",
        "   A nota organiza vagas; nao e probabilidade de contratacao.",
    ]
    return "\n".join(out)


def format_alerts(conn, include_acknowledged: bool = False) -> str:
    alerts = list_alerts(conn, include_acknowledged)
    if not alerts:
        return "Nenhum alerta de duplicidade."
    lines = []
    for a in alerts:
        mark = " [revisado]" if a["acknowledged"] else ""
        lines.append(
            f"#{a['id']} {a['confidence']} ({a['match_type']}){mark} - {a['original_title']} / {a['original_company']}"
        )
        lines.append(f"    encontrada em: {a['source_found']} {a['url_found']}".rstrip())
        if a["found_title"]:
            lines.append(f"    vaga encontrada: {a['found_title']}")
        if a["applied_flag"]:
            lines.append("    ATENCAO: ligada a uma vaga ja inscrita (already_applied)")
    return "\n".join(lines)
