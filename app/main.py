"""Linha de comando do JobHunter.

Uso:
    python -m app.main init-db
    python -m app.main import ARQUIVO.json
    python -m app.main run [--source all|remotive|greenhouse|none]
    python -m app.main report
    python -m app.main alerts [--all] [--ack ID]
"""

import argparse
import logging
import sys

from app import config
from app.collectors.greenhouse import collect_greenhouse, load_boards
from app.collectors.remotive import collect_remotive
from app.database.db import init_db
from app.database.jobs import acknowledge_alert
from app.importer.panel_importer import import_panel_file
from app.lock import LockError, RunLock
from app.matching.scorer import apply_scores
from app.profile import load_profile
from app.report import build_report, format_alerts

log = logging.getLogger("jobhunter.main")


def _cmd_init_db(args) -> int:
    init_db(config.DB_PATH).close()
    print(f"Banco pronto em {config.DB_PATH}")
    return 0


def _cmd_import(args) -> int:
    conn = init_db(config.DB_PATH)
    try:
        res = import_panel_file(conn, args.file, config.PROFILE_PATH)
    finally:
        conn.close()
    print(
        f"Importado: {res.applied_imported} ja inscritas, {res.queue_imported} na fila de revisao, "
        f"{res.skipped_empty} registros vazios ignorados, {res.alerts_created} alertas, "
        f"perfil {'atualizado' if res.profile_updated else 'sem mudancas'}."
    )
    return 0


def _cmd_run(args) -> int:
    conn = init_db(config.DB_PATH)
    try:
        profile = load_profile(config.PROFILE_PATH)
        if args.source in ("all", "remotive"):
            res = collect_remotive(conn)
            print(f"Remotive: {res.status}, {res.jobs_found} vagas, {res.jobs_new} novas/vinculadas"
                  + (f" ({res.error})" if res.error else ""))
        if args.source in ("all", "greenhouse"):
            boards = load_boards(config.BOARDS_PATH)
            if not boards:
                print("Greenhouse: nenhum board configurado em data/greenhouse_boards.json")
            for res in collect_greenhouse(conn, boards):
                print(f"Greenhouse {res.target}: {res.status}, {res.jobs_found} vagas"
                      + (f" ({res.error})" if res.error else ""))
        apply_scores(conn, profile["preferences"].get("keywords", []))
        print()
        print(build_report(conn, profile))
    finally:
        conn.close()
    return 0


def _cmd_report(args) -> int:
    conn = init_db(config.DB_PATH)
    try:
        print(build_report(conn, load_profile(config.PROFILE_PATH)))
    finally:
        conn.close()
    return 0


def _cmd_alerts(args) -> int:
    conn = init_db(config.DB_PATH)
    try:
        if args.ack is not None:
            if not acknowledge_alert(conn, args.ack):
                print(f"Alerta #{args.ack} nao encontrado.")
                return 1
            print(f"Alerta #{args.ack} marcado como revisado (mantido no historico).")
        print(format_alerts(conn, include_acknowledged=args.all))
    finally:
        conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobhunter", description="Busca e organizacao de vagas (Fase 1).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="cria o banco").set_defaults(func=_cmd_init_db)
    p_import = sub.add_parser("import", help="importa o JSON exportado pelo painel")
    p_import.add_argument("file")
    p_import.set_defaults(func=_cmd_import)
    p_run = sub.add_parser("run", help="coleta, pontua e mostra o relatorio")
    p_run.add_argument("--source", choices=["all", "remotive", "greenhouse", "none"], default="all")
    p_run.set_defaults(func=_cmd_run)
    sub.add_parser("report", help="mostra o relatorio sem coletar").set_defaults(func=_cmd_report)
    p_alerts = sub.add_parser("alerts", help="lista alertas de duplicidade")
    p_alerts.add_argument("--all", action="store_true", help="inclui alertas ja revisados")
    p_alerts.add_argument("--ack", type=int, help="marca o alerta como revisado")
    p_alerts.set_defaults(func=_cmd_alerts)
    return parser


# comandos que gravam no banco/perfil e por isso usam a trava
LOCKED_COMMANDS = {"init-db", "import", "run", "alerts"}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config.setup_logging(logging.WARNING)
    try:
        if args.command in LOCKED_COMMANDS:
            with RunLock(config.LOCK_PATH):
                return args.func(args)
        return args.func(args)
    except LockError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        print(f"Se tiver certeza de que nao ha outra execucao, confira e remova manualmente: {config.LOCK_PATH}",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
