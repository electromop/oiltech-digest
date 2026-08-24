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


GLOSSARY: tuple[GlossaryTerm, ...] = (
    GlossaryTerm(("hydraulic fracturing", "fracking", "fracing", "frac stimulation", "hydraulic stimulation"), "ГРП", "гидроразрыв пласта", ("фракинг",), "Для дайджеста используем отраслевой термин ГРП."),
    GlossaryTerm(("well completion", "completion", "completions", "well completions"), "заканчивание скважины", None, ("завершение скважины",), "Completion в контексте скважин переводится как заканчивание."),
    GlossaryTerm(("workover", "workovers", "well workover"), "КРС", "капитальный ремонт скважин", ("ворковер",), "Workover в нефтегазовом контексте — КРС."),
    GlossaryTerm(("enhanced oil recovery", "eor"), "МУН", "методы увеличения нефтеотдачи", (), "EOR допустимо оставлять как аббревиатуру только рядом с МУН."),
    GlossaryTerm(("artificial lift",), "механизированная добыча", None, ("искусственный лифт",), ""),
    GlossaryTerm(("drilling fluid", "drilling mud", "mud system"), "буровой раствор", None, ("буровая грязь",), ""),
    GlossaryTerm(("managed pressure drilling", "mpd"), "бурение с управляемым давлением", None, (), ""),
    GlossaryTerm(("measurement while drilling", "mwd"), "MWD", "измерения в процессе бурения", (), ""),
    GlossaryTerm(("logging while drilling", "lwd"), "LWD", "каротаж в процессе бурения", (), ""),
    GlossaryTerm(("coiled tubing",), "колтюбинг", None, ("гибкая труба",), "В отраслевом тексте предпочтительно колтюбинг."),
    GlossaryTerm(("proppant", "proppants"), "проппант", None, (), ""),
    GlossaryTerm(("flowback", "flowback fluid"), "жидкость обратного притока", None, ("флоубэк",), ""),
    GlossaryTerm(("produced water",), "попутно добываемая вода", None, (), ""),
    GlossaryTerm(("subsea", "subsea production"), "подводная добыча", None, ("сабси",), ""),
    GlossaryTerm(("offshore",), "шельфовый", None, ("оффшорный",), "В нефтегазовом контексте обычно шельфовый/морской."),
    GlossaryTerm(("upstream",), "разведка и добыча", None, ("апстрим",), ""),
    GlossaryTerm(("midstream",), "транспортировка и хранение", None, ("мидстрим",), ""),
    GlossaryTerm(("downstream",), "переработка и сбыт", None, ("даунстрим",), ""),
    GlossaryTerm(("liquefied natural gas", "lng"), "СПГ", "сжиженный природный газ", (), ""),
    GlossaryTerm(("carbon capture and storage", "ccs"), "CCS", "улавливание и хранение CO2", (), ""),
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
        forbidden = f" | forbidden_ru: {', '.join(term.forbidden_ru)}" if term.forbidden_ru else ""
        note = f" | note: {term.note}" if term.note else ""
        lines.append(f"- source: {aliases} -> preferred_ru: {term.preferred_ru}{full}{forbidden}{note}")
    return "\n".join(lines)


def enforce_glossary_text(text: str, article: dict) -> str:
    """Apply safe deterministic replacements for known bad Russian terms."""
    result = text or ""
    for term in relevant_glossary_terms(article):
        for forbidden in term.forbidden_ru:
            result = _replace_case_insensitive(result, forbidden, term.preferred_ru)
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
    return warnings


def _article_text(article: dict) -> str:
    return " ".join(
        str(article.get(field) or "")
        for field in ("title", "title_ru", "summary", "raw_text", "source_category")
    ).lower()


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
