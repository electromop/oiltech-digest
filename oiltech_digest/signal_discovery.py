"""Технологический радар: находки поиска GPT для «Отбора» (#131, #138).

Прогон ищет материалы по темам (signal_research), докачивает страницы издателей, отсеивает технический брак поиска
и сводит находки одного события между собой, с карточками радара и с сигналами ленты («Дедуп»). Брать ли находку,
решает «Отбор». Прежняя цепочка (статьи корпуса, поиск Brave, судья, ревью пачки, проверка темы) снята 08.11: поля
её решений в базе остались и показываются как история («Почему такое решение», «Ход решения», «Отсеянные и скрытые»).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, datetime, timedelta
import hashlib
import json
import logging
import re
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

from oiltech_digest import config as app_config
from oiltech_digest import signal_dedup, signal_research
from oiltech_digest.db import repository, search_trial_store
from oiltech_digest.ingestion import publisher_page
from oiltech_digest.ingestion.normalize import url_key as article_url_key
from oiltech_digest.processing.domain_glossary import enforce_glossary_text
from oiltech_digest.processing.pipeline import make_client
from oiltech_digest.signal_card_pack import TEXT_LIMIT
from oiltech_digest.signal_feedback import (
    apply_feedback_glossary,
    known_titles_block,
    memory_snapshot_rows,
    use_memory_snapshot,
)

logger = logging.getLogger(__name__)


DEFAULT_RADAR_TOPICS = [
    {
        "name": "HSE robotics / Physical AI",
        "description": "Роботы и physical AI, которые убирают людей из опасных зон и операций.",
        "industry_scope": ["oil and gas", "mining", "metals", "chemicals", "industrial logistics"],
        "query_seeds": [
            "autonomous inspection robot hazardous area",
            "physical AI industrial safety",
            "robotic drilling removes workers from drill floor",
            "remote inspection robot environmental monitoring industrial site",
            "具身智能 油气 巡检机器人",
            "防爆巡检机器人 石油 天然气",
            "油气站场 无人巡检 机器人",
        ],
    },
    {
        "name": "Predictive HSE",
        "description": "Переход от реактивной безопасности к прогнозированию опасного состояния.",
        "industry_scope": ["oil and gas", "mining", "chemicals", "energy"],
        "query_seeds": [
            "predictive safety industrial operations",
            "preventive safety hazardous condition detection",
            "AI predicts safety incidents industrial equipment",
            "predictive maintenance HSE risk reduction",
            "预测性安全 矿山 油气",
            "安全生产 双重预防机制 人工智能",
            "设备状态监测 预测性维护 油田",
        ],
    },
    {
        "name": "Digital PTW / Control of Work",
        "description": "Dynamic PTW, LOTO, SIMOPS и continuous control assurance.",
        "industry_scope": ["oil and gas", "chemicals", "mining", "heavy industry"],
        "query_seeds": [
            "dynamic permit to work continuous control assurance",
            "digital PTW isolation certificate shift handover",
            "LOTO SIMOPS control of work software industrial safety",
            "permit to work system contractor competence integration",
            "电子作业票 特殊作业 安全生产",
            "作业许可 盲板 抽堵 动火 受限空间 数字化",
            "承包商 能力 作业许可 隔离 交接班",
        ],
    },
    {
        "name": "Industrial transport safety",
        "description": "Телематика, fatigue/distraction, collision avoidance и контроль опасных зон транспорта.",
        "industry_scope": ["oilfield service", "mining", "industrial logistics", "construction"],
        "query_seeds": [
            "heavy equipment collision avoidance mining safety",
            "fatigue detection industrial fleet safety",
            "AI video telematics hazardous industrial transport",
            "worker vehicle proximity detection industrial site",
            "矿卡 防碰撞 系统 安全",
            "疲劳驾驶监测 矿山 车辆",
            "人员车辆 防碰撞 露天矿",
        ],
    },
]

BUSINESS_RADAR_TOPICS = [
    {
        "name": "Сейсморазведка, ГРР и геолого-геофизические услуги",
        "description": "Поиск сигналов в разведке, сейсмике, интерпретации и геологическом сопровождении решений.",
        "industry_scope": ["exploration", "geoscience", "seismic", "oil and gas"],
        "query_seeds": [
            "AI seismic interpretation oil gas exploration",
            "seismic acquisition automation nodal seismic oil gas",
            "full waveform inversion cloud seismic interpretation",
            "fiber optic DAS seismic monitoring oilfield",
            "地震勘探 人工智能 油气 勘探",
            "物探 数字化 油气 勘探",
        ],
    },
    {
        "name": "ГИС, промысловая геофизика и петрофизика",
        "description": "Сигналы в каротаже, петрофизике, данных по пласту и диагностике качества скважин.",
        "industry_scope": ["wireline", "logging", "petrophysics", "reservoir"],
        "query_seeds": [
            "AI petrophysics automated log interpretation",
            "wireline formation evaluation machine learning",
            "borehole imaging AI reservoir characterization",
            "fiber optic well diagnostics production logging",
            "智能测井 解释 人工智能 油田",
            "岩心分析 数字化 测井 油气",
        ],
    },
    {
        "name": "Бурение, направленное бурение, растворы и буровое оборудование",
        "description": "Автоматизация строительства скважин, траекторное сопровождение, растворы, инструменты и оборудование.",
        "industry_scope": ["drilling", "directional drilling", "drilling fluids", "rig equipment"],
        "query_seeds": [
            "automated drilling rig closed loop drilling",
            "AI geosteering directional drilling deployment",
            "drilling fluids real time monitoring automation",
            "red zone automation drill floor pipe handling",
            "智能钻井 自动化钻机 油气",
            "钻井液 在线监测 自动化",
        ],
    },
    {
        "name": "Цементирование и изоляционные работы",
        "description": "Сигналы в креплении, зональной изоляции, герметичности и ликвидации перетоков.",
        "industry_scope": ["cementing", "zonal isolation", "well integrity"],
        "query_seeds": [
            "real time cementing automation well integrity",
            "self healing cement oil gas wells",
            "CO2 resistant cement carbon storage wells",
            "zonal isolation monitoring fiber optic cementing",
            "固井 自动化 井完整性 油气",
            "封隔 堵漏 水泥环 完整性",
        ],
    },
    {
        "name": "Заканчивание скважин",
        "description": "Компоновки заканчивания, внутрискважинное оборудование, intelligent completions и контроль притока.",
        "industry_scope": ["completions", "sand control", "downhole equipment"],
        "query_seeds": [
            "intelligent completion downhole control oil gas",
            "autonomous inflow control device deployment",
            "sand control completion technology field trial",
            "multistage completion monitoring fiber optic",
            "智能完井 井下控制 油田",
            "防砂 完井 工具 油气",
        ],
    },
    {
        "name": "ГРП, МГРП и стимуляция",
        "description": "Сигналы в дизайне и выполнении ГРП, оборудовании, химии, проппанте и мониторинге.",
        "industry_scope": ["hydraulic fracturing", "stimulation", "proppant"],
        "query_seeds": [
            "closed loop fracturing autonomous frac",
            "electric frac fleet field deployment",
            "fracturing fiber optic diagnostics real time",
            "proppant logistics automation oilfield",
            "智能压裂 自动化 压裂 油田",
            "电驱压裂 连续压裂 油气",
        ],
    },
    {
        "name": "КРС, ТКРС и well intervention",
        "description": "Ремонт, восстановление и вмешательства в скважину, включая rigless и coiled tubing.",
        "industry_scope": ["well intervention", "workover", "coiled tubing", "slickline"],
        "query_seeds": [
            "rigless well intervention automation",
            "coiled tubing real time downhole telemetry",
            "well intervention robotics oil gas",
            "live well intervention digital operations",
            "修井 自动化 连续油管 油田",
            "井下机器人 修井 油气",
        ],
    },
    {
        "name": "Добыча, механизированная добыча и внутрискважинное оборудование",
        "description": "Поддержание добычи, механизированная добыча, мониторинг оборудования и оптимизация фонда.",
        "industry_scope": ["production", "artificial lift", "downhole equipment"],
        "query_seeds": [
            "artificial lift optimization AI oilfield",
            "ESP predictive maintenance oil gas",
            "autonomous production optimization well pad",
            "production chemicals digital dosing automation",
            "智能采油 机械采油 优化",
            "电潜泵 预测性维护 油田",
        ],
    },
    {
        "name": "Повышение нефтеотдачи и химизация добычи",
        "description": "Методы повышения нефтеотдачи, химические сервисы, подготовка и защита оборудования.",
        "industry_scope": ["enhanced oil recovery", "production chemistry", "waterflood"],
        "query_seeds": [
            "enhanced oil recovery nanotechnology field trial",
            "polymer flooding digital optimization",
            "production chemistry AI corrosion scale inhibitor",
            "chemical EOR monitoring reservoir surveillance",
            "提高采收率 聚合物驱 油田",
            "油田化学剂 腐蚀 结垢 智能加药",
        ],
    },
    {
        "name": "Промысловая инфраструктура, surface facilities и проекты обустройства",
        "description": "Поверхностная инфраструктура, сбор, подготовка, измерение, управление потоками и безлюдные объекты.",
        "industry_scope": ["surface facilities", "midstream", "field infrastructure"],
        "query_seeds": [
            "unmanned oilfield facility remote operations",
            "surface facilities digital twin oil gas",
            "gas leak detection autonomous plant inspection",
            "edge AI oilfield facility monitoring",
            "无人站场 油气 智能巡检",
            "油气站场 泄漏检测 远程运维",
        ],
    },
    {
        "name": "Энергетика и промысловые энергосистемы",
        "description": "Энергоснабжение буровых, ГРП, кустов, удаленных объектов и промысловой инфраструктуры.",
        "industry_scope": ["oilfield power", "microgrid", "electrification", "energy systems"],
        "query_seeds": [
            "oilfield microgrid battery storage drilling rig",
            "electric frac power generation gas turbine",
            "rig electrification hybrid power oilfield",
            "remote oilfield power management microgrid",
            "油田 微电网 储能 供电",
            "电驱压裂 供电 油气",
        ],
    },
    {
        "name": "Роботизация и автономные системы",
        "description": "Физические роботы и автономные системы для бурения, инспекции, мониторинга и опасных операций.",
        "industry_scope": ["robotics", "autonomous systems", "industrial operations"],
        "query_seeds": [
            "autonomous inspection robot oil gas hazardous area",
            "explosion proof quadruped robot refinery oil depot",
            "autonomous drilling robot red zone removal",
            "subsea autonomous drone inspection oil gas",
            "防爆巡检机器人 油气 石化",
            "具身智能 油田 巡检",
        ],
    },
    {
        "name": "Логистика, транспорт и supply chain нефтесервисных операций",
        "description": "Материалы, техника, транспорт, вода, проппант, химия и supply chain для нефтесервисных операций.",
        "industry_scope": ["oilfield logistics", "industrial transport", "supply chain"],
        "query_seeds": [
            "driverless proppant logistics oilfield",
            "oilfield fleet safety fatigue monitoring",
            "industrial vehicle collision avoidance EMESRT Level 9",
            "oilfield water logistics optimization automation",
            "油服 物流 自动驾驶 运输",
            "矿卡 防碰撞 车辆干预",
        ],
    },
    {
        "name": "Экология, промышленная безопасность, HSE и устойчивое развитие",
        "description": "Безопасность, экология, отходы, выбросы, мониторинг и снижение промышленных рисков.",
        "industry_scope": ["HSE", "environment", "process safety", "sustainability"],
        "query_seeds": [
            "predictive HSE oil gas AI safety",
            "digital permit to work oil gas LOTO SIMOPS",
            "methane detection drone satellite oil gas",
            "produced water treatment reuse oilfield",
            "电子作业票 作业许可 石化 安全",
            "甲烷 泄漏检测 无人机 油气",
        ],
    },
    {
        "name": "Лабораторные, испытательные и R&D-сервисы",
        "description": "Испытания, квалификация технологий, подбор решений, опытно-промышленные работы и лабораторная автоматизация.",
        "industry_scope": ["laboratory", "testing", "R&D", "qualification"],
        "query_seeds": [
            "oilfield laboratory automation core analysis AI",
            "technology qualification oil gas field trial",
            "robotic laboratory petroleum testing",
            "materials testing CCUS hydrogen wells",
            "油气 实验室 自动化 岩心分析",
            "技术评价 现场试验 油服",
        ],
    },
    {
        "name": "Инжиниринг, проектирование, управление проектами и консалтинг",
        "description": "Проектные, технические, экономические и управленческие сервисы для нефтегазовых проектов.",
        "industry_scope": ["engineering", "EPC", "project management", "consulting"],
        "query_seeds": [
            "AI engineering design oil gas EPC",
            "digital project delivery oil gas engineering",
            "modular oilfield facilities engineering automation",
            "carbon capture project engineering oil gas",
            "油气 工程设计 数字化 人工智能",
            "石化 EPC 项目管理 数字化",
        ],
    },
    {
        "name": "Рынок, экономика, бизнес-модели, контракты и M&A",
        "description": "Рынок нефтесервиса, ставки, загрузка мощностей, сделки, контрактные модели и стратегические сигналы.",
        "industry_scope": ["oilfield services market", "contracts", "M&A", "business models"],
        "query_seeds": [
            "oilfield services contract automation technology deployment",
            "oilfield services M&A technology acquisition",
            "performance based contract oilfield services",
            "strategic partnership drilling automation oil gas",
            "油服 合同 战略合作 技术",
            "油服 并购 自动化 技术",
        ],
    },
]


DEFAULT_RADAR_TOPICS = [*DEFAULT_RADAR_TOPICS, *BUSINESS_RADAR_TOPICS]


@dataclass(frozen=True)
class SignalDiscoveryConfig:
    topic: str | None = None
    days: int = 14
    # Офлайн: без «Дедупа» прогона (модель дедупа не нужна), задача идёт на ядре РФ. Поиск GPT зовётся и тогда, а с
    # РФ-адреса OpenAI отвечает 403: проверка радара только через очередь с --no-offline.
    offline: bool = True
    dry_run: bool = True
    background_job_id: int | None = None
    # Записать прогон в signal_generation_runs: его читают правило «раз в N суток» и здоровье поиска. Имя прежнее: до
    # 08.11 к прогону писались ещё и обучающие примеры судьи.
    persist_training_examples: bool = True
    # Поиск GPT (#138): модель, уровень рассуждения, потолок материалов на тему и период — решение ядра при постановке
    # (research_settings), едут в задаче: своих строк поиска NL не читает. Модель без умолчания и без отката.
    # period_end — ГГГГ-ММ-ДД, пусто — сегодня; research_days — дней до него (пробный прогон берёт месяц целиком).
    research_model: str = ""
    research_reasoning: str = ""
    research_limit: int = 0
    research_days: int = 0
    period_end: str = ""
    # Пробный прогон широкого поиска (#138, signal_search_trial): находки ядро пишет отдельно от радара — метка пути
    # пробного прогона и свой ключ, существующие сигналы прогон не меняет. Воркеру признак не нужен: ищет он так же.
    trial: bool = False


def seed_default_radar_topics() -> int:
    return repository.seed_signal_radar_topics(DEFAULT_RADAR_TOPICS)


# Разведка разрезана на три слоя, как остальные ИИ-стадии (см. external_ai):
#   1. build_discovery_snapshot. Ядро читает базу: темы, разобранные ссылки, карточки радара и сигналы ленты для
#      «Дедупа», ссылки и сайты сбора, (для воркера) теги и память ОС;
#   2. run_discovery. Ни одного обращения к базе: поиск GPT, докачка страниц, технические проверки, «Дедуп»;
#   3. apply_discovery. Ядро пишет находки, технический отсев поиска и склейки «Дедупа».
# Раньше всё шло одной функцией с базой внутри, и маршрут «через внешний воркер»
# (ca23016) упирался в воркер без базы и без этого вида задач: с 13.09 радар не
# отработал по расписанию ни разу — 134 запуска, все 403 с РФ-адреса.

# Теги для прогона без базы. None — читать из repository, как на ядре.
_TAGS_SNAPSHOT: ContextVar[list[dict[str, Any]] | None] = ContextVar("signal_discovery_tags", default=None)
# Расход ИИ прогона по стадиям (поиск и «Дедуп»): копится на NL, едет ядру в итоге (run.ai_usage), ядро пишет в
# ai_processing_runs, и экран «Статистика» видит радар отдельно от ленты.
_USAGE: ContextVar[dict[tuple[str, str], dict[str, Any]] | None] = ContextVar("signal_discovery_usage", default=None)


def _record_usage(stage: str, model: str | None, *, input_tokens: int = 0, output_tokens: int = 0,
                  calls: int = 1, web_search_calls: int = 0) -> None:
    usage = _USAGE.get()
    if usage is None or not calls:
        return
    row = usage.setdefault((stage, str(model or "")), {
        "stage": stage, "model": str(model or ""), "calls": 0, "input_tokens": 0, "output_tokens": 0,
        "web_search_calls": 0,
    })
    row["calls"] += int(calls)
    row["input_tokens"] += int(input_tokens or 0)
    row["output_tokens"] += int(output_tokens or 0)
    row["web_search_calls"] += int(web_search_calls or 0)


_TOPIC_SNAPSHOT_FIELDS = ("name", "description", "query_seeds_json", "query_seeds", "industry_scope_json", "tag_id")
_TAG_SNAPSHOT_FIELDS = (
    "id", "parent_id", "name", "name_en", "parent_name", "parent_name_en", "description",
    "keywords_json", "keywords_en_json",
)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


# Сутки радара по Москве: дата события из будущего отбрасывается по московскому дню.
RADAR_TZ = ZoneInfo("Europe/Moscow")


def _topic_name(topic: dict[str, Any], config: SignalDiscoveryConfig) -> str:
    return str(topic.get("name") or config.topic or "").strip()


# Ссылки статей сбора в снимке широкого поиска (#138) — собранные с начала периода поиска и ещё за столько дней до
# него: материал периода парсер мог взять раньше его даты. Ссылку статьи старше этого ловит запись на ядре
# (_store_otbor_finding сверяет все статьи сбора).
COLLECTED_MARGIN_DAYS = 7


def build_discovery_snapshot(config: SignalDiscoveryConfig, *, for_external: bool = False) -> dict[str, Any]:
    """Всё, что разведке нужно из базы, одним JSON-снимком.

    for_external=True добавляет теги и память ОС: у внешнего воркера базы нет, и без
    снимка эти обогащения молча отваливались бы в except — тот же класс, что тематики
    гейта 17.09 (фича, которая на проде пустая, а в тестах зелёная)."""
    topics = _selected_topics(config.topic)
    if not topics:
        topics = [{"name": config.topic or "HSE technology radar", "query_seeds_json": []}]
    try:
        known_urls = repository.list_reviewed_signal_urls()
    except Exception:  # noqa: BLE001 - фильтр разобранного — улучшение, а не условие прогона
        known_urls = []
    try:
        existing_signals = repository.list_signals_for_dedup()
    except Exception:  # noqa: BLE001 - без сверки с базой дедуп ограничится карточками прогона
        existing_signals = []
    start, end = _research_window(config)
    # Видимые сигналы ленты окна поиска (блок А5, 04.11): находка того же события садится в них, а не заводит вторую
    # карточку. Окно — как у ссылок сбора: материал периода сбор мог взять раньше его даты.
    feed_since = start - timedelta(days=COLLECTED_MARGIN_DAYS)
    try:
        feed_signals = repository.list_feed_signals_for_dedup(since=feed_since)
    except Exception:  # noqa: BLE001 - без сверки с лентой дедуп сверит находки только с радаром
        # Не молча: в итоге прогона это видно и по feed_signals = 0 у «Дедупа».
        logger.exception("radar_feed_snapshot_failed since=%s", feed_since)
        feed_signals = []
    snapshot: dict[str, Any] = {
        "topics": [{key: topic.get(key) for key in _TOPIC_SNAPSHOT_FIELDS if key in topic} for topic in topics],
        "known_urls": sorted({_normalize_url_for_key(url) for url in known_urls if url}),
        "existing_signals": existing_signals,
        "feed_signals": feed_signals,
        "dedup_max_pairs": app_config.SIGNAL_DEDUP_MAX_PAIRS,
        # Широкий поиск (#138): ссылка, которая уже есть в статьях сбора, новой находкой не становится — воркер
        # отбрасывает её до докачки страницы; сайты, которые парсер собирает целиком, поиск получает как «уже есть».
        "collected_url_keys": repository.collected_url_keys_since(since=feed_since),
        "covered_sites": repository.covered_collection_sites(
            since=start, until=end + timedelta(days=1), min_articles=app_config.SIGNAL_RESEARCH_COVERED_MIN_ARTICLES),
    }
    if for_external:
        try:
            tags = repository.list_enabled_tags()
        except Exception:  # noqa: BLE001 - без тегов разведка беднее, но работает
            tags = []
        snapshot["tags"] = [{key: tag.get(key) for key in _TAG_SNAPSHOT_FIELDS} for tag in tags]
        # Память ОС на воркере нужна глоссарию: русский заголовок и суть докачанной страницы (_enforce_glossary).
        snapshot["memory"] = memory_snapshot_rows()
    return _jsonable(snapshot)


@contextmanager
def use_discovery_snapshot(snapshot: dict[str, Any]) -> Iterator[None]:
    """Включить теги и память ОС из снимка, если они в нём есть."""
    tags_token = _TAGS_SNAPSHOT.set(snapshot["tags"]) if snapshot.get("tags") is not None else None
    try:
        if snapshot.get("memory") is not None:
            with use_memory_snapshot(snapshot["memory"]):
                yield
        else:
            yield
    finally:
        if tags_token is not None:
            _TAGS_SNAPSHOT.reset(tags_token)


def run_discovery(
    config: SignalDiscoveryConfig,
    snapshot: dict[str, Any],
    *,
    heartbeat: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Средний слой: без базы. Возвращает находки поиска по каждой теме."""
    if not config.offline:
        signal_dedup.configured_model()  # без модели дедупа — сбой до оплаченного поиска, а не после него
    beat = heartbeat or (lambda: None)
    usage_token = _USAGE.set({})
    try:
        return _run_discovery_topics(config, snapshot, beat)
    finally:
        _USAGE.reset(usage_token)


