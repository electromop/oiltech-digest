"""Дочерние общества для фильтра «всё про компанию» (решение владельца 20.10): список «дочка → мать», проверка
новой пары, дочки компании по цепочке и разбор вставки списком.

Без базы: список пар (дочка, мать) и правило «одна ли это компания» (same) передаёт вызывающий. Правило одно с
фильтром, поэтому форма собственности, регистр и падеж в списке не повторяются: «ПАО «Газпром»» и «Газпром» одна
компания. Другое написание (латиница, прежнее название) записывается отдельной строкой. Список многоуровневый
(Газпром → Газпром нефть → Газпромнефть-Заполярье); у дочки одна мать, циклов нет: это держит check_pair.
Хранение: db/company_parents_repo.py, экран «Дочерние общества».
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
import functools
from typing import Any

# Пара списка: (дочка, мать) как написаны.
Pair = tuple[str, str]
# Одна ли это компания: правило «одна компания» (вызывающий).
Same = Callable[[str, str], bool]

MAX_NAME = 200
MAX_NOTE = 500
MAX_LINES = 500
# Знаков во вставке списком: 500 строк с двумя именами и примечанием, с запасом на пробелы.
MAX_TEXT = 500_000
FORMAT = "Нужен формат «дочка; мать», третьим полем можно примечание"


def clean(text: str | None) -> str:
    """Имя или примечание как написано, без лишних пробелов (в том числе неразрывных) по краям и внутри."""
    return " ".join(str(text or "").split())


def check_pair(child: str, parent: str, pairs: Sequence[Pair], same: Same) -> str | None:
    """Почему пару «дочка → мать» нельзя добавить к списку pairs, или None. Проверки одни для формы и вставки
    списком: имя не пустое и не длиннее MAX_NAME; дочка не та же компания, что мать; такой пары ещё нет; у
    дочки нет другой матери (перенос в другую группу: сначала удалить старую строку); мать не сама дочка и
    не её дочка по цепочке (цикл)."""
    child, parent = clean(child), clean(parent)
    for role, name in (("дочки", child), ("матери", parent)):
        if not name:
            return f"Не указано имя {role}"
        if len(name) > MAX_NAME:
            return f"Имя {role} длиннее {MAX_NAME} знаков"
    if same(child, parent):
        return f"Дочка и мать: одна и та же компания («{child}» и «{parent}»)"
    for known_child, known_parent in pairs:
        if same(child, known_child):
            if same(parent, known_parent):
                return f"Такая пара уже есть: «{known_child}» → «{known_parent}»"
            return f"У «{known_child}» уже есть мать «{known_parent}»: удалите старую строку"
    for chain in _walk(parent, pairs, same, up=True):
        if same(chain[-1], child):
            return "Получится цикл: " + " → ".join(f"«{name}»" for name in (child, parent, *chain))
    return None


def check_note(note: str | None) -> str | None:
    """Почему примечание не подходит, или None."""
    return f"Примечание длиннее {MAX_NOTE} знаков" if len(clean(note)) > MAX_NOTE else None


def descendants(name: str, pairs: Sequence[Pair], same: Same) -> list[str]:
    """Все дочки компании по цепочке, как написаны в списке: Газпром → Газпром нефть → Газпромнефть-Заполярье.
    Ближние первыми. Мать в ответ не входит: клик по дочке показывает только её и её дочек. Битый список
    (цикл, вторая мать, правка мимо экрана) не зацикливает и не повторяет компанию."""
    name = clean(name)
    found: list[str] = []
    if not name:
        return found
    for chain in _walk(name, pairs, same, up=False):
        if not any(same(chain[-1], seen) for seen in (name, *found)):
            found.append(chain[-1])
    return found


def parse_lines(text: str, pairs: Sequence[Pair], same: Same) -> list[dict[str, Any]]:
    """Разбор вставки списком: строка «дочка; мать», третьим полем можно примечание (точку с запятой в нём
    можно). Пустые строки пропускаются; line — номер строки в тексте. problem — ошибка формата или причина
    check_pair против списка pairs и строк вставки выше (строка с ошибкой в счёт не идёт), иначе None.
    ValueError — непустых строк больше MAX_LINES."""
    numbered = [(number, line) for number, line in enumerate(str(text or "").splitlines(), 1) if line.strip()]
    if len(numbered) > MAX_LINES:
        raise ValueError(f"Не больше {MAX_LINES} строк за раз, во вставке {len(numbered)}")
    # Одни и те же имена сверяются строка за строкой: правило считаем один раз на пару имён.
    remembered = functools.cache(same)
    known = list(pairs)
    rows: list[dict[str, Any]] = []
    for number, line in numbered:
        fields = [clean(field) for field in line.split(";", 2)]
        child, parent, note = (*fields, "", "")[:3]
        problem = FORMAT if len(fields) < 2 else check_pair(child, parent, known, remembered) or check_note(note)
        rows.append({"line": number, "child_name": child, "parent_name": parent, "note": note or None,
                     "problem": problem})
        if problem is None:
            known.append((child, parent))
    return rows


def _walk(start: str, pairs: Sequence[Pair], same: Same, *, up: bool) -> Iterator[list[str]]:
    """Обход списка от компании по цепочке, ближние первыми: вверх по матерям (up) или вниз по дочкам. Даёт
    цепочку имён от соседа start до найденного, как написаны в списке. Каждая пара проходится один раз:
    битый список с циклом обход не зацикливает."""
    queue: list[list[str]] = [[]]
    used: set[int] = set()
    while queue:
        chain = queue.pop(0)
        here = chain[-1] if chain else start
        for index, (child, parent) in enumerate(pairs):
            near, far = (child, parent) if up else (parent, child)
            if index in used or not same(near, here):
                continue
            used.add(index)
            queue.append([*chain, far])
            yield queue[-1]
