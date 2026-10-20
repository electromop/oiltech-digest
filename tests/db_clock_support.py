"""Сторож часов тестовой базы (20.10): прогон с базой не начинается, если её часы прыгают.

Выдача задач сравнивает run_after с now() базы. Откатятся часы базы назад, и только что
поставленная задача окажется «в будущем»: claim вернёт None, тесты поплывут (TypeError
'NoneType', «карточки не записаны»). 20.10 так было в ВМ colima: timesyncd (NTP через VPN)
гнал часы вперёд, lima-guestagent раз в 10 с возвращал их к часам Mac, и now() базы
откатывался на 150...245 мс, а иногда на секунды. Ловить это по плавающим падениям долго;
замер хода часов за секунду видит сразу. Подключает фикстура db_clock_steady (conftest.py).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time

# Здоровые часы расходятся с часами Mac на доли миллисекунды в секунду, 20.10 в ВМ colima
# было около 18 мс/с. Решение принимается по границам погрешности замера, а не по середине:
# медленный ответ базы расширяет границы и делает сторожа мягче, а не пугливее.
MAX_DRIFT_MS_PER_S = 5.0
# Шаг назад меньше миллисекунды не перестановка часов: NTP так не шагает.
MIN_BACKWARD_JUMP_MS = 1.0

# (часы Mac до запроса, часы базы, часы Mac после запроса), секунды эпохи.
Sample = tuple[float, float, float]


@dataclass(frozen=True)
class ClockCheck:
    drift_ms_per_s: float  # оценка ухода часов базы от часов Mac
    drift_low: float  # границы ухода с учётом задержки запросов
    drift_high: float
    backward_jumps_ms: tuple[float, ...]
    span_s: float

    @property
    def steady(self) -> bool:
        drifting = self.drift_low > MAX_DRIFT_MS_PER_S or self.drift_high < -MAX_DRIFT_MS_PER_S
        return not drifting and not self.backward_jumps_ms


def sample(conn) -> Sample:
    """Один замер на соединении с автокоммитом: clock_timestamp(), а не now() транзакции."""
    before = time.time()
    db_time = conn.execute("SELECT extract(epoch FROM clock_timestamp())::float8").fetchone()[0]
    return before, float(db_time), time.time()


def check_clock(read: Callable[[], Sample], *, span: float = 1.0, burst: int = 5, poll: float = 0.02,
                now: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep) -> ClockCheck:
    """Ход часов базы за span секунд: пачка замеров в начале, опрос в середине, пачка в конце."""
    samples = [read() for _ in range(burst)]
    deadline = now() + span
    while now() < deadline:
        sleep(poll)
        samples.append(read())
    samples += [read() for _ in range(burst)]
    return judge(samples, burst=burst)


def judge(samples: list[Sample], *, burst: int) -> ClockCheck:
    """Уход и скачки по замерам. В пачке берётся замер с самым быстрым ответом: у него точнее
    смещение. Скачок назад видно прямо по часам базы, там погрешности замера нет."""
    jumps = tuple(round((a[1] - b[1]) * 1000, 1) for a, b in zip(samples, samples[1:])
                  if (a[1] - b[1]) * 1000 >= MIN_BACKWARD_JUMP_MS)
    first = min(samples[:burst], key=_round_trip)
    last = min(samples[-burst:], key=_round_trip)
    elapsed = _middle(last) - _middle(first)
    if elapsed <= 0:
        return ClockCheck(0.0, 0.0, 0.0, jumps, 0.0)
    # Истинное смещение замера лежит между (база - после) и (база - до).
    low = (last[1] - last[2] - (first[1] - first[0])) * 1000 / elapsed
    high = (last[1] - last[0] - (first[1] - first[2])) * 1000 / elapsed
    middle = (_offset(last) - _offset(first)) * 1000 / elapsed
    return ClockCheck(round(middle, 1), round(low, 1), round(high, 1), jumps, round(elapsed, 2))


def explain(result: ClockCheck) -> str:
    """Текст остановки прогона: что не так, чем грозит, что делать."""
    jumps = (f"скачков назад {len(result.backward_jumps_ms)}, самый большой {max(result.backward_jumps_ms):.0f} мс"
             if result.backward_jumps_ms else "скачков назад нет")
    return (
        f"Часы тестовой базы нестабильны: за {result.span_s:.1f} с уходят от часов Mac на "
        f"{result.drift_ms_per_s:+.1f} мс/с, {jumps}. Выдача задач сравнивает run_after с now() базы, "
        "и тесты будут плавать (claim вернёт None). Обычная причина: в ВМ colima спорят NTP (timesyncd) "
        "и lima-guestagent. Лечение: colima ssh -- sudo timedatectl set-ntp false, затем colima restart "
        "под замком тестов. Запустить прогон всё равно: OILTECH_TEST_ALLOW_CLOCK_DRIFT=1."
    )


def _round_trip(item: Sample) -> float:
    return item[2] - item[0]


def _middle(item: Sample) -> float:
    return (item[0] + item[2]) / 2


def _offset(item: Sample) -> float:
    return item[1] - _middle(item)
