"""Seed tags and scoring criteria from domain workbooks/defaults."""

from __future__ import annotations

import pathlib
import re

import openpyxl

from oiltech_digest.config import DIRECTIONS_XLSX
from oiltech_digest.db import repository


DIRECTIONS_SHEET = "Направления"
KEYWORDS_SHEET = "Ключевые слова"


DEFAULT_SCORING_CRITERIA = [
    {
        "name": "Технологическая новизна",
        "description": "Насколько материал описывает новую или заметно улучшенную технологию, сервис, оборудование или метод.",
        "weight": 35,
        "keywords_json": ["новая технология", "пилот", "разработка", "автоматизация", "инновация"],
        "keywords_en_json": ["new technology", "pilot", "development", "automation", "innovation"],
        "sort_order": 10,
    },
    {
        "name": "Применимость для РФ и зрелых активов",
        "description": "Насколько решение потенциально применимо в российских нефтегазовых условиях, на зрелых месторождениях или в сложной логистике.",
        "weight": 30,
        "keywords_json": ["зрелые месторождения", "импортозамещение", "трудноизвлекаемые", "снижение затрат"],
        "keywords_en_json": ["mature fields", "hard-to-recover", "cost reduction", "remote operations"],
        "sort_order": 20,
    },
    {
        "name": "Бизнес-эффект",
        "description": "Ожидаемый эффект по добыче, срокам, безопасности, CAPEX/OPEX, НПВ или операционной устойчивости.",
        "weight": 25,
        "keywords_json": ["эффект", "экономия", "снижение затрат", "рост добычи", "безопасность"],
        "keywords_en_json": ["efficiency", "cost savings", "production increase", "safety", "NPT reduction"],
        "sort_order": 30,
    },
    {
        "name": "Достоверность и зрелость сигнала",
        "description": "Надёжность источника и зрелость события: промышленный запуск, контракт, результаты испытаний важнее ранних заявлений.",
        "weight": 10,
        "keywords_json": ["контракт", "промышленный", "результаты испытаний", "внедрение"],
        "keywords_en_json": ["contract", "commercial deployment", "field trial", "test results", "implementation"],
        "sort_order": 40,
    },
]


