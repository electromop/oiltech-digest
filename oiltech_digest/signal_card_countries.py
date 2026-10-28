"""Страна сигнала у «Карточки» (#137, спека #128): справочник стран у кода и правило написаний.

Страна у сигнала в двух ролях: страны события (signals.event_countries, до трёх, по месту события, иначе страна
главного участника) и страна каждого участника (поле country его записи в participants_json). Роль видна по месту
значения: фильтр «Страна» ищет по обеим (repository._COUNTRY_SQL), а разделить его можно без нового прогона модели.

Справочник — блок countries в signal_card_dictionaries.json: короткие русские имена, как у региона, написания
(полные имена, сокращения, английские) и регионы страны — таблица «страна → регион» для экзамена #133 (страна
сверяется с регионом эксперта; страна на стыке регионов называет оба). Полного списка модель не видит: пишет имя
сама, код сводит написание к имени справочника (регистр, «ё», точки и прочие знаки не различаются), чужое
отбрасывает.
"""

from __future__ import annotations

from collections.abc import Iterable
import re
from typing import Any

# Стран события не больше (спека #128).
EVENT_LIMIT = 3
_SEPARATORS_RE = re.compile(r"[\W_]+")


class Countries:
    """Справочник стран одной версии: имена по порядку справочника, написание → имя, страна → регионы."""

    def __init__(self, dictionaries: dict[str, Any]):
        entries = dictionaries.get("countries") or []
        self.names = [entry["name"] for entry in entries]
        self._regions = {entry["name"]: list(entry["regions"]) for entry in entries}
        self._by_spelling = {spelling_key(spelling): entry["name"]
                             for entry in entries for spelling in (entry["name"], *entry["aliases"])}

    def canonical(self, value: Any) -> str | None:
        """Имя справочника по написанию модели; страны нет в справочнике — None."""
        return self._by_spelling.get(spelling_key(value)) if isinstance(value, str) else None

    def event(self, values: Iterable[Any]) -> list[str]:
        """Страны события из ответа модели: имена справочника по порядку ответа, без повторов и чужих, не больше
        EVENT_LIMIT."""
        found: list[str] = []
        for value in values:
            name = self.canonical(value)
            if name and name not in found:
                found.append(name)
        return found[:EVENT_LIMIT]

    def regions(self, name: str) -> list[str]:
        """Регионы страны по таблице «страна → регион» (первый — основной); страны нет — пусто."""
        return list(self._regions.get(name) or [])


def spelling_key(value: str) -> str:
    """Написание без различий: регистр, «ё», точки сокращений («U.S.A.» — «USA»), прочие знаки — пробел."""
    text = str(value or "").replace("ё", "е").replace("Ё", "Е").casefold().replace(".", "")
    return " ".join(_SEPARATORS_RE.sub(" ", text).split())
