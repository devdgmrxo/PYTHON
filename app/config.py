"""Caminhos e constantes do JobHunter."""

import logging
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"

DB_PATH = DATA_DIR / "jobhunter.db"
PROFILE_PATH = DATA_DIR / "profile.json"
BOARDS_PATH = DATA_DIR / "greenhouse_boards.json"
LOCK_PATH = DATA_DIR / "jobhunter.lock"
LOG_PATH = LOGS_DIR / "jobhunter.log"

REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
REMOTIVE_CATEGORY = "software-dev"
REMOTIVE_DAILY_LIMIT = 4

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"

HTTP_TIMEOUT = 30
USER_AGENT = "JobHunter/0.1 (busca pessoal de vagas; uso local)"


def setup_logging(level: int = logging.INFO) -> None:
    """Configura log em arquivo (logs/jobhunter.log) e no console."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger("jobhunter")
    root.setLevel(level)
    if root.handlers:
        return
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(file_handler)
    root.addHandler(console)