def _run_discovery_topics(
    config: SignalDiscoveryConfig,
    snapshot: dict[str, Any],
    beat: Callable[[], None],
) -> dict[str, Any]:
    known_urls = set(snapshot.get("known_urls") or [])
    topics_out: list[dict[str, Any]] = []
    with use_discovery_snapshot(snapshot):
        research_start, _ = _research_window(config)
        collected = set(snapshot.get("collected_url_keys") or [])
        covered = [str(site) for site in snapshot.get("covered_sites") or []]
        tag_topic_names = [
            _topic_name(topic, config)
            for topic in snapshot.get("topics") or []
            if topic.get("tag_id") is not None and _topic_name(topic, config)
        ]
        for topic in snapshot.get("topics") or []:
            beat()
            topic_name = _topic_name(topic, config)
            known_titles = _known_titles(snapshot, topic_name, since=research_start)
            web_search = _topic_web_evidence(topic, topic_name, config, heartbeat=beat, known_titles=known_titles,
                                             collected=collected, covered=covered)
            found = web_search.pop("evidence")
            # Дальше блок нужен только счётчиками: сами тексты уже в находках. Без этого результат воркера вёз каждую
            # докачанную страницу лишний раз.
            web_search["evidence_count"] = len(found)
            evidence = _dedupe_evidence(found)
            fresh = [item for item in evidence if _normalize_url_for_key(str(item.get("source_url") or "")) not in known_urls]
            clusters = _cluster_evidence(fresh)
            research = web_search.get("research") or {}
            # Технический отсев поиска до находки не доходит. Ядро сохранит его карточками «Отсеяно»: эксперт
            # просматривает всё, а не только найденное.
            filtered_findings = [{"stage": "search", **item}
                                 for item in [*(research.get("dropped") or []), *(research.get("unverified") or [])]]
            topics_out.append({
                "topic": topic_name,
                "total_evidence": len(evidence),
                "skipped_reviewed": len(evidence) - len(fresh),
                "web_search": web_search,
                "clusters": len(clusters),
                # Поиск только находит (#131): потолка числа находок нет, каждая ждёт решения «Отбора».
                "candidates": [_finding_candidate(cluster, topic, topic_name, tag_topic_names) for cluster in clusters],
                "filtered_findings": filtered_findings,
            })
    dedup = _dedupe_run(config, snapshot, topics_out, beat)
    return {"topics": topics_out, "dedup": dedup, "ai_usage": list((_USAGE.get() or {}).values())}


