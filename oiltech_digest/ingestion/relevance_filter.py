"""Cheap deterministic pre-filter for obviously irrelevant RSS items.

The goal is not to replace AI relevance. It only blocks clear noise such as
sports, entertainment and generic incidents when there is no oil/gas, energy,
industrial or business-development signal in the RSS title/summary.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class PreFilterResult:
    keep: bool
    reason: str
    matched_keywords: tuple[str, ...] = ()
    matched_noise: tuple[str, ...] = ()


POSITIVE_KEYWORDS = (
    # RU oil & gas core
    "нефт", "газ", "газов", "газпром", "роснефт", "лукойл", "татнефт",
    "сургутнефт", "новатэк", "сибур", "транснефт", "нефтесервис",
    "нефтегаз", "тэк", "топливно-энергет", "энергетик",
    "месторожд", "скважин", "бурени", "буров", "грп", "гидроразрыв",
    "добыч", "разведк", "геологоразвед", "сейсмик", "пласт", "коллектор",
    "керн", "шельф", "upstream", "downstream", "midstream",
    "нефтепровод", "газопровод", "трубопровод", "спг", "сжиженн",
    "lng", "водород", "нефтехим", "петрохим", "переработк", "нпз",
    "завод", "терминал", "танкер", "проппант", "цементирован",
    "телеметр", "геонавигац", "каротаж", "заканчиван", "интенсификац",
    "пнд", "ппд", "пнд", "пнд", "капремонт", "криогенн",
    # RU industrial / adjacent signals we should keep for AI to judge
    "промышлен", "индустри", "производств", "оборудован", "компрессор",
    "насос", "турбин", "генерац", "электростанц", "аэс", "тэц", "гэс",
    "лэп", "подстанц", "сети", "россети", "автоматизац", "цифровизац",
    "искусственн", "робот", "датчик", "кибер", "импортозамещ",
    "логистик", "контракт", "подряд", "сделк", "m&a", "инвестиц",
    "санкц", "экспорт", "импорт", "судостро", "машиностро", "металлург",
    "горнодобы", "уголь", "минеральн", "критическ", "редкозем",
    # EN oil & gas core
    "oil", "gas", "petroleum", "petrochemical", "hydrocarbon", "energy",
    "oilfield", "drilling", "well", "wellbore", "reservoir", "completion",
    "stimulation", "fracturing", "fracking", "frac", "proppant", "cementing",
    "wireline", "logging", "seismic", "geoscience", "geothermal",
    "pipeline", "midstream", "refinery", "refining", "lng", "flng",
    "upstream", "offshore", "onshore", "subsea", "decommissioning",
    "production", "exploration", "operator", "rig", "epc", "opec",
    # EN industrial / adjacent
    "industrial", "manufacturing", "automation", "digital twin", "ai",
    "sensor", "robot", "cybersecurity", "power grid", "utility", "nuclear",
    "hydrogen", "carbon capture", "ccus", "renewable", "contract",
    "supply chain", "logistics", "sanction", "export", "import", "mining",
    "critical minerals", "rare earth", "steel", "turbine", "compressor",
)


NOISE_KEYWORDS = (
    # sports
    "футбол", "хоккей", "баскетбол", "волейбол", "гандбол", "теннис",
    "спорт", "спортсмен", "спортсменк", "матч", "суперлиг", "чемпионат",
    "кубок", "олимпи", "football", "soccer", "hockey", "basketball",
    "handball", "tennis", "match", "league", "cup", "olympic",
    # entertainment/culture/lifestyle
    "музей", "театр", "кино", "фильм", "актёр", "актер", "актрис",
    "певец", "певиц", "концерт", "фестивал", "выставк", "искусств",
    "ресторан", "туризм", "путешеств", "гороскоп", "museum", "theatre",
    "theater", "movie", "film", "actor", "actress", "concert", "festival",
    "restaurant", "travel", "horoscope",
    # generic crime/incidents/transport when no industrial signal exists
    "убийств", "пожар", "дтп", "авария", "происшеств", "суд арест",
    "аэропорт", "рейс", "самолет", "самолёт", "бпла", "дрон", "атака",
    "взрыв", "эвакуац", "crime", "murder", "airport", "flight", "plane",
    "drone", "attack", "explosion", "evacuation",
    # generic politics/public life
    "выбор", "депутат", "парламент", "партия", "митинг", "протест",
    "election", "parliament", "protest",
)


_WORD_RE = re.compile(r"\s+")


def should_keep_article(title: str, summary: str = "", source: dict | None = None) -> PreFilterResult:
    article_text = _normalize(" ".join([title or "", summary or ""]))
    source_text = _normalize(" ".join([
        title or "",
        summary or "",
        (source or {}).get("name") or "",
        (source or {}).get("category") or "",
        (source or {}).get("source_type") or "",
    ]))
    article_positive = _matches(article_text, POSITIVE_KEYWORDS)
    positive = article_positive or _matches(source_text, POSITIVE_KEYWORDS)
    noise = _matches(article_text, NOISE_KEYWORDS)

    if noise and not article_positive:
        return PreFilterResult(False, "obvious non-domain noise without positive signal", (), noise)
    if positive:
        return PreFilterResult(True, "domain keyword matched", positive, noise)
    return PreFilterResult(True, "no strong negative signal")


def _matches(text: str, keywords: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(keyword for keyword in keywords if _keyword_in_text(text, keyword))


def _keyword_in_text(text: str, keyword: str) -> bool:
    if not keyword:
        return False
    if keyword.isascii() and keyword.replace(" ", "").replace("&", "").isalnum():
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None
    return keyword in text


def _normalize(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    return _WORD_RE.sub(" ", text).strip()
