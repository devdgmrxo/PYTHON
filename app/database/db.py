"""Banco SQLite do JobHunter: conexão, esquema, transações.

Estratégia de versão: PRAGMA user_version guarda a versão do esquema.
- Banco novo (versão 0): o esquema completo é criado em uma transação.
- Mesma versão: nada a fazer.
- Versão maior que a conhecida: aborta (não abrir banco de versão futura).
- Mudanças futuras entram como migrações explícitas e aditivas em MIGRATIONS,
  somente com autorização. Nunca se recria ou apaga tabela com dados.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1


class DatabaseError(Exception):
    pass


class TransactionError(DatabaseError):
    pass


SCHEMA_STATEMENTS = [
    # Vagas. url_key, url e identificadores ausentes são NULL (nunca '').
    """
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL CHECK (title <> ''),
        company TEXT NOT NULL DEFAULT '',
        url TEXT CHECK (url IS NULL OR url <> ''),
        url_key TEXT CHECK (url_key IS NULL OR url_key <> ''),
        url_key_generic INTEGER NOT NULL DEFAULT 0 CHECK (url_key_generic IN (0, 1)),
        text_key TEXT NOT NULL CHECK (text_key <> ''),
        description TEXT,
        location TEXT,
        is_remote INTEGER CHECK (is_remote IS NULL OR is_remote IN (0, 1)),
        location_status TEXT NOT NULL DEFAULT 'unknown'
            CHECK (location_status IN ('allowed_brazil', 'unknown', 'restricted')),
        score INTEGER,
        score_details TEXT,
        status TEXT NOT NULL DEFAULT 'new'
            CHECK (status IN ('new', 'pending_review', 'already_applied', 'ignored')),
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        UNIQUE (url_key)
    )
    """,
    "CREATE INDEX idx_jobs_text_key ON jobs(text_key)",
    "CREATE INDEX idx_jobs_status ON jobs(status)",
    # Fontes em que cada vaga foi encontrada.
    """
    CREATE TABLE job_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
        source TEXT NOT NULL CHECK (source <> ''),
        external_id TEXT CHECK (external_id IS NULL OR external_id <> ''),
        url TEXT CHECK (url IS NULL OR url <> ''),
        url_key TEXT CHECK (url_key IS NULL OR url_key <> ''),
        attribution TEXT,
        found_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        UNIQUE (source, external_id)
    )
    """,
    "CREATE INDEX idx_job_sources_job ON job_sources(job_id)",
    "CREATE INDEX idx_job_sources_source_url ON job_sources(source, url_key)",
    # Candidaturas (Fase 1: apenas já realizadas, vindas do painel).
    """
    CREATE TABLE applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE RESTRICT,
        status TEXT NOT NULL DEFAULT 'already_applied'
            CHECK (status IN ('already_applied', 'applied')),
        applied_at TEXT,
        origin TEXT NOT NULL DEFAULT 'panel',
        created_at TEXT NOT NULL
    )
    """,
    # Fila de revisão humana.
    """
    CREATE TABLE review_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE RESTRICT,
        status TEXT NOT NULL DEFAULT 'pending_review'
            CHECK (status IN ('pending_review', 'reviewed', 'dismissed')),
        origin TEXT NOT NULL DEFAULT 'panel',
        added_at TEXT NOT NULL,
        reviewed_at TEXT
    )
    """,
    # Alertas de duplicidade (nunca apagados). url_found vazio = '' para a
    # restrição UNIQUE funcionar (NULL não colidiria).
    """
    CREATE TABLE duplicate_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_original_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
        job_found_id INTEGER REFERENCES jobs(id) ON DELETE RESTRICT,
        source_found TEXT NOT NULL,
        url_found TEXT NOT NULL DEFAULT '',
        match_type TEXT NOT NULL
            CHECK (match_type IN ('url', 'external_id', 'text_key', 'generic_url')),
        confidence TEXT NOT NULL CHECK (confidence IN ('confirmed', 'possible')),
        detected_at TEXT NOT NULL,
        acknowledged INTEGER NOT NULL DEFAULT 0 CHECK (acknowledged IN (0, 1)),
        UNIQUE (job_original_id, source_found, url_found, match_type)
    )
    """,
    "CREATE INDEX idx_alerts_ack ON duplicate_alerts(acknowledged)",
    # Histórico de consultas à Remotive (limite diário persistente).
    """
    CREATE TABLE remotive_queries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query_date TEXT NOT NULL,
        queried_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'started' CHECK (status IN ('started', 'ok', 'error')),
        jobs_returned INTEGER,
        error TEXT
    )
    """,
    "CREATE INDEX idx_remotive_date ON remotive_queries(query_date)",
    # Resultado de cada coleta (por fonte/empresa).
    """
    CREATE TABLE collector_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        target TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('ok', 'error')),
        jobs_found INTEGER NOT NULL DEFAULT 0,
        jobs_new INTEGER NOT NULL DEFAULT 0,
        error TEXT
    )
    """,
    # Histórico não pode ser apagado.
    """
    CREATE TRIGGER trg_applications_no_delete BEFORE DELETE ON applications
    BEGIN SELECT RAISE(ABORT, 'candidaturas nao podem ser apagadas'); END
    """,
    """
    CREATE TRIGGER trg_alerts_no_delete BEFORE DELETE ON duplicate_alerts
    BEGIN SELECT RAISE(ABORT, 'alertas nao podem ser apagados'); END
    """,
]

# Migrações futuras: {versão_destino: [statements]}. Vazio na versão 1.
MIGRATIONS: dict[int, list[str]] = {}


def now_utc_iso(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | Path) -> sqlite3.Connection:
    """Abre conexão com transações manuais e os PRAGMAs obrigatórios."""
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE ... COMMIT, com ROLLBACK em qualquer exceção.

    Transações aninhadas são rejeitadas.
    """
    if conn.in_transaction:
        raise TransactionError("transacao aninhada nao e permitida")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def init_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise DatabaseError(
            f"banco com versao {version} e mais novo que o programa ({SCHEMA_VERSION}); abortando"
        )
    if version == SCHEMA_VERSION:
        return
    with transaction(conn):
        if version == 0:
            existing = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
            if existing:
                raise DatabaseError(
                    "banco sem versao mas com tabelas: nao sera alterado; confira manualmente"
                )
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            version = 1
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS[target]:
                conn.execute(statement)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def init_db(path: str | Path) -> sqlite3.Connection:
    """Cria a pasta, abre a conexão e garante o esquema."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    init_schema(conn)
    return conn