def _finding_candidate(cluster: list[dict[str, Any]], topic: dict[str, Any], topic_name: str,
                       tag_topic_names: list[str]) -> dict[str, Any]:
    """Находка поиска (#131): решения у неё нет, его примет «Отбор». Сигнал несёт только то, что о публикации известно
    без модели: заголовок страницы и тему по ключам тематик (тему ставит «Карточка», #132). Черновые данные поиска
    (тип события, компания, суть, «почему важно») остаются в ссылке и в сигнал не идут."""
    best = max(cluster, key=lambda item: float(item.get("strength") or 0))
    signal: dict[str, Any] = {"title": _trim(str(best.get("title") or ""), 220), "theme": topic_name, "summary": "",
                              "companies": [], "maturity": "watch", "score": 0}
    if topic.get("tag_id") is not None:
        signal["theme"], _ = _content_theme(signal, cluster, topic_name, tag_topic_names)
    signal["signal_key"] = _signal_key(cluster)
    signal["evidence_count"] = len({str(item.get("source_url") or "") for item in cluster if item.get("source_url")})
    signal["evidence"] = cluster
    return {"signal": signal}


def _dedupe_run(
    config: SignalDiscoveryConfig,
    snapshot: dict[str, Any],
    topics_out: list[dict[str, Any]],
    beat: Callable[[], None],
) -> dict[str, Any]:
    """Сверить находки прогона между собой, с сохранёнными карточками радара и с сигналами ленты.

    Решение кладётся в находку (duplicate_of) и в existing_merges, пишет их ядро."""
    if config.offline:
        return {"skipped": "offline"}
    nodes: list[dict[str, Any]] = []
    # Сохранённые карточки радара и видимые сигналы ленты окна поиска (feed_signals, 04.11): сигнал ленты сверяется
    # только с находками прогона (signal_dedup._eligible), находка того же события уходит ссылкой в сигнал ленты.
    for row in [*(snapshot.get("existing_signals") or []), *(snapshot.get("feed_signals") or [])]:
        nodes.append({
            "kind": "existing",
            "origin": row.get("origin"),
            "id": int(row["id"]),
            "reviewed": bool(row.get("reviewed") or row.get("verdict")),
            "verdict": row.get("verdict"),
            "fresh": bool(row.get("fresh")),
            # Не на экране (качество, архив); снимок старого ядра поля не знает — видна.
            "hidden": bool(row.get("hidden")),
            "urls": row.get("evidence_urls") or [],
            "signal": row,
        })
    refs: dict[int, dict[str, Any]] = {}
    for topic in topics_out:
        for candidate in topic["candidates"]:
            signal = candidate["signal"]
            refs[len(nodes)] = candidate
            nodes.append({
                "kind": "new",
                "reviewed": False,
                "fresh": True,
                "order": len(refs),
                "urls": [item.get("source_url") for item in signal.get("evidence") or [] if item.get("source_url")],
                "signal": _finding_dedup_view(signal),
            })
    result = signal_dedup.dedupe(
        nodes,
        client_factory=_radar_client,
        heartbeat=beat,
        max_pairs=int(snapshot.get("dedup_max_pairs") or signal_dedup.MAX_JUDGED_PAIRS),
    )
    stats = result["stats"]
    _record_usage("radar_dedup", app_config.SIGNAL_DEDUP_MODEL, calls=int(stats.get("judged") or 0),
                  input_tokens=int(stats.get("input_tokens") or 0), output_tokens=int(stats.get("output_tokens") or 0))
    existing_merges = []
    for index, (primary_index, reason) in result["assigned"].items():
        primary = nodes[primary_index]
        if index in refs:
            refs[index]["duplicate_of"] = (
                {"signal_id": primary["id"]} if primary["kind"] == "existing"
                else {"signal_key": primary["signal"]["signal_key"]}
            )
            refs[index]["duplicate_reason"] = reason
        elif primary["kind"] == "existing":
            existing_merges.append({"signal_id": nodes[index]["id"], "into": primary["id"], "reason": reason})
    feed = {index for index, node in enumerate(nodes) if node.get("origin") == signal_dedup.FEED_ORIGIN}
    return {
        **result["stats"],
        "duplicates_new": sum(1 for index in result["assigned"] if index in refs),
        # Сверка с лентой (04.11): сколько сигналов ленты было в снимке и сколько находок ушло в них ссылкой.
        "feed_signals": len(feed),
        "duplicates_of_feed": sum(1 for index, (primary_index, _) in result["assigned"].items()
                                  if index in refs and primary_index in feed),
        "existing_merges": existing_merges,
    }


def _finding_dedup_view(signal: dict[str, Any]) -> dict[str, Any]:
    """Что «Дедуп» знает о находке поиска: сигнал несёт только заголовок страницы (#131), а черновые данные модели
    поиска лежат в её ссылках: русский заголовок, компании и суть. Правилу пар и судье они нужны: без них пара
    Halliburton и Keystone прогона 51351 с английскими заголовками сошлась на 0,18 при пороге 0,25, а пара с русской
    статьёй ленты не сошлась бы вовсе (замер 04.11). В сигнал и во вход «Отбора» они не идут."""
    payloads = [item.get("raw_payload") or {} for item in signal.get("evidence") or []]
    titles = [str(payload["research_title"]) for payload in payloads if payload.get("research_title")]
    return {
        **signal,
        "title_ru": titles[0] if titles else None,
        "research_titles": titles,
        "companies": [str(payload["company"]) for payload in payloads if payload.get("company")],
        "summary": next((str(payload["research_summary"]) for payload in payloads
                         if payload.get("research_summary")), ""),
    }