TAG_SIGNAL_ENRICHMENT = [
    {
        "match": ("робот", "автоном"),
        "keywords_ru": [
            "роботизация опасных операций",
            "автономная инспекция",
            "робот-инспектор",
            "снижение присутствия человека",
            "опасная зона",
            "дистанционный обход",
            "физический ИИ",
        ],
        "keywords_en": [
            "physical AI",
            "autonomous inspection robot",
            "robotic inspection",
            "hazardous area robot",
            "remote inspection",
            "reduce human exposure",
            "robotic drilling",
            "red zone removal",
            "autonomous mobile robot",
        ],
        "keywords_cn": [
            "具身智能",
            "物理人工智能",
            "巡检机器人",
            "防爆巡检机器人",
            "自主巡检",
            "危险作业机器人",
            "井场机器人",
            "油气机器人",
            "无人化巡检",
        ],
    },
    {
        "match": ("экология", "промышленная безопасность", "hse", "устойчив"),
        "keywords_ru": [
            "предиктивная безопасность",
            "предотвращение инцидентов",
            "контроль опасных зон",
            "цифровой наряд-допуск",
            "динамический наряд-допуск",
            "LOTO",
            "SIMOPS",
            "усталость водителя",
            "предотвращение столкновений",
        ],
        "keywords_en": [
            "predictive HSE",
            "predictive safety",
            "preventive safety",
            "continuous control assurance",
            "digital permit to work",
            "dynamic permit to work",
            "control of work",
            "LOTO",
            "SIMOPS",
            "fatigue detection",
            "collision avoidance",
            "near miss",
            "danger zone detection",
        ],
        "keywords_cn": [
            "预测性安全",
            "智能安全",
            "作业许可",
            "电子作业票",
            "特殊作业票",
            "安全生产",
            "双重预防机制",
            "风险分级管控",
            "隐患排查治理",
            "疲劳驾驶监测",
            "防碰撞系统",
            "人员定位",
        ],
    },
    {
        "match": ("логистика", "транспорт", "supply chain"),
        "keywords_ru": [
            "автономное вмешательство",
            "предотвращение столкновений",
            "усталость оператора",
            "слепая зона",
            "телематика спецтехники",
            "опасное сближение",
        ],
        "keywords_en": [
            "vehicle intervention system",
            "collision intervention",
            "collision avoidance system",
            "EMESRT Level 9",
            "operator alertness",
            "fatigue monitoring",
            "blind spot detection",
            "proximity detection",
            "heavy equipment safety",
        ],
        "keywords_cn": [
            "矿卡防碰撞",
            "车辆干预系统",
            "主动防碰撞",
            "盲区监测",
            "疲劳监测",
            "矿山无人驾驶",
            "智能矿卡",
            "人员车辆防碰撞",
        ],
    },
    {
        "match": ("бурение", "буровое"),
        "keywords_ru": [
            "автоматизированная буровая",
            "роботизация буровой",
            "красная зона буровой",
            "удаленное управление буровой",
            "автоматизация спуско-подъемных операций",
        ],
        "keywords_en": [
            "automated drilling rig",
            "robotic drilling",
            "drill floor automation",
            "red zone automation",
            "remote drilling operations",
            "pipe handling robot",
            "automated pipe handling",
        ],
        "keywords_cn": [
            "自动化钻机",
            "智能钻井",
            "钻台自动化",
            "管柱自动处理",
            "钻井机器人",
            "远程钻井",
        ],
    },
    {
        "match": ("добыча", "механизирован"),
        "keywords_ru": [
            "предиктивная диагностика оборудования",
            "состояние оборудования",
            "автоматический обход",
            "цифровой двойник промысла",
        ],
        "keywords_en": [
            "predictive maintenance",
            "condition monitoring",
            "autonomous field inspection",
            "digital twin operations",
            "equipment health monitoring",
        ],
        "keywords_cn": [
            "预测性维护",
            "设备状态监测",
            "智能油田",
            "数字孪生油田",
            "油田无人巡检",
        ],
    },
    {
        "match": ("промысловая инфраструктура", "surface facilities", "обустрой"),
        "keywords_ru": [
            "безлюдный объект",
            "автономная инспекция объекта",
            "мониторинг утечек",
            "газоанализ",
            "цифровой двойник объекта",
        ],
        "keywords_en": [
            "unmanned facility",
            "autonomous plant inspection",
            "gas leak detection",
            "remote operations center",
            "site surveillance",
            "environmental monitoring",
        ],
        "keywords_cn": [
            "无人站场",
            "无人值守",
            "智能巡检",
            "泄漏检测",
            "气体检测",
            "远程运维",
            "油气站场",
        ],
    },
    {
        "match": ("рынок", "экономика", "контракт", "m&a"),
        "keywords_ru": [
            "масштабирование технологии",
            "промышленное внедрение",
            "поставщик технологии",
            "рамочный контракт",
            "совместная разработка",
        ],
        "keywords_en": [
            "commercial deployment",
            "technology supplier",
            "framework agreement",
            "strategic partnership",
            "joint development",
            "field deployment",
        ],
        "keywords_cn": [
            "商业化应用",
            "规模化应用",
            "战略合作",
            "联合研发",
            "示范应用",
            "现场应用",
        ],
    },
]


def seed_tags_from_directions(path=DIRECTIONS_XLSX) -> dict:
    """Load D01-D18 as top-level tags with RU/EN keywords."""
    wb = openpyxl.load_workbook(path, data_only=True)
    try:
        directions = _sheet_dicts(wb[DIRECTIONS_SHEET])
        keyword_rows = {row["ID направления"]: row for row in _sheet_dicts(wb[KEYWORDS_SHEET])}

        total = 0
        for order, row in enumerate(directions, start=10):
            direction_id = row.get("ID")
            if not direction_id:
                continue
            keywords = keyword_rows.get(direction_id, {})
            ru_keywords = _split_keywords(keywords.get("Ключевые слова RU"))
            en_keywords = _split_keywords(keywords.get("Keywords EN"))
            extra = _tag_signal_enrichment(
                row.get("Направление RU") or "",
                row.get("Direction EN") or "",
            )
            repository.upsert_tag(
                {
                    "parent_id": None,
                    "name": row.get("Направление RU") or row.get("Direction EN"),
                    "name_en": row.get("Direction EN"),
                    "description": row.get("Что покрывает"),
                    "keywords_json": _dedupe([*ru_keywords, *extra["keywords_ru"]]),
                    "keywords_en_json": _dedupe([*en_keywords, *extra["keywords_en"], *extra["keywords_cn"]]),
                    "sort_order": order,
                }
            )
            total += 1
        return {"tags": total}
    finally:
        wb.close()


