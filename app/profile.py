"""Leitura, gravação atômica e mesclagem do perfil (data/profile.json)."""

import copy
import json
import os
from pathlib import Path

DEFAULT_PROFILE = {
    "schema_version": 1,
    "personal": {
        "full_name": "",
        "first_name": "",
        "last_name": "",
        "email": "",
        "phone": "",
        "city": "",
        "state": "",
        "country": "Brasil",
        "linkedin": "",
        "github": "",
        "website": "",
    },
    "preferences": {
        "keywords": [],
        "minimum_score": 70,
        "daily_application_limit": 10,
        "remote": "qualquer",
        "international_remote": True,
    },
    "education": [],
    "skills": [],
    "experience": [],
    "projects": [],
    "languages": [],
    "documents": {"resume_pt": "", "resume_en": ""},
    "additional_answers": {},
}

VALID_REMOTE = {"qualquer", "remoto", "presencial"}

# painel -> perfil interno (campos pessoais: só preenche se estiver vazio)
PANEL_PERSONAL_MAP = {
    "nome": "full_name",
    "email": "email",
    "tel": "phone",
    "cidade": "city",
    "linkedin": "linkedin",
    "github": "github",
    "site": "website",
}


def load_profile(path: Path) -> dict:
    """Carrega o perfil; se não existir, devolve o modelo vazio (sem gravar)."""
    path = Path(path)
    if not path.exists():
        return copy.deepcopy(DEFAULT_PROFILE)
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} nao contem um objeto JSON")
    merged = copy.deepcopy(DEFAULT_PROFILE)
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def save_profile(path: Path, profile: dict) -> None:
    """Grava de forma atômica (arquivo temporário + os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def parse_keywords(value) -> list[str]:
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return []
    return [str(item).strip() for item in items if str(item).strip()]


def _to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def merge_panel_profile(profile: dict, panel: dict | None) -> dict:
    """Mescla o perfil do painel sem apagar nem sobrescrever dados manuais.

    - Campos pessoais: só preenchidos se estiverem vazios.
    - Palavras-chave: união (sem duplicar, ignorando maiúsculas).
    - minimum_score / daily_application_limit / remote: o valor do painel só é
      usado se o perfil ainda tiver o valor padrão (nunca foi alterado à mão).
    - education, skills, experience, projects, languages, documents e
      additional_answers nunca são tocados.
    """
    result = copy.deepcopy(profile)
    if not isinstance(panel, dict):
        return result

    personal = result.setdefault("personal", {})
    for panel_key, field_name in PANEL_PERSONAL_MAP.items():
        value = str(panel.get(panel_key) or "").strip()
        if value and not str(personal.get(field_name) or "").strip():
            personal[field_name] = value

    prefs = result.setdefault("preferences", {})
    existing = prefs.setdefault("keywords", [])
    seen = {str(k).strip().lower() for k in existing}
    for keyword in parse_keywords(panel.get("kw")):
        if keyword.lower() not in seen:
            existing.append(keyword)
            seen.add(keyword.lower())

    defaults = DEFAULT_PROFILE["preferences"]
    cut = _to_int(panel.get("cut"))
    if cut is not None and 0 <= cut <= 100 and prefs.get("minimum_score") == defaults["minimum_score"]:
        prefs["minimum_score"] = cut
    lim = _to_int(panel.get("lim"))
    if lim is not None and lim > 0 and prefs.get("daily_application_limit") == defaults["daily_application_limit"]:
        prefs["daily_application_limit"] = lim
    remote = str(panel.get("remote") or "").strip().lower()
    if remote in VALID_REMOTE and prefs.get("remote") == defaults["remote"]:
        prefs["remote"] = remote
    return result