def apply_discovery(
    config: SignalDiscoveryConfig,
    run: dict[str, Any],
    *,
    generation_run_id: int | None = None,
) -> dict[str, Any]:
    """Ядро: записать находки из результата run_discovery и собрать итог."""
    if not config.dry_run and run.get("ai_usage"):
        repository.record_radar_ai_usage(config.background_job_id, run["ai_usage"])
    all_signals: list[dict[str, Any]] = []
    topic_results = []
    duplicates: list[tuple[str, dict[str, Any]]] = []
    stored_keys: dict[str, int] = {}
    found_again_keys: dict[str, int | None] = {}
    owners = _key_owners(config, run)
    for topic in run.get("topics") or []:
        topic_name = str(topic.get("topic") or "")
        filtered_stored = 0
        # Технический отсев пробного прогона (#138) в разделы отклонённого не пишется: в итоге задачи — счётчики.
        if not config.dry_run and not config.trial and topic.get("filtered_findings"):
            filtered_stored = _store_filtered_findings(topic_name, topic["filtered_findings"])
        found = []
        topic_duplicates = 0
        # Ссылки из статей сбора (#138): отброшенные воркером по снимку и записью здесь.
        already_collected = list(((topic.get("web_search") or {}).get("research") or {}).get("already_collected")
                                 or [])
        for candidate in topic.get("candidates") or []:
            signal = candidate["signal"]
            _reconcile_with_owner(candidate, owners.get(str(signal.get("signal_key") or "")))
            if candidate.get("duplicate_of"):
                # Главная карточка группы может стоять в другой теме дальше по списку —
                # дубли пишем, когда все главные уже сохранены.
                duplicates.append((topic_name, candidate))
                topic_duplicates += 1
                continue
            signal_id = _store_otbor_finding(config, candidate)
            if signal_id is not None:
                stored_keys[signal["signal_key"]] = signal_id
            if signal.get("found_again"):
                found_again_keys[signal["signal_key"]] = signal.get("found_again_id") or signal.get("id")
            already_collected.extend(candidate.get("already_collected") or [])
            if candidate.get("collected_whole"):
                continue
            found.append(signal)
            all_signals.append(signal)
        topic_results.append({
            "topic": topic_name,
            "total_evidence": topic.get("total_evidence", 0),
            "skipped_reviewed": topic.get("skipped_reviewed", 0),
            "web_search": topic.get("web_search"),
            "web_error": _search_error_summary(topic.get("web_search")),
            "clusters": topic.get("clusters", 0),
            "signals": found,
            "duplicates": topic_duplicates,
            "filtered_stored": filtered_stored,
            "already_collected": already_collected,
        })
    dedup = dict(run.get("dedup") or {})
    merged = 0
    results_by_topic = {row["topic"]: row for row in topic_results}
    for topic_name, candidate in duplicates:
        merged_now = _merge_duplicate(config, candidate, stored_keys, found_again_keys)
        if candidate.get("already_collected") and topic_name in results_by_topic:
            results_by_topic[topic_name]["already_collected"].extend(candidate["already_collected"])
        if candidate["signal"].get("found_again_id") and topic_name in results_by_topic:
            results_by_topic[topic_name].setdefault("found_again_duplicates", []).append(
                candidate["signal"].get("found_again_id"))
        if merged_now:
            merged += 1
        elif not config.dry_run:
            # Главной карточки нет (удалена, не записана, скрылась, пока шёл прогон): находку не теряем, она идёт
            # своей карточкой в «Отбор» и в итог своей темы (пробный прогон ставит «Отбор» по этому списку).
            signal_id = _store_otbor_finding(config, candidate)
            candidate["signal"]["id"] = signal_id
            all_signals.append(candidate["signal"])
            if signal_id is not None and topic_name in results_by_topic:
                results_by_topic[topic_name]["signals"].append(candidate["signal"])
    merged_existing = 0
    # Сигналы платформы пробный прогон (#138) не склеивает: он их не меняет.
    if not config.dry_run and not config.trial:
        for merge in dedup.pop("existing_merges", None) or []:
            if repository.mark_signal_merged(int(merge["signal_id"]), int(merge["into"]), str(merge.get("reason") or "")):
                merged_existing += 1
    else:
        dedup.pop("existing_merges", None)
    if dedup or merged or merged_existing:
        dedup.update({"merged_new": merged, "merged_existing": merged_existing})
    return {
        "dry_run": config.dry_run,
        "offline": config.offline,
        "days": config.days,
        "topics": [str(topic.get("topic") or "") for topic in run.get("topics") or []],
        "all_signals": len(all_signals),
        "topic_results": topic_results,
        "generation_run_id": generation_run_id,
        "dedup": dedup or None,
    }


def _key_owners(config: SignalDiscoveryConfig, run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if config.dry_run:
        return {}
    keys = sorted({
        str(candidate["signal"].get("signal_key") or "")
        for topic in run.get("topics") or []
        for candidate in topic.get("candidates") or []
        if candidate["signal"].get("signal_key")
    })
    return repository.signal_key_owners(keys) if keys else {}


def _reconcile_with_owner(candidate: dict[str, Any], owner: dict[str, Any] | None) -> None:
    """Находка с ключом уже сохранённой карточки: это она же, найденная снова.

    Скрытая дублем: повтор идёт в её главную, иначе запись ушла бы в скрытую строку, и
    сигнал пропал бы с экрана. Разобранная человеком остаётся собой: судья «Дедупа» не
    переспорит разбор, и её ссылка не уедет в чужую карточку. Видимая неразобранная, которую
    судья счёл дублем другой, скрывается вместе с ним."""
    if not owner:
        return
    if owner.get("merged_into_signal_id") is not None:
        candidate["duplicate_of"] = {"signal_id": int(owner["merged_into_signal_id"])}
        candidate["duplicate_reason"] = "та же ссылка уже склеена с этой карточкой"
    elif owner.get("reviewed"):
        candidate.pop("duplicate_of", None)
    elif candidate.get("duplicate_of"):
        candidate["key_owner_id"] = int(owner["id"])


def _store_otbor_finding(config: SignalDiscoveryConfig, candidate: dict[str, Any]) -> int | None:
    """Находка поиска (#131) становится сигналом радара с меткой пути «Отбора»: пока решения нет, его не видно нигде,
    и решит его «Отбор» (шаг планировщика). Найденная снова (тот же ключ) только отмечается: решённое «Отбором» или
    прежними правилами второй раз не решается и не оплачивается. Все ссылки уже у видимых карточек: отмечается
    карточка, которая их держит."""
    if config.dry_run:
        return None
    if config.trial:
        return _store_trial_finding(candidate, run=config.background_job_id)
    signal = candidate["signal"]
    cluster = _drop_collected_links(candidate)
    if not cluster:
        return None
    repeat_of = _repeat_of_existing_card(signal, cluster)
    if repeat_of is not None:
        repository.touch_signal(repeat_of)
        signal["id"] = repeat_of
        signal["found_again"] = True
        return repeat_of
    signal_id, created = repository.insert_otbor_finding({**signal, "decision_log": _otbor_finding_log(cluster)})
    if created:
        for item in cluster:
            repository.upsert_signal_evidence(signal_id, item)
        signal["evidence_count"] = repository.refresh_signal_evidence_count(signal_id)
    else:
        repository.touch_signal(signal_id)
        signal["found_again"] = True
    signal["id"] = signal_id
    return signal_id


# Ключ находки пробного прогона (#138) — свой: повтор того же события обычным прогоном радара заводит обычную находку,
# а не уходит в невидимую находку пробного прогона.
TRIAL_KEY_PREFIX = "trial:"


def _store_trial_finding(candidate: dict[str, Any], *, run: int | None) -> int | None:
    """Находка пробного прогона широкого поиска (#138): сигнал с меткой пути пробного прогона и своим ключом — его не
    видят витрины, разделы отклонённого, шаги конвейера и следующий прогон радара (снимок дедупа его не берёт).
    Существующие сигналы прогон не меняет: ссылка, которая уже есть у сигнала радара, ленты или материала, к находке
    не переходит; все ссылки уже есть — находка найдена снова (found_again_id — у кого ссылка), без отметки. Ссылку
    находки прошлого пробного прогона новая забирает себе, а ключ у находки свой у каждого прогона (run — задача
    прогона): каждый пробный прогон меряется целиком, и его решения «Отбора» не смешиваются с прошлыми."""
    signal = candidate["signal"]
    own, held = _trial_own_links(_drop_collected_links(candidate))
    if not own:
        if held:
            signal["found_again"] = True
            signal["found_again_id"] = int(next(iter(held.values()))["id"])
        return None
    signal_id, _ = repository.insert_otbor_finding(
        {**signal, "signal_key": f"{TRIAL_KEY_PREFIX}{run}:{signal['signal_key']}", "decision_log": _otbor_finding_log(own)},
        selector=repository.SIGNAL_SELECTOR_TRIAL)
    for item in own:
        repository.upsert_signal_evidence(signal_id, item)
    signal["evidence_count"] = repository.refresh_signal_evidence_count(signal_id)
    signal["id"] = signal_id
    return signal_id


def _trial_own_links(cluster: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict]]:
    """Ссылки находки пробного прогона (#138), которых нет у сигналов платформы, и те, что есть: {url: владелец}."""
    owners = search_trial_store.evidence_owners([str(item.get("source_url") or "") for item in cluster])
    held = {url: owner for url, owner in owners.items() if owner["selector"] != repository.SIGNAL_SELECTOR_TRIAL}
    return [item for item in cluster if item.get("source_url") and item["source_url"] not in held], held


def _merge_trial_duplicate(candidate: dict[str, Any], stored_keys: dict[str, int],
                           found_again_keys: dict[str, int | None]) -> bool:
    """Дубль в пробном прогоне (#138): его ссылки — в главную находку этого прогона; дубль сигнала платформы или
    главной, которая сама оказалась сигналом платформы, — найден снова (found_again_id), сигнал прогон не трогает.
    Главная не записана по другой причине (её ссылки собрал сбор, пока шёл прогон): False, и дубль со своей ссылкой
    пишется сам и идёт в «Отбор»."""
    signal = candidate["signal"]
    target = candidate.get("duplicate_of") or {}
    key = str(target.get("signal_key") or "")
    if target.get("signal_id") or key in found_again_keys:
        signal["found_again"] = True
        signal["found_again_id"] = int(target["signal_id"]) if target.get("signal_id") else found_again_keys[key]
        return True
    target_id = stored_keys.get(key)
    if target_id is None:
        return False
    own, _ = _trial_own_links(_drop_collected_links(candidate))
    for item in own:
        repository.upsert_signal_evidence(target_id, item)
    repository.refresh_signal_evidence_count(target_id)
    return True


