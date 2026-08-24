"""Oil and gas terminology guardrails for Russian summaries and titles."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class GlossaryTerm:
    source_terms: tuple[str, ...]
    preferred_ru: str
    full_ru: str | None = None
    forbidden_ru: tuple[str, ...] = ()
    note: str = ""
    forbidden_patterns: tuple[str, ...] = ()


GLOSSARY: tuple[GlossaryTerm, ...] = (
    GlossaryTerm(("hydraulic fracturing", "fracking", "fracing", "frac stimulation", "hydraulic stimulation", "electric frac", "frac fleet"), "ГРП", "гидроразрыв пласта", ("фракинг",), "Для дайджеста используем отраслевой термин ГРП.", forbidden_patterns=(r"\bфрак(?:инг|ингу|ингом|инга|инге|ингов|инги|овый|овые|овая|овое)\b",)),
    GlossaryTerm(("well completion", "completion", "completions", "well completions", "intelligent completion", "smart completion"), "заканчивание скважины", None, ("завершение скважины", "завершения скважины", "завершении скважины"), "Completion в контексте скважин переводится как заканчивание."),
    GlossaryTerm(("workover", "workovers", "well workover", "workover rig"), "КРС", "капитальный ремонт скважин", ("ворковер",), "Workover в нефтегазовом контексте — КРС.", forbidden_patterns=(r"\bворковер(?:а|ов|ом|е|ы)?\b",)),
    GlossaryTerm(("enhanced oil recovery", "eor", "improved oil recovery", "ior"), "МУН", "методы увеличения нефтеотдачи", ("улучшенное извлечение нефти",), "EOR/IOR в дайджесте нормализуем как МУН."),
    GlossaryTerm(("artificial lift", "esp", "electric submersible pump", "rod lift", "gas lift"), "механизированная добыча", None, ("искусственный лифт",)),
    GlossaryTerm(("drilling fluid", "drilling mud", "mud system", "mud motor"), "буровой раствор", None, ("буровая грязь",)),
    GlossaryTerm(("managed pressure drilling", "mpd"), "бурение с управляемым давлением"),
    GlossaryTerm(("measurement while drilling", "mwd"), "MWD", "измерения в процессе бурения"),
    GlossaryTerm(("logging while drilling", "lwd"), "LWD", "каротаж в процессе бурения"),
    GlossaryTerm(("coiled tubing", "ct intervention"), "колтюбинг", None, ("гибкая труба", "свернутая труба"), "В отраслевом тексте предпочтительно колтюбинг."),
    GlossaryTerm(("proppant", "proppants"), "проппант"),
    GlossaryTerm(("flowback", "flowback fluid"), "жидкость обратного притока", None, ("флоубэк",), forbidden_patterns=(r"\bфлоубэк(?:а|ом|е)?\b",)),
    GlossaryTerm(("produced water",), "попутно добываемая вода"),
    GlossaryTerm(("subsea", "subsea production", "subsea tieback"), "подводная добыча", None, ("сабси",), forbidden_patterns=(r"\bсабси\b",)),
    GlossaryTerm(("offshore", "offshore drilling", "offshore production"), "шельфовый", None, ("оффшорный", "оффшоре", "оффшора"), "В нефтегазовом контексте обычно шельфовый/морской.", forbidden_patterns=(r"\bоффшор(?:ный|ная|ное|ные|е|а|ом)?\b",)),
    GlossaryTerm(("upstream",), "разведка и добыча", None, ("апстрим",)),
    GlossaryTerm(("midstream",), "транспортировка и хранение", None, ("мидстрим",)),
    GlossaryTerm(("downstream",), "переработка и сбыт", None, ("даунстрим",)),
    GlossaryTerm(("liquefied natural gas", "lng"), "СПГ", "сжиженный природный газ"),
    GlossaryTerm(("carbon capture and storage", "ccs"), "CCS", "улавливание и хранение CO2"),
    GlossaryTerm(("carbon capture utilization and storage", "ccus"), "CCUS", "улавливание, использование и хранение CO2"),
    GlossaryTerm(("carbon dioxide", "co2"), "CO2", "диоксид углерода"),
    GlossaryTerm(("drill bit", "drilling bit"), "буровое долото"),
    GlossaryTerm(("bottomhole assembly", "bha"), "КНБК", "компоновка низа бурильной колонны"),
    GlossaryTerm(("rate of penetration", "rop"), "механическая скорость проходки"),
    GlossaryTerm(("wellbore", "well bore"), "ствол скважины", None, ("скважинный ствол",)),
    GlossaryTerm(("casing", "casing string"), "обсадная колонна"),
    GlossaryTerm(("cementing", "well cementing"), "цементирование скважины"),
    GlossaryTerm(("perforation", "perforating"), "перфорация"),
    GlossaryTerm(("stimulation", "well stimulation"), "интенсификация притока", None, ("стимуляция скважины",), forbidden_patterns=(r"\bстимуляци(?:я|и|ю|ей)\s+скважин(?:ы|е|ой)?\b",)),
    GlossaryTerm(("reservoir", "reservoir management"), "пласт", None, ("резервуар",), "Reservoir в добыче — пласт, не резервуар.", forbidden_patterns=(r"\bрезервуар(?:а|у|ом|е|ы|ов|ам|ами|ах)?\b",)),
    GlossaryTerm(("digital twin", "digital twins"), "цифровой двойник"),
    GlossaryTerm(("predictive maintenance",), "предиктивное обслуживание"),
    GlossaryTerm(("condition monitoring",), "мониторинг состояния оборудования"),
)


def relevant_glossary_terms(article: dict, *, limit: int = 12) -> list[GlossaryTerm]:
    text = _article_text(article)
    matches = [term for term in GLOSSARY if any(_contains_term(text, source) for source in term.source_terms)]
    return matches[:limit]


def glossary_prompt_block(article: dict, *, limit: int = 12) -> str:
    terms = relevant_glossary_terms(article, limit=limit)
    if not terms:
        return ""
    lines = [
        "domain_glossary:",
        "Используй эти нефтегазовые термины строго. Если термин встречается в тексте, применяй preferred_ru; forbidden_ru не используй.",
    ]
    for term in terms:
        aliases = ", ".join(term.source_terms[:5])
        full = f" | full_ru: {term.full_ru}" if term.full_ru else ""
        forbidden_items = list(term.forbidden_ru) + [pattern.replace(r"\b", "").replace("(?:", "(") for pattern in term.forbidden_patterns]
        forbidden = f" | forbidden_ru: {', '.join(forbidden_items[:5])}" if forbidden_items else ""
        note = f" | note: {term.note}" if term.note else ""
        lines.append(f"- source: {aliases} -> preferred_ru: {term.preferred_ru}{full}{forbidden}{note}")
    return "\n".join(lines)


def enforce_glossary_text(text: str, article: dict) -> str:
    """Apply safe deterministic replacements for known bad Russian terms."""
    result = text or ""
    for term in relevant_glossary_terms(article):
        for forbidden in term.forbidden_ru:
            result = _replace_case_insensitive(result, forbidden, term.preferred_ru)
        for pattern in _forbidden_patterns(term):
            result = re.sub(pattern, term.preferred_ru, result, flags=re.I)
    return result


def terminology_warnings(text: str, article: dict) -> list[dict[str, str]]:
    warnings = []
    lower = (text or "").lower()
    for term in relevant_glossary_terms(article):
        for forbidden in term.forbidden_ru:
            if forbidden.lower() in lower:
                warnings.append({
                    "forbidden_ru": forbidden,
                    "preferred_ru": term.preferred_ru,
                    "source_terms": ", ".join(term.source_terms[:5]),
                })
        for pattern in _forbidden_patterns(term):
            if re.search(pattern, text or "", flags=re.I):
                warnings.append({
                    "forbidden_ru": pattern,
                    "preferred_ru": term.preferred_ru,
                    "source_terms": ", ".join(term.source_terms[:5]),
                })
    return warnings


def _article_text(article: dict) -> str:
    return " ".join(
        str(article.get(field) or "")
        for field in ("title", "title_ru", "summary", "raw_text", "source_category")
    ).lower()


def _forbidden_patterns(term: GlossaryTerm) -> tuple[str, ...]:
    return term.forbidden_patterns if isinstance(term.forbidden_patterns, tuple) else ()


def _contains_term(text: str, term: str) -> bool:
    term = (term or "").strip().lower()
    if not term:
        return False
    if len(term) <= 4:
        return re.search(rf"(?<![a-zа-я0-9]){re.escape(term)}(?![a-zа-я0-9])", text, flags=re.I) is not None
    if re.search(r"\s", term):
        return term in text
    return re.search(rf"\b{re.escape(term)}\b", text, flags=re.I) is not None


def _replace_case_insensitive(text: str, old: str, new: str) -> str:
    return re.sub(rf"(?<![А-Яа-яA-Za-z0-9]){re.escape(old)}(?![А-Яа-яA-Za-z0-9])", new, text, flags=re.I)
