"""Pontuação de compatibilidade e classificação geográfica.

A nota NÃO é probabilidade de contratação: é só um filtro de organização.
Enquanto a formação não for comparada de verdade (peso 10 fixo em 0), a nota
máxima efetiva é 90.
"""

import json
import re
from dataclasses import dataclass, field

from app.collectors.common import normalize_text
from app.database.db import transaction

WEIGHTS = {"keywords": 30, "role": 20, "seniority": 25, "location": 15, "education": 10}
MAX_EFFECTIVE_SCORE = 90  # formação ainda não implementada (0 de 10)

_WORD = r"(?<![a-z0-9_]){}(?![a-z0-9_])"


def keyword_pattern(keyword: str):
    """Regex com limites de palavra, correta para C++, C#, .NET e Node.js.

    'git' não casa 'digital', 'java' não casa 'javascript', 'react' não casa
    'reactive' e 'c' não casa 'c++'/'c#'.
    """
    kw = normalize_text(keyword)
    if not kw:
        return None
    left = "" if kw.startswith(".") else r"(?<![a-z0-9_])"
    return re.compile(left + re.escape(kw) + r"(?![a-z0-9_+#])")


def find_keywords(text: str, keywords: list[str]) -> list[str]:
    normalized = normalize_text(text)
    found = []
    for keyword in keywords:
        pattern = keyword_pattern(keyword)
        if pattern and pattern.search(normalized):
            found.append(keyword)
    return found


# ---------------------------------------------------------------- localização
_COUNTRY = (
    r"(?:usa|us|united states(?: of america)?|north america|uk|united kingdom|eu|european union|europe|"
    r"emea|apac|canada|mexico|australia|india|germany|france|spain|portugal|netherlands|ireland|poland)"
)
_RESTRICTED_PATTERNS = [
    re.compile(rf"\b{_COUNTRY}(?: based)? only\b"),
    re.compile(rf"\b{_COUNTRY} (?:residents?|citizens?) only\b"),
    re.compile(rf"\bmust (?:be )?(?:legally |currently )?(?:authorized|eligible) to work (?:legally )?in (?:the )?{_COUNTRY}\b"),
    re.compile(rf"\bmust (?:currently )?(?:reside|live|be located|be based|be a resident) in (?:the )?{_COUNTRY}\b"),
    re.compile(rf"\b(?:work authorization|right to work) (?:in|for) (?:the )?{_COUNTRY}\b"),
    re.compile(rf"\b(?:remote|hybrid)[^a-z0-9]{{1,4}}{_COUNTRY}\b"),
    re.compile(rf"\b{_COUNTRY} (?:residents?|citizens?)\b"),
]
_BRAZIL_RE = re.compile(r"\b(?:brasil|brazil)\b")
_BR_CITY_RE = re.compile(
    r"\b(?:sao paulo|rio de janeiro|belo horizonte|curitiba|porto alegre|florianopolis|"
    r"recife|brasilia|salvador|fortaleza|campinas)\b"
)


@dataclass
class LocationResult:
    status: str  # allowed_brazil | unknown | restricted
    reason: str


def _loc_normalize(value: str | None) -> str:
    text = normalize_text(value)
    return re.sub(r"\b([a-z])\.([a-z])\.", r"\1\2", text)  # u.s. -> us


def classify_location(location: str | None, description: str | None = None, is_remote=None) -> LocationResult:
    loc = _loc_normalize(location)
    combined = f"{loc} {_loc_normalize(description)}".strip()

    restricted = None
    for pattern in _RESTRICTED_PATTERNS:
        match = pattern.search(combined)
        if match:
            restricted = match.group(0)
            break
    brazil = bool(_BRAZIL_RE.search(combined)) or bool(_BR_CITY_RE.search(loc))

    if restricted and brazil:
        return LocationResult("unknown", f"conflito: cita Brasil e restricao '{restricted}'; verificar")
    if restricted:
        return LocationResult("restricted", f"restricao explicita: '{restricted}'")
    if brazil:
        return LocationResult("allowed_brazil", "Brasil citado explicitamente")
    if is_remote or not loc or "remote" in loc or "remoto" in loc:
        return LocationResult("unknown", "remoto sem paises informados; verificar localizacao")
    return LocationResult("unknown", f"localizacao '{location}' nao confirma o Brasil; verificar")