def _drop_collected_links(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """Ссылки находки без тех, что уже есть в статьях сбора (#138): воркер отбросил известные ему по снимку, здесь —
    собранные, пока шёл прогон, и старше окна снимка. Отброшенные — в candidate["already_collected"] для итога
    задачи; все ссылки собраны — находки нет (candidate["collected_whole"])."""
    signal = candidate["signal"]
    cluster = list(signal.get("evidence") or [])
    keys = {article_url_key(str(item.get("source_url") or "")): item for item in cluster if item.get("source_url")}
    collected = repository.collected_url_keys_among(list(keys)) if keys else set()
    if not collected:
        return cluster
    candidate["already_collected"] = [{"title": _trim(str(item.get("title") or ""), 300), "url": item["source_url"]}
                                      for key, item in keys.items() if key in collected]
    cluster = [item for item in cluster
               if not item.get("source_url") or article_url_key(str(item["source_url"])) not in collected]
    signal["evidence"] = cluster
    candidate["collected_whole"] = not cluster
    return cluster


def _otbor_finding_log(cluster: list[dict[str, Any]]) -> list[dict[str, str]]:
    """«Ход решения» находки: что нашёл поиск (заголовок и сайт ссылки). Решения поиска нет."""
    return [{"stage": "поиск", "text": f"Найдено: {item.get('title') or item.get('source_url') or ''}"
             + (f" ({_domain_of(str(item['source_url']))})" if item.get("source_url") else "")} for item in cluster]


def _domain_of(url: str) -> str:
    return _normalize_url_for_key(url).split("/", 1)[0]


def _links_held_elsewhere(urls: list[str], signal_key: str) -> dict[str, dict]:
    """Какие из ссылок держит другая карточка — на радаре или разобранная человеком.

    Отсеянное такую ссылку не пишет: signal_evidence уникальна по адресу, и запись
    перезаписала бы заголовок и суть чужой ссылки, хотя сама ссылка осталась бы у хозяйки."""
    owners = repository.visible_evidence_owners([url for url in urls if url])
    return {url: owner for url, owner in owners.items() if owner.get("signal_key") != signal_key}


def _store_filtered_findings(topic_name: str, findings: list[dict[str, Any]]) -> int:
    """Технический отсев поиска пишется карточками «Отсеяно» (разделы «Поиск → …»).

    Ключ — по ссылке (или заголовку): повторно отсеянное обновляет ту же карточку, а
    найденное снова и прошедшее проверки получит свой ключ находки и пойдёт в «Отбор». Ссылку,
    которую забраковали судья или ревью пачки прежней цепочки, повторный отсев не трогает:
    этапы идут только вперёд."""
    stored = 0
    for item in findings:
        title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
        url = str(item.get("url") or "").strip()
        if not title and not url:
            continue
        key = "filtered:" + hashlib.sha1((_normalize_url_for_key(url) or title.lower()).encode("utf-8")).hexdigest()[:24]
        owner = repository.signal_key_owners([key]).get(key)
        if owner and (owner.get("filter_stage") is None or owner.get("merged_into_signal_id")):
            # Склеенная отсеянная: событие уже найдено снова, повтор отсева её не оживляет и не
            # уводит ссылку от забравшей.
            continue
        if url and _links_held_elsewhere([url], key):
            # Ссылка уже у карточки на радаре или разобранной человеком: это повтор события
            # (вчерашнее сегодня вышло за период поиска), а не новое отсеянное. Запись
            # затёрла бы её ссылке русский заголовок и суть (ON CONFLICT source_url).
            continue
        if url and any(owner["signal_key"] != key for owner in repository.rejected_evidence_owners([url]).values()):
            # Ссылка у забракованной судьёй или ревью прежней цепочки (история): событие разобрано
            # дальше, а повторный отсев не новое отсеянное. Запись забрала бы у неё ссылку и
            # оставила в «Отсеянных» пустую копию с лучшей причиной (17.10).
            continue
        signal_id = repository.upsert_signal({
            "signal_key": key,
            "title": _trim(title or url, 220),
            "title_ru": _trim(title or url, 220),
            "theme": topic_name,
            "summary": _trim(str(item.get("summary") or ""), 600),
            "maturity": "reject",
            "score": 0,
            "event_date": _normalize_event_date(item.get("event_date")),
            "filter_stage": "search",
            "filter_reason": _trim(str(item.get("reason") or ""), 500),
            "decision_log": _finding_log(item),
        })
        if url.startswith("http"):
            repository.upsert_signal_evidence(signal_id, {
                "source_url": url, "title": title or url, "publisher": item.get("publisher"),
                "published_at": _normalize_event_date(item.get("event_date")),
                "evidence_type": "news", "extracted_fact": item.get("summary") or "",
                "raw_payload": {"evidence_source": "filtered", "stage": "search"},
            })
            repository.refresh_signal_evidence_count(signal_id)
        stored += 1
    return stored


def _finding_log(item: dict[str, Any]) -> list[dict[str, str]]:
    """Ход решения для находки, отсеянной поиском: что нашёл поиск и почему отсеяно."""
    found = ", ".join(part for part in (str(item.get("publisher") or ""), str(item.get("event_date") or "")) if part)
    log = [{"stage": "поиск", "text": f"Найдено: {item.get('title') or item.get('url') or ''}" + (f" ({found})" if found else "")}]
    if item.get("technology"):
        log.append({"stage": "поиск", "text": f"Технология по версии поиска: {item['technology']}"})
    log.append({"stage": "отсев", "text": f"Отсеяно (поиск): {item.get('reason') or 'причина не указана'}"})
    return log


def _repeat_of_existing_card(signal: dict[str, Any], cluster: list[dict[str, Any]]) -> int | None:
    """Карточка, которой уже принадлежат ВСЕ ссылки находки, или None.

    Своя карточка (тот же ключ) не в счёт: это обычное обновление. Если ссылки у
    нескольких карточек — берём ту, у которой их больше."""
    urls = [str(item.get("source_url") or "") for item in cluster if item.get("source_url")]
    if not urls:
        return None
    owners = repository.visible_evidence_owners(urls)
    foreign = [owners[url]["id"] for url in urls if url in owners and owners[url]["signal_key"] != signal.get("signal_key")]
    if len(foreign) < len(urls):
        return None
    return max(set(foreign), key=foreign.count)


MERGE_LOG_STAGE = "«Дедуп»"


def _merge_log_text(added: list[dict[str, Any]], reason: Any) -> str:
    """Строка «Хода решения» главной: какую публикацию поиска «Дедуп» к ней прибавил и почему."""
    found = "; ".join(f"{item.get('title') or item['source_url']} ({_domain_of(str(item['source_url']))})"
                      for item in added)
    text = f"Та же новость найдена поиском радара: {found}"
    return _trim(f"{text}. Судья: {reason}" if reason else text, 600)


def _merge_duplicate(
    config: SignalDiscoveryConfig,
    candidate: dict[str, Any],
    stored_keys: dict[str, int],
    found_again_keys: dict[str, int | None] | None = None,
) -> bool:
    """Ссылки дубля — в главную карточку, новой карточки нет. False — главной не нашлось."""
    signal = candidate["signal"]
    target = candidate.get("duplicate_of") or {}
    if config.dry_run:
        return True
    if config.trial:
        return _merge_trial_duplicate(candidate, stored_keys, found_again_keys or {})
    target_id = target.get("signal_id") or stored_keys.get(str(target.get("signal_key") or ""))
    target_id = repository.resolve_signal_merge_root(int(target_id)) if target_id else None
    if target_id is None:
        return False
    if target.get("signal_id") and not repository.signal_takes_findings(target_id):
        # Сохранённая главная скрылась, пока шёл прогон (04.11): находка — своей карточкой, её решит «Отбор».
        return False
    owner_id = candidate.get("key_owner_id")
    if owner_id and owner_id != target_id:
        # Ключ дубля — у видимой карточки: это тот же материал, она тоже дубль главной.
        repository.mark_signal_merged(int(owner_id), target_id, str(candidate.get("duplicate_reason") or ""))
    # Ссылка, которую сбор успел собрать, пока шёл прогон (#138), к главной находке не прибавляется.
    _drop_collected_links(candidate)
    held = repository.signal_evidence_urls(target_id)
    for item in signal.get("evidence") or []:
        repository.upsert_signal_evidence(target_id, item)
    # Главной бывает и сигнал ленты (04.11): в её «Ходе решения» видно, откуда у неё ссылка поиска. Только когда ссылка
    # прибавилась: та же находка следующего прогона строку не повторяет.
    new_urls = repository.signal_evidence_urls(target_id) - held
    added = [item for item in signal.get("evidence") or [] if item.get("source_url") in new_urls]
    if added:
        repository.append_signal_decision_log(target_id, MERGE_LOG_STAGE,
                                              _merge_log_text(added, candidate.get("duplicate_reason")))
    repository.refresh_signal_evidence_count(target_id)
    if owner_id and owner_id != target_id:
        repository.refresh_signal_evidence_count(int(owner_id))
    repository.touch_signal(target_id)
    return True


def discover_signals(config: SignalDiscoveryConfig) -> dict[str, Any]:
    """Прогон целиком на ядре (CLI, локальная очередь, тесты). Настройки поиска GPT, которых нет в конфиге разовой
    команды, — настройки ядра (#138): прогон идёт там же, где они заданы."""
    config = replace(
        config,
        research_model=config.research_model or app_config.SIGNAL_RESEARCH_MODEL,
        research_reasoning=config.research_reasoning or app_config.SIGNAL_RESEARCH_REASONING,
        research_limit=config.research_limit or app_config.SIGNAL_RESEARCH_EVENTS_PER_TOPIC,
        research_days=_research_days(config),
    )
    generation_run_id = None
    if config.persist_training_examples and not config.dry_run:
        generation_run_id = repository.create_signal_generation_run(
            config_payload=asdict(config),
            trigger="signal_discovery",
            background_job_id=config.background_job_id,
        )
    try:
        snapshot = build_discovery_snapshot(config)
        run = run_discovery(config, snapshot)
        result = apply_discovery(config, run, generation_run_id=generation_run_id)
    except Exception as exc:
        if generation_run_id is not None:
            repository.finish_signal_generation_run(
                generation_run_id,
                status="failed",
                result={},
                error_message=str(exc)[:1000],
            )
        raise
    if generation_run_id is not None:
        _finish_generation_run(generation_run_id, result)
    return result


def _payload_number(payload: dict[str, Any], key: str, default: Any, cast: Callable[[Any], Any]) -> Any:
    """Нет значения — умолчание; явный 0 — это 0.

    `int(payload.get(key) or 20)` превращал 0 в 20, и явный ноль в задаче не значил ничего (21.09)."""
    value = payload.get(key)
    if value is None or value == "":
        return default
    return cast(value)


def config_from_payload(payload: dict[str, Any], *, background_job_id: int | None = None) -> SignalDiscoveryConfig:
    """Параметры задачи из payload: те же умолчания, что у локального обработчика. Ключи прежней цепочки (limit,
    web_only, search_mode, otbor и т. п.) в задаче, поставленной до 08.11, просто не читаются."""
    return SignalDiscoveryConfig(
        topic=str(payload["topic"]) if payload.get("topic") else None,
        days=_payload_number(payload, "days", 14, int),
        offline=bool(payload.get("offline", True)),
        dry_run=bool(payload.get("dry_run", False)),
        background_job_id=background_job_id,
        # Задача, поставленная до #138, настроек поиска не несёт: те же решения ядра, но в момент выдачи.
        research_model=str(payload.get("research_model") or app_config.SIGNAL_RESEARCH_MODEL or "").strip(),
        research_reasoning=str(payload.get("research_reasoning") or app_config.SIGNAL_RESEARCH_REASONING or "").strip(),
        research_limit=_payload_number(payload, "research_limit", app_config.SIGNAL_RESEARCH_EVENTS_PER_TOPIC, int),
        research_days=_payload_number(payload, "research_days",
                                      max(_payload_number(payload, "days", 14, int), app_config.SIGNAL_RESEARCH_DAYS),
                                      int),
        period_end=str(payload.get("period_end") or ""),
        trial=bool(payload.get("search_trial")),
    )


class SearchSetupError(ValueError):
    """Прогон радара с поиском GPT нельзя поставить: модель поиска не задана, снимается или нет уровня (#138)."""


def research_settings(*, offline: bool = False, days: int | None = None) -> dict[str, Any]:
    """Настройки поиска GPT для задачи прогона радара (#138): решение ядра в момент постановки, их и исполнит NL.
    Офлайн-прогону ничего не нужно: он идёт на ядре и берёт настройки ядра в момент выдачи (config_from_payload).
    Модель без умолчания и отката: не задана, снимается 11.12.2026 (gpt-5, gpt-5-mini, gpt-5-nano) или нет уровня
    рассуждения: отказ с причиной."""
    if offline:
        return {}
    settings = {
        "research_model": app_config.SIGNAL_RESEARCH_MODEL,
        "research_reasoning": app_config.SIGNAL_RESEARCH_REASONING,
        "research_limit": app_config.SIGNAL_RESEARCH_EVENTS_PER_TOPIC,
        "research_days": max(int(days or 0), app_config.SIGNAL_RESEARCH_DAYS),
    }
    problem = research_problem(settings["research_model"], settings["research_reasoning"])
    if problem:
        raise SearchSetupError(problem)
    return settings


def research_problem(model: str, reasoning: str) -> str | None:
    if not model:
        return "SIGNAL_RESEARCH_MODEL не задан в .env ядра: модели поиска по умолчанию нет"
    if signal_research.retired_model(model):
        return f"модель поиска {model} снимается 11.12.2026: задайте SIGNAL_RESEARCH_MODEL не из семейства gpt-5"
    if not reasoning:
        return "SIGNAL_RESEARCH_REASONING не задан: у новых моделей нет уровня minimal, нужен явный (low и выше)"
    return None


def _research_window(config: SignalDiscoveryConfig) -> tuple[date, date]:
    """Период поиска GPT: research_days до period_end (по умолчанию — сегодня)."""
    end = date.fromisoformat(config.period_end) if config.period_end else None
    return signal_research.research_period(_research_days(config), end)


def build_external_payload(job_payload: dict[str, Any]) -> dict[str, Any]:
    """Ядро, в момент выдачи задачи воркеру: параметры + снимок базы. Поиску GPT без годной модели задача не
    выдаётся (#138): воркер искал бы моделью своего .env или не искал вовсе, а прогон прошёл бы «ok» с нулём."""
    config = config_from_payload(job_payload)
    if not config.offline:
        problem = research_problem(config.research_model, config.research_reasoning)
        if problem:
            raise SearchSetupError(problem)
    config_dict = {key: value for key, value in asdict(config).items() if key != "background_job_id"}
    return {
        "kind": "signal_discovery",
        "config": config_dict,
        "snapshot": build_discovery_snapshot(config, for_external=True),
    }


def process_external_payload(payload: dict[str, Any], heartbeat: Callable[[], None] | None = None) -> dict[str, Any]:
    """Сторона воркера: в базу не ходит, возвращает находки ядру."""
    allowed = {item.name for item in fields(SignalDiscoveryConfig)}
    config = SignalDiscoveryConfig(**{
        key: value for key, value in (payload.get("config") or {}).items() if key in allowed
    })
    run = run_discovery(config, payload.get("snapshot") or {}, heartbeat=heartbeat)
    return {"signal_discovery": True, "config": payload.get("config") or {}, "run": run}


def apply_external_result(result: dict[str, Any], *, job_id: int, trial: bool = False) -> dict[str, Any]:
    """Ядро, при завершении задачи: записать находки и вернуть компактный итог.

    Полный результат (находки с текстами страниц) в result_json задачи не кладём: находки пишутся в signals и
    signal_evidence, в задаче остаются счётчики. Пробный ли это прогон (#138), решает задача ядра (trial), а не конфиг,
    который вернул воркер."""
    allowed = {item.name for item in fields(SignalDiscoveryConfig)}
    config = replace(
        SignalDiscoveryConfig(**{
            key: value for key, value in (result.get("config") or {}).items() if key in allowed
        }),
        background_job_id=job_id,
        trial=trial,
    )
    generation_run_id = None
    if config.persist_training_examples and not config.dry_run:
        generation_run_id = repository.create_signal_generation_run(
            config_payload=asdict(config),
            trigger="signal_discovery_external",
            background_job_id=job_id,
        )
    applied = apply_discovery(config, result.get("run") or {}, generation_run_id=generation_run_id)
    topics = []
    for row in applied["topic_results"]:
        web = row.get("web_search") or {}
        topics.append({
            "topic": row["topic"],
            "web_status": web.get("status"),
            "web_error": row.get("web_error"),
            # Чем искали, что вернул поиск, сколько страниц докачано и чем: по этому проверяют прогон.
            "provider": web.get("provider"),
            "research": _research_summary(web.get("research")),
            "total_evidence": row.get("total_evidence"),
            "skipped_reviewed": row.get("skipped_reviewed"),
            "clusters": row.get("clusters"),
            "signals": len(row.get("signals") or []),
            "duplicates": row.get("duplicates", 0),
            # Широкий поиск (#138): какие находки заведены этим прогоном, сколько найдено снова и какие ссылки уже были
            # в сборе — для отчёта пробного прогона (radar-search-trial); номера и адреса, не тексты.
            "signal_ids": [int(signal["id"]) for signal in row.get("signals") or []
                           if signal.get("id") and not signal.get("found_again")],
            "found_again_ids": [value for signal in row.get("signals") or [] if signal.get("found_again")
                                and (value := signal.get("found_again_id") or signal.get("id"))]
            + list(row.get("found_again_duplicates") or []),
            "already_collected": row.get("already_collected") or [],
        })
    summary = {
        "generation_run_id": generation_run_id,
        "signals": applied["all_signals"],
        "persisted": 0 if config.dry_run else applied["all_signals"],
        "topics": topics,
        "dedup": applied.get("dedup"),
    }
    if generation_run_id is not None:
        _finish_generation_run(generation_run_id, applied)
    return summary


# Поиск ответил: выдача есть или честно пуста. Иначе (error) тема осталась без поиска.
_SEARCH_ANSWERED = frozenset({"ok", "empty"})


def _search_error_summary(web_search: dict[str, Any] | None) -> str | None:
    """Сбой поиска темы коротко. Поиск GPT кладёт его в research.error (403 ключа без web_search, обрыв сети, не
    задана модель), а status у темы просто «error»."""
    if not web_search:
        return None
    research_error = str((web_search.get("research") or {}).get("error") or "").strip()
    if research_error:
        return _trim(research_error, 160)
    status = str(web_search.get("status") or "")
    if not status or status in _SEARCH_ANSWERED:
        return None
    return _trim(str(web_search.get("reason") or status), 160)


def _research_summary(research: dict[str, Any] | None) -> dict[str, Any] | None:
    """Итог поиска GPT по теме коротко, для итога задачи: счётчики, не события."""
    if not research:
        return None
    summary = {
        key: research.get(key)
        for key in ("status", "events", "evidence_count", "feedback_chars", "web_search_calls", "attempts")
        if research.get(key) is not None
    }
    summary["unverified"] = len(research.get("unverified") or [])
    summary["dropped"] = len(research.get("dropped") or [])
    if research.get("fulltext"):
        # Счётчики докачки (добор, 04.11): сколько страниц открыто и чем.
        summary["fulltext"] = research["fulltext"]
    if "already_collected" in research:
        summary["already_collected"] = len(research["already_collected"] or [])
    if research.get("error"):
        summary["error"] = _trim(str(research["error"]), 160)
    return summary


def _search_failed(web_search: dict[str, Any] | None) -> bool:
    status = str((web_search or {}).get("status") or "")
    return bool(status) and status not in _SEARCH_ANSWERED


def _search_health(topic_results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Здоровье поиска прогона: в скольких темах искали, во скольких поиск не ответил и первая ошибка: по ним экран
    пишет админу. Без тем None."""
    searched = [row for row in topic_results if row.get("web_search")]
    if not searched:
        return None
    failed = [row for row in searched if _search_failed(row["web_search"])]
    return {
        "topics": len(searched),
        "failed": len(failed),
        "first_error": failed[0].get("web_error") if failed else None,
    }


def _finish_generation_run(generation_run_id: int, applied: dict[str, Any]) -> None:
    """Записать прогон в signal_generation_runs — одинаково на пути на месте и через воркер.

    Поиск не ответил ни в одной теме: прогон failed с понятной причиной, находок нет ни у одной
    темы. Статус ЗАДАЧИ остаётся ok: его читает правило «ежедневный уже ставили в эти сутки»
    (background_jobs.enqueue_daily_signal_discovery). Статус прогона читают правило «раз в N
    суток» (failed не держит следующий прогон, решение владельца 04.11) и выгрузка для обучения
    (signal_training), а там он не фильтр."""
    result: dict[str, Any] = {
        "topics": len(applied["topic_results"]),
        "signals": applied["all_signals"],
    }
    finish: dict[str, Any] = {"status": "ok", "result": result}
    health = _search_health(applied["topic_results"])
    if health is not None:
        result["search_health"] = health
        if health["failed"] == health["topics"]:
            finish["status"] = "failed"
            finish["error_message"] = (
                f"Поиск не ответил во всех темах прогона ({health['failed']} из {health['topics']}): "
                f"{health['first_error'] or 'причина не записана'}"
            )
    repository.finish_signal_generation_run(generation_run_id, **finish)


def _radar_client():
    """Клиент модели для «Дедупа» радара: свой таймаут, модель с рассуждением думает дольше ленты."""
    client = make_client(False)
    if hasattr(client, "timeout"):
        client.timeout = app_config.SIGNAL_AI_TIMEOUT_SECONDS
    return client


def list_signals(*, maturity: str | None = None, theme: str | None = None, limit: int = 50) -> list[dict]:
    return repository.list_signals(maturity=maturity, theme=theme, limit=limit)


def topics_from_tags(tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Темы радара = корневые тематики заказчика (13), без служебного приёмника.

    Пункт 12 Виктора: сигналы раскладывать по его тегам. Прежние 21 тема (4 HSE +
    17 направлений из сида) давали 25 из 38 сигналов с темой «HSE/…» — отсюда
    «меньше в основных, больше в технологических»."""
    topics = []
    for tag in tags:
        if tag.get("parent_id") is not None:
            continue
        name = str(tag.get("name") or "").strip()
        if not name or name == repository.SYSTEM_TAG_UNCLASSIFIED:
            continue
        topics.append({
            "name": name,
            "description": str(tag.get("description") or "").strip(),
            "query_seeds_json": [],
            "tag_id": tag.get("id"),
        })
    return topics


def _radar_topics() -> list[dict]:
    if app_config.SIGNAL_RADAR_TOPIC_SOURCE == "tags":
        try:
            topics = topics_from_tags(repository.list_enabled_tags())
        except Exception:  # noqa: BLE001 - без справочника тегов откатываемся на таблицу тем
            topics = []
        if topics:
            return topics
    return repository.list_signal_radar_topics(enabled_only=True)


def _selected_topics(topic: str | None) -> list[dict]:
    rows = _radar_topics()
    if not topic:
        # Поиск широкий (#138): все темы, и рынок тоже; что брать, решает «Отбор».
        return rows or DEFAULT_RADAR_TOPICS
    topic_l = topic.lower()
    selected = [row for row in rows if topic_l in str(row.get("name") or "").lower()]
    if selected:
        return selected
    default_selected = [row for row in DEFAULT_RADAR_TOPICS if topic_l in row["name"].lower()]
    return default_selected or [{"name": topic, "query_seeds_json": []}]


def search_topic_names() -> list[str]:
    """Темы, которые ищет прогон радара (пробный прогон #138 считает по ним вызовы и цену)."""
    return [str(topic.get("name") or "") for topic in _selected_topics(None)]


def _research_days(config: SignalDiscoveryConfig) -> int:
    return int(config.research_days or 0) or max(int(config.days or 0), app_config.SIGNAL_RESEARCH_DAYS)


def _known_titles(snapshot: dict[str, Any], topic_name: str, *, since: date) -> list[str]:
    """Карточки темы, которые поиск может принести снова: радар видел их за период поиска.

    Раньше здесь были только fresh — первое появление за 3 дня (окно дедупа). Разовые прогоны
    идут реже: 14.10 и 17.10 свежих на старте не было ни одной, список пуст во всех темах, и
    15 и 20 находок оказались уже известными карточками. Порядок — по последней находке, свежие
    первыми: при пределе на тему (signal_feedback.RESEARCH_KNOWN_TITLES) старые уходят первыми.
    Снимок ядра без last_seen_day — по дню первой находки; без обоих дней — как раньше, fresh."""
    start = since.isoformat()
    known = []
    for row in snapshot.get("existing_signals") or []:
        if str(row.get("theme") or "") != topic_name:
            continue
        day = str(row.get("last_seen_day") or row.get("first_seen_day") or "")
        if (day >= start) if day else row.get("fresh"):
            known.append((day, str(row.get("title_ru") or row.get("title") or "")))
    known.sort(key=lambda item: item[0], reverse=True)
    return [title for _, title in known]


def _topic_web_evidence(
    topic: dict[str, Any],
    topic_name: str,
    config: SignalDiscoveryConfig,
    *,
    heartbeat: Callable[[], None],
    known_titles: list[str] | None = None,
    collected: set[str] | frozenset[str] = frozenset(),
    covered: list[str] | None = None,
) -> dict[str, Any]:
    """Находки темы: поиск GPT только находит (signal_research, #131), текст страницы хранится до предела входа
    «Отбора» (TEXT_LIMIT, как у статьи сбора). Широкий поиск (#138): ссылка из статей сбора (collected: ключи
    normalize.url_key) отбрасывается до докачки страницы, сайты, которые парсер собирает целиком (covered), поиск
    получает как «уже есть, ищи дальше»."""
    heartbeat()
    context = _topic_tag_context(topic_name)
    topic_context = "\n".join(part for part in (
        "; ".join(context.get("descriptions") or []),
        "Ключевые слова: " + ", ".join((context.get("keywords_ru") or [])[:12] + (context.get("keywords_en") or [])[:12]),
    ) if part.strip() and not part.endswith(": "))
    # Граница данных О-7 (спека #128, решение владельца 03.11): из прошлого поиску нужны только уже найденное и сайты
    # сбора; ОС экспертов (одобренные и отклонённые с причинами, правила, доверенные сайты) в его промпт не идёт. ОС
    # поиска строится позже из итогов воронки.
    feedback = "\n\n".join(part for part in (signal_research.covered_sites_block(covered or []),
                                             known_titles_block(known_titles)) if part)
    _, period_end = _research_window(config)
    research = signal_research.research_topic(topic_name, days=_research_days(config), period_end=period_end,
                                              limit=config.research_limit or None, topic_context=topic_context,
                                              heartbeat=heartbeat, feedback=feedback,
                                              model=config.research_model, reasoning=config.research_reasoning)
    heartbeat()
    # Докачка проверяет, живая ли ссылка и о том ли она; текст страницы идёт «Отбору». Срока на тему нет: находка без
    # страницы во вход агента не идёт, и срезать её по времени значило бы потерять её молча (признак продвижения
    # подаётся перед каждой страницей, аренда держится). По той же причине после запроса идёт добор (PDF, ядро РФ,
    # браузер; порядок цикла владельца 04.11), до дедупа прогона.
    events, already = _split_collected(research.pop("evidence"), collected)
    research["already_collected"] = already
    found, fulltext = _enrich_web_evidence_with_full_text(events, topic_name, heartbeat=heartbeat)
    verified, unverified = signal_research.verify_research_evidence(found)
    research.update(fulltext=fulltext, unverified=unverified, evidence_count=len(verified))
    _record_usage("radar_research", research.get("model"), calls=int(research.get("attempts") or 0),
                  input_tokens=int(research.get("input_tokens") or 0),
                  output_tokens=int(research.get("output_tokens") or 0),
                  web_search_calls=int(research.get("web_search_calls") or 0))
    return {"status": research["status"], "provider": signal_research.EVIDENCE_SOURCE, "research": research,
            "evidence": verified}


def _split_collected(events: list[dict[str, Any]], collected: set[str] | frozenset[str],
                     ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Находки поиска, чья ссылка уже есть в статьях сбора (#138), — не новые материалы: (новые, уже собранные).
    Ключ — тот же, что у статьи сбора (normalize.url_key): другое написание адреса — тот же материал."""
    if not collected:
        return events, []
    fresh, already = [], []
    for item in events:
        url = str(item.get("source_url") or "")
        if url and article_url_key(url) in collected:
            already.append({"title": _trim(str(item.get("title") or ""), 300), "url": url})
        else:
            fresh.append(item)
    return fresh, already


# Сколько ключей другой тематики должно найтись в карточке, чтобы тема ушла от запроса.
# Одно совпадение — не повод: «бурение» мелькает в обзоре про дроны мимоходом.
THEME_SWITCH_MIN_HITS = 2


def _content_theme(
    signal: dict[str, Any],
    cluster: list[dict[str, Any]],
    search_topic: str,
    topic_names: list[str],
) -> tuple[str, dict[str, Any]]:
    """Тема карточки из 13 тематик заказчика — по содержанию, а не по запросу.

    Раньше тема была темой поиска: статья про ИИ и дроны, найденная запросом по
    бурению, получала «бурение» (замечание заказчика 22.09). Теперь считаем ключи
    каждой тематики (с подтегами) в заголовке, сути и тезисе карточки и в заголовках
    её статей. Тема поиска остаётся, если её никто не обгоняет хотя бы на
    THEME_SWITCH_MIN_HITS совпадений; при равенстве — тоже она.
    """
    text = _norm_match_text(" ".join(
        [str(signal.get(field) or "") for field in ("title", "title_ru", "summary", "thesis")]
        + [str(item.get("title") or "") for item in cluster]
    ))
    hits: dict[str, int] = {}
    for name in _dedupe([search_topic, *topic_names]):
        context = _topic_tag_context(name)
        keywords = _dedupe([*context["keywords_ru"], *context["keywords_en"]])
        hits[name] = sum(1 for keyword in keywords if _contains_negative_keyword(text, keyword))
    best = max(hits, key=lambda name: (hits[name], name == search_topic))
    if best != search_topic and hits[best] >= THEME_SWITCH_MIN_HITS and hits[best] > hits.get(search_topic, 0):
        chosen, reason = best, "content"
    elif hits.get(search_topic, 0):
        chosen, reason = search_topic, "search_topic_confirmed"
    else:
        # Ключей не нашлось ни у одной тематики: оставляем тему поиска, но помечаем —
        # по таким карточкам видно, где словарь тематик не покрывает находки радара.
        chosen, reason = search_topic, "no_keywords"
    return chosen, {
        "search_topic": search_topic,
        "theme": chosen,
        "reason": reason,
        "hits": {name: count for name, count in hits.items() if count},
    }


def _topic_tag_context(topic_name: str) -> dict[str, Any]:
    tags = _TAGS_SNAPSHOT.get()
    if tags is None:
        try:
            tags = repository.list_enabled_tags()
        except Exception:  # noqa: BLE001 - tag context is an enrichment, not a hard dependency for search
            tags = []
    selected = _select_topic_tags(topic_name, tags)
    return {
        "keywords_ru": _dedupe(_flatten_tag_values(selected, "keywords_json"))[:20],
        "keywords_en": _dedupe(_flatten_tag_values(selected, "keywords_en_json"))[:20],
        "descriptions": _dedupe([str(row.get("description") or "").strip() for row in selected if row.get("description")])[:8],
    }


def _select_topic_tags(topic_name: str, tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not topic_name or not tags:
        return []
    topic_norm = _norm_match_text(topic_name)
    # Тема = тематика заказчика по имени — берём её и её подтеги, и только их. Иначе
    # пересечение по общим словам («оборудование», «промышленных») притягивало соседние
    # тематики, и «Добыча…» искала заодно ключами «Бурения…».
    exact = [tag for tag in tags if _norm_match_text(str(tag.get("name") or "")) == topic_norm]
    if exact:
        exact_ids = {int(tag["id"]) for tag in exact if tag.get("id") is not None}
        return [
            tag for tag in tags
            if (tag.get("id") is not None and int(tag["id"]) in exact_ids)
            or (tag.get("parent_id") is not None and int(tag["parent_id"]) in exact_ids)
        ][:24]
    direct_ids: set[int] = set()
    direct_parent_ids: set[int] = set()
    for tag in tags:
        if _tag_matches_topic(tag, topic_norm):
            tag_id = tag.get("id")
            parent_id = tag.get("parent_id")
            if tag_id is not None:
                direct_ids.add(int(tag_id))
            if parent_id is not None:
                direct_parent_ids.add(int(parent_id))

    selected = []
    for tag in tags:
        tag_id = tag.get("id")
        parent_id = tag.get("parent_id")
        include = False
        if tag_id is not None and int(tag_id) in direct_ids:
            include = True
        if tag_id is not None and int(tag_id) in direct_parent_ids:
            include = True
        if parent_id is not None and int(parent_id) in direct_ids:
            include = True
        if include:
            selected.append(tag)
    return selected[:24]


def _tag_matches_topic(tag: dict[str, Any], topic_norm: str) -> bool:
    fields = [
        tag.get("name"),
        tag.get("name_en"),
        tag.get("parent_name"),
        tag.get("parent_name_en"),
        tag.get("description"),
    ]
    for field in fields:
        field_norm = _norm_match_text(str(field or ""))
        if field_norm and (field_norm in topic_norm or topic_norm in field_norm):
            return True
        if field_norm and _meaningful_token_overlap(topic_norm, field_norm):
            return True
    for keyword in (tag.get("keywords_json") or []) + (tag.get("keywords_en_json") or []):
        keyword_norm = _norm_match_text(str(keyword or ""))
        if keyword_norm and (keyword_norm in topic_norm or _meaningful_token_overlap(topic_norm, keyword_norm)):
            return True
    return False


def _meaningful_token_overlap(left: str, right: str) -> bool:
    left_tokens = {token for token in re.findall(r"[a-zа-яё0-9]{4,}", left) if token not in {"and", "with", "news"}}
    right_tokens = {token for token in re.findall(r"[a-zа-яё0-9]{4,}", right) if token not in {"and", "with", "news"}}
    return bool(left_tokens & right_tokens)


def _norm_match_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


def _flatten_tag_values(tags: list[dict[str, Any]], field: str) -> list[str]:
    values: list[str] = []
    for tag in tags:
        for value in tag.get(field) or []:
            text = str(value or "").strip()
            if text:
                values.append(text)
    return values


def _contains_negative_keyword(text: str, keyword: str) -> bool:
    keyword_norm = _norm_match_text(keyword)
    if not keyword_norm:
        return False
    if re.search(r"[\u3400-\u9fff]", keyword_norm):
        return keyword_norm in text
    if re.fullmatch(r"[a-zа-яё0-9 ]+", keyword_norm):
        pattern = r"(?<![a-zа-яё0-9])" + re.escape(keyword_norm) + r"(?![a-zа-яё0-9])"
        return bool(re.search(pattern, text))
    return keyword_norm in text


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _fetch_full_text(url: str, fallback_title: str = "", *, timeout: int | None = None) -> dict[str, Any]:
    """Открыть страницу находки и достать текст издателя вместо сводки модели поиска.

    Страница нужна находке: во вход «Отбора» идёт только текст издателя. Поэтому после запроса идёт добор (поручение
    владельца 04.11): PDF, ядро РФ и браузер (publisher_page.read); итог несёт fetcher, чем открыта страница. Таймаут
    одной попытки: SIGNAL_DISCOVERY_FULLTEXT_TIMEOUT_SECONDS. Вынесено в отдельную функцию: тесты подменяют её
    целиком, не трогая сеть."""
    return publisher_page.read(url, fallback_title,
                               timeout=timeout or app_config.SIGNAL_DISCOVERY_FULLTEXT_TIMEOUT_SECONDS,
                               weak_title=signal_research.is_list_page)


def _enrich_web_evidence_with_full_text(
    evidence: list[dict[str, Any]],
    topic: str,
    *,
    heartbeat: Callable[[], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Докачать страницу каждой находки: текст издателя до TEXT_LIMIT знаков (предел входа «Отбора»). В счётчиках
    via: сколько страниц открыли ядро РФ и браузер, pdf: сколько прочитано из PDF; в находке: full_text_fetcher (и
    full_text_pdf).

    Любая ошибка страницы ставит пометку у находки (full_text_fetched = False), а не роняет тему: 21.09 пустое тело
    (200) роняло lxml ParserError сквозь весь прогон. Находку без страницы отсеет проверка поиска
    (signal_research.verify_research_evidence). Перед каждой страницей heartbeat: срока на тему нет, аренда держится
    признаком продвижения."""
    beat = heartbeat or (lambda: None)
    stats: dict[str, Any] = {"attempted": 0, "fetched": 0, "too_short": 0, "failed": 0,
                             "via": dict.fromkeys(publisher_page.TOP_UP_FETCHERS, 0), "pdf": 0}
    enriched: list[dict[str, Any]] = []
    for item in evidence:
        url = str(item.get("source_url") or "")
        if not url:
            enriched.append(item)
            continue
        beat()
        stats["attempted"] += 1
        try:
            fetched = _fetch_full_text(url, str(item.get("title") or ""))
        except Exception as exc:  # noqa: BLE001 - одна страница не роняет тему
            fetched = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}", "raw_text": ""}
        raw_text = _clean_search_text(fetched.get("raw_text") or "")
        if not fetched.get("ok") or len(raw_text) < app_config.MIN_ARTICLE_TEXT_CHARS:
            stats["failed" if not fetched.get("ok") else "too_short"] += 1
            enriched.append({
                **item,
                "raw_payload": {
                    **(item.get("raw_payload") or {}),
                    "full_text_fetched": False,
                    "full_text_error": fetched.get("error") or "too_short",
                },
            })
            continue

        stats["fetched"] += 1
        fetcher = fetched.get("fetcher")
        if fetcher in stats["via"]:
            stats["via"][fetcher] += 1
        if fetched.get("pdf"):
            stats["pdf"] += 1
        title = fetched.get("title") or item.get("title") or ""
        fact = raw_text[:TEXT_LIMIT]
        context = {"title": title, "raw_text": f"{title}\n{fact}", "language": "mixed"}
        enriched.append({
            **item,
            "title": title,
            "title_ru": _enforce_glossary(title, context, topic),
            # Строкой ISO, как все даты снимка: объект datetime ронял отправку итога
            # воркера ядру (проверка 4712, 21.09) — тот же класс, что сбор 18.09.
            "published_at": _iso(fetched.get("published_at")) or item.get("published_at"),
            "evidence_type": _evidence_type(f"{title} {fact}"),
            "extracted_fact": fact,
            "summary_ru": _enforce_glossary(fact, context, topic),
            "raw_payload": {
                **(item.get("raw_payload") or {}),
                "full_text_fetched": True,
                "full_text_chars": len(raw_text),
                **({"full_text_fetcher": fetcher} if fetcher else {}),
                **({"full_text_pdf": True} if fetched.get("pdf") else {}),
            },
        })
    return enriched, stats


def _clean_search_text(text: str) -> str:
    return publisher_page.clean_text(text)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        normalized = re.sub(r"\s+", " ", value).strip()
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _dedupe_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Одна ссылка, одна находка: другое написание адреса (схема, www, хвост, метки) даёт ту же ссылку. Без ссылки
    находки нет: поиск отбрасывает такую до проверок (signal_research.NO_LINK_OR_TITLE)."""
    seen: set[str] = set()
    result = []
    for item in evidence:
        url = _normalize_url_for_key(str(item.get("source_url") or ""))
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(item)
    return result


def _cluster_evidence(evidence: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Находка поиска идёт своей группой из одной ссылки: модель поиска уже разложила материалы по событиям, а склейка
    по словам смешала бы разные события одной компании (сигнал 71). События сводит «Дедуп» прогона. Порядок по силе
    источника (первоисточник выше пересказа), при равенстве как вернул поиск."""
    return [[item] for item in sorted(evidence, key=lambda item: float(item.get("strength") or 0), reverse=True)]


def _normalize_event_date(value: Any) -> str | None:
    """ГГГГ-ММ-ДД или None. Дата из будущего (больше суток вперёд) — ошибка модели, не событие."""
    text = str(value or "").strip()[:10]
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None
    if parsed > datetime.now(RADAR_TZ).date() + timedelta(days=1):
        return None
    return parsed.isoformat()


def _enforce_glossary(text: str, context: dict[str, Any], topic: str | None = None) -> str:
    return apply_feedback_glossary(enforce_glossary_text(text, context), topic)


def _signal_key(evidence: list[dict[str, Any]]) -> str:
    """Ключ находки по её ссылке: та же ссылка в следующем прогоне даёт ту же находку, найденную снова, и решённое
    «Отбором» второй раз не решается. Формула прежняя («url:» и адрес без схемы, www и хвоста): ключи находок,
    заведённых до 08.11, совпадают."""
    urls = sorted({_normalize_url_for_key(str(item.get("source_url") or ""))
                   for item in evidence if item.get("source_url")})
    return hashlib.sha256(f"url:{urls[0] if urls else ''}".encode("utf-8")).hexdigest()[:24]


def _normalize_url_for_key(url: str) -> str:
    url = (url or "").strip().lower()
    if not url:
        return ""
    url = re.sub(r"^https?://", "", url)
    url = url.split("#", 1)[0].split("?", 1)[0]
    url = re.sub(r"/+$", "", url)
    return url.removeprefix("www.")


def _evidence_type(text: str) -> str:
    text_l = text.lower()
    if re.search(r"deploy|implemented|внедр|запуст", text_l):
        return "deployment"
    if re.search(r"supplier|contract|поставщик|контракт", text_l):
        return "supplier"
    if re.search(r"guide|opinion|podcast|колон", text_l):
        return "analysis"
    return "article"


def _trim(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[: limit - 3].rstrip() + "..."
