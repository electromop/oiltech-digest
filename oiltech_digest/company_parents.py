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


def checked_row(child: str | None, parent: str | None, note: str | None, pairs: Sequence[Pair],
                same: Same) -> dict[str, Any]:
    """Строка списка, как её запишут, и её проверка: дочка, мать и примечание без лишних пробелов (clean), пустое
    примечание None; problem: почему строку нельзя добавить к списку pairs (check_pair, затем длина примечания),
    или None. Одна проверка у формы на экране и у вставки списком (parse_lines)."""
    child, parent, note = clean(child), clean(parent), clean(note)
    problem = check_pair(child, parent, pairs, same) or (
        f"Примечание длиннее {MAX_NOTE} знаков" if len(note) > MAX_NOTE else None)
    return {"child_name": child, "parent_name": parent, "note": note or None, "problem": problem}


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
    можно). Пустые строки пропускаются; line: номер строки в тексте. problem: ошибка формата или причина
    checked_row против списка pairs и строк вставки выше (строка с ошибкой в счёт не идёт), иначе None.
    ValueError: непустых строк нет или больше MAX_LINES."""
    numbered = [(number, line) for number, line in enumerate(str(text or "").splitlines(), 1) if line.strip()]
    if not numbered:
        raise ValueError("Нет ни одной строки «дочка; мать»")
    if len(numbered) > MAX_LINES:
        raise ValueError(f"Не больше {MAX_LINES} строк за раз, во вставке {len(numbered)}")
    # Одни и те же имена сверяются строка за строкой: правило считаем один раз на пару имён.
    remembered = functools.cache(same)
    known = list(pairs)
    rows: list[dict[str, Any]] = []
    for number, line in numbered:
        child, parent, note = (*line.split(";", 2), "", "")[:3]
        row = {"line": number, **checked_row(child, parent, note, known, remembered)}
        if ";" not in line:
            # Строка не разобрана: ошибка формата, а не проверки пары.
            row["problem"] = FORMAT
        elif row["problem"] is None:
            known.append((row["child_name"], row["parent_name"]))
        rows.append(row)
    return rows


def paste_answer(lines: Sequence[dict[str, Any]], *, applied: bool) -> dict[str, Any]:
    """Ответ на вставку списком: ok (ошибок нет), applied (строки записаны), problems (строк с ошибками) и разбор
    строк lines (parse_lines)."""
    problems = sum(1 for line in lines if line["problem"])
    return {"ok": not problems, "applied": applied, "problems": problems, "lines": list(lines)}


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