# ------------------------------------------------------------------ pontuação
_ROLE_RE = re.compile(
    r"\b(?:desenvolvedor\w*|developer\w*|programador\w*|engenheir[oa] de software|software engineer\w*|"
    r"dev|backend|back end|frontend|front end|full ?stack|web developer|analista desenvolvedor)\b"
)
_JUNIOR_RE = re.compile(
    r"\b(?:estagi\w*|intern|interns|internship|trainee|junior|jr|entry level|entry-level|aprendiz)\b"
)
_SENIOR_RE = re.compile(
    r"\b(?:senior|sr|lead|staff|principal|especialista|gerente|manager|head|coordenador|"
    r"diretor|architect|arquiteto)\b"
)
_MID_RE = re.compile(r"\b(?:pleno|mid|mid level|mid-level|midlevel)\b")
_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:years|yrs|anos)")


@dataclass
class ScoreResult:
    total: int
    components: dict[str, int] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    location_status: str = "unknown"
    max_effective: int = MAX_EFFECTIVE_SCORE

    def to_json(self) -> str:
        return json.dumps(
            {
                "total": self.total,
                "components": self.components,
                "reasons": self.reasons,
                "location_status": self.location_status,
                "max_effective": self.max_effective,
            },
            ensure_ascii=False,
        )


def _seniority(title: str, description: str) -> tuple[int, str]:
    junior_t, senior_t, mid_t = (bool(r.search(title)) for r in (_JUNIOR_RE, _SENIOR_RE, _MID_RE))
    if senior_t and not junior_t:
        return 0, "titulo indica nivel senior/lideranca"
    if junior_t and (senior_t or mid_t):
        return 12, "titulo cita estagio/junior junto com outro nivel"
    if junior_t:
        return 25, "titulo indica estagio/junior"
    if mid_t:
        return 6, "titulo indica nivel pleno"
    if _JUNIOR_RE.search(description):
        return 18, "descricao cita estagio/junior"
    years = [int(n) for n in _YEARS_RE.findall(description)]
    if years and max(years) >= 5:
        return 0, f"descricao pede {max(years)}+ anos de experiencia"
    if years and max(years) >= 3:
        return 4, f"descricao pede {max(years)}+ anos de experiencia"
    return 12, "nivel nao informado"


def score_job(title: str, description: str | None, location: str | None, is_remote, keywords: list[str]) -> ScoreResult:
    """Calcula a nota (0-100, efetiva até 90) e os motivos."""
    norm_title = normalize_text(title)
    norm_desc = normalize_text(description)
    reasons: list[str] = []
    comp: dict[str, int] = {}

    kws = [k for k in keywords if normalize_text(k)]
    if kws:
        found = find_keywords(f"{title} {description or ''}", kws)
        target = min(len(kws), 5)
        comp["keywords"] = round(WEIGHTS["keywords"] * min(1.0, len(found) / target))
        reasons.append(
            f"palavras-chave: {len(found)}/{len(kws)} ({', '.join(found) or 'nenhuma'}) = {comp['keywords']}/30"
        )
    else:
        comp["keywords"] = 0
        reasons.append("palavras-chave: perfil sem palavras-chave = 0/30")

    if _ROLE_RE.search(norm_title):
        comp["role"], why = 20, "funcao de desenvolvimento no titulo"
    elif _ROLE_RE.search(norm_desc[:800]):
        comp["role"], why = 8, "funcao de desenvolvimento so na descricao"
    else:
        comp["role"], why = 0, "funcao nao parece ser de desenvolvimento"
    reasons.append(f"funcao: {why} = {comp['role']}/20")

    comp["seniority"], why = _seniority(norm_title, norm_desc)
    reasons.append(f"senioridade: {why} = {comp['seniority']}/25")

    loc = classify_location(location, description, is_remote)
    comp["location"] = {"allowed_brazil": 15, "unknown": 5, "restricted": 0}[loc.status]
    reasons.append(f"localizacao ({loc.status}): {loc.reason} = {comp['location']}/15")

    comp["education"] = 0
    reasons.append("formacao: comparacao ainda nao implementada = 0/10 (nota maxima efetiva: 90)")

    return ScoreResult(sum(comp.values()), comp, reasons, loc.status)


def apply_scores(conn, keywords: list[str]) -> int:
    """Recalcula nota e localização de todas as vagas e grava no banco."""
    rows = conn.execute("SELECT id, title, description, location, is_remote FROM jobs").fetchall()
    with transaction(conn):
        for row in rows:
            result = score_job(row["title"], row["description"], row["location"], row["is_remote"], keywords)
            conn.execute(
                "UPDATE jobs SET score=?, score_details=?, location_status=? WHERE id=?",
                (result.total, result.to_json(), result.location_status, row["id"]),
            )
    return len(rows)