def seed_default_scoring_criteria() -> dict:
    for rec in DEFAULT_SCORING_CRITERIA:
        repository.upsert_scoring_criterion(rec)
    return {"criteria": len(DEFAULT_SCORING_CRITERIA), "weight_sum": 100}


def _sheet_dicts(ws) -> list[dict]:
    rows = list(ws.iter_rows(values_only=True))
    header = [str(c).strip() if c is not None else "" for c in rows[0]]
    result = []
    for row in rows[1:]:
        rec = {}
        for i, name in enumerate(header):
            if not name:
                continue
            value = row[i] if i < len(row) else None
            rec[name] = str(value).strip() if value is not None else ""
        result.append(rec)
    return result


def _split_keywords(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r";|\n", value) if part and part.strip()]


def _tag_signal_enrichment(name_ru: str, name_en: str = "") -> dict[str, list[str]]:
    haystack = f"{name_ru} {name_en}".lower()
    result = {"keywords_ru": [], "keywords_en": [], "keywords_cn": []}
    for item in TAG_SIGNAL_ENRICHMENT:
        if any(token.lower() in haystack for token in item["match"]):
            result["keywords_ru"].extend(item.get("keywords_ru", []))
            result["keywords_en"].extend(item.get("keywords_en", []))
            result["keywords_cn"].extend(item.get("keywords_cn", []))
    return {key: _dedupe(value) for key, value in result.items()}


def _dedupe(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        item = str(value or "").strip()
        key = item.casefold()
        if item and key not in seen:
            result.append(item)
            seen.add(key)
    return result


# =========================================================================
# 13 тематик заказчика (13.09.2026) — заменяют 18 направлений D01–D18
# =========================================================================
# Список прислан заказчиком файлом «Теги платформы, 13 тематик»: у каждой тематики
# описание для модели, ключевые слова RU и EN и стоп-слова. Исходник лежит рядом
# с сидером (data/seed/tags_13_tematik.md) — теги обязаны быть воспроизводимы из
# репозитория, а не существовать только в проде.
#
# Прежние 18 направлений НЕ удаляются, а выключаются: на них ссылается article_tags,
# и удаление уничтожило бы историю классификации 11 тысяч статей.
TAGS_13_FILE = "tags_13_tematik.md"

# Явный тег для непопавшего. Прямая рекомендация заказчика в том же файле: «родительский
# тег не должен быть фильтром допуска статьи; если статья не совпадает ни с одной
# тематикой, она должна попадать в Unclassified / потенциально новая тема, чтобы
# Discovery Agent не был ограничен текущей taxonomy».
# Он же закрывает найденный дефект: при неудачном тегировании статья молча уезжала
# в ПЕРВЫЙ тег списка (pipeline.keyword_tag → tags[0]), то есть в «Геологоразведку».
UNCLASSIFIED_TAG = repository.SYSTEM_TAG_UNCLASSIFIED


def _parse_tags_13(text: str) -> list[dict]:
    """Разобрать файл заказчика в записи тегов.

    Формат жёсткий и задан им же: «## N. Название», затем блоки «**Описание для AI**»,
    «**Ключевые слова RU**», «**Keywords EN**», «**Стоп-слова**». Прочерк «—» в
    стоп-словах означает «их нет», а не название стоп-слова.
    """
    records: list[dict] = []
    parts = re.split(r"\n## (\d+)\. ", text)[1:]
    for i in range(0, len(parts), 2):
        number, body = parts[i], parts[i + 1]
        name = body.split("\n")[0].strip()

        def grab(label: str) -> str:
            m = re.search(rf"\*\*{label}\*\*\s*\n(.+?)(?=\n\n\*\*|\n\n## |\Z)", body, re.S)
            return m.group(1).strip() if m else ""

        def split_list(raw: str) -> list[str]:
            # Прочерк «—» в файле означает «их нет». Плюс у последней тематики в этот же
            # блок попадает горизонтальная линейка «---», которой файл отделяет
            # рекомендации — её тоже надо отбросить, иначе «---» уедет в стоп-слово
            # и начнёт отбивать статьи по подстроке.
            raw = " ".join(raw.split())
            words = [w.strip() for w in raw.split(",")]
            return [w for w in words if w and w.strip("—-–— ")]

        records.append({
            "sort_order": int(number),
            "name": name,
            "description": " ".join(grab("Описание для AI").split()),
            "keywords_json": split_list(grab("Ключевые слова RU")),
            "keywords_en_json": split_list(grab("Keywords EN")),
            "negative_keywords_json": split_list(grab("Стоп-слова")),
        })
    return records


def seed_tags_13(path: str | None = None) -> dict:
    """Завести 13 тематик заказчика и выключить всё, чего нет в списке.

    Возвращает, сколько заведено и сколько прежних тегов выключено — цифры уходят
    в вывод CLI, чтобы результат прогона был виден, а не молчалив.
    """
    source = pathlib.Path(path) if path else pathlib.Path(DIRECTIONS_XLSX).parent / TAGS_13_FILE
    records = _parse_tags_13(source.read_text(encoding="utf-8"))
    if len(records) != 13:
        raise ValueError(f"Ожидалось 13 тематик, разобрано {len(records)} — проверьте {source}")

    keep_names = []
    for rec in records:
        repository.upsert_tag({
            "parent_id": None,
            "name": rec["name"],
            "name_en": None,
            "description": rec["description"],
            "keywords_json": rec["keywords_json"],
            "keywords_en_json": rec["keywords_en_json"],
            "negative_keywords_json": rec["negative_keywords_json"],
            "sort_order": rec["sort_order"],
        })
        keep_names.append(rec["name"])

    # Тег-приёмник идёт последним по порядку и БЕЗ ключевых слов: он не должен
    # выигрывать сопоставление, он нужен только как явное «не подошло ни к чему».
    repository.upsert_tag({
        "parent_id": None, "name": UNCLASSIFIED_TAG, "name_en": "Unclassified",
        "description": ("Статья не отнесена ни к одной тематике. Не фильтр допуска, а "
                        "признак того, что тему стоит рассмотреть как новую."),
        "keywords_json": [], "keywords_en_json": [], "negative_keywords_json": [],
        "sort_order": 99,
    })
    keep_names.append(UNCLASSIFIED_TAG)

    # ВАЖНО: сид ГАРАНТИРУЕТ существование 13 тематик и БОЛЬШЕ НИЧЕГО не выключает.
    # Он запускается в bootstrap на КАЖДОМ деплое. Если бы он гасил всё, чего нет в
    # файле, то первое же переименование тега заказчиком в UI было бы отменено
    # следующей выкаткой: сид создал бы тег с исходным именем, а переименованный
    # выключил. Ровно этот класс ошибки уже сработал с критериями скоринга 11.09.
    # Выключение прежней таксономии — разовая миграция, отдельной командой `retire-tags`.
    return {"tags": len(records) + 1, "disabled": 0}


def retire_old_tags(path: str | None = None) -> dict:
    """Разовая миграция: выключить всё, чего нет в списке заказчика.

    Отделена от сида намеренно (см. комментарий выше): сид идёт на каждом деплое,
    а эта операция — осознанное решение человека сменить таксономию.
    """
    source = pathlib.Path(path) if path else pathlib.Path(DIRECTIONS_XLSX).parent / TAGS_13_FILE
    keep = [rec["name"] for rec in _parse_tags_13(source.read_text(encoding="utf-8"))]
    keep.append(UNCLASSIFIED_TAG)
    return {"disabled": repository.disable_tags_except(keep)}
