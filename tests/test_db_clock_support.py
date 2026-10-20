"""Сторож часов тестовой базы (db_clock_support): когда пропускает прогон, когда останавливает."""

import pytest

from tests import db_clock_support as clock

T0 = 1_792_520_000.0  # 20.10.2026, секунды эпохи


def samples(*, drift_ms_per_s=0.0, jump_after=None, jump_ms=0.0, count=60, step=0.02, round_trip=0.001):
    """Замеры раз в step секунд: часы базы уходят на drift_ms_per_s, после замера jump_after
    откатываются на jump_ms."""
    out = []
    for i in range(count):
        host = T0 + i * step
        db_time = host + drift_ms_per_s / 1000 * (host - T0)
        if jump_after is not None and i > jump_after:
            db_time -= jump_ms / 1000
        out.append((host - round_trip / 2, db_time, host + round_trip / 2))
    return out


def test_steady_clock_lets_the_run_start():
    result = clock.judge(samples(), burst=5)
    assert (result.steady, result.drift_ms_per_s, result.backward_jumps_ms) == (True, 0.0, ())


def test_clock_running_fast_as_in_colima_on_20_10_stops_the_run():
    result = clock.judge(samples(drift_ms_per_s=18), burst=5)
    assert not result.steady
    assert result.drift_ms_per_s == pytest.approx(18, abs=0.1)


def test_step_back_as_lima_guestagent_made_stops_the_run():
    # Откат на 180 мс между замерами с шагом 20 мс виден как шаг назад на 160 мс.
    result = clock.judge(samples(jump_after=30, jump_ms=180), burst=5)
    assert not result.steady
    assert result.backward_jumps_ms == (pytest.approx(160, abs=0.1),)


def test_slow_answers_widen_the_bounds_instead_of_raising_a_false_alarm():
    # Под нагрузкой ответ базы шёл 30 мс, и момент чтения часов внутри ответа неизвестен: в
    # первой пачке база прочла часы в конце ответа, в последней в начале. Середина замера
    # сдвинута на ±12 мс, часы при этом ровные: тревоги нет.
    slow = samples(round_trip=0.030)
    shifted = [(before, db_time + (0.012 if i < 5 else -0.012 if i >= len(slow) - 5 else 0.0), after)
               for i, (before, db_time, after) in enumerate(slow)]
    result = clock.judge(shifted, burst=5)
    assert result.steady
    assert result.drift_ms_per_s < -clock.MAX_DRIFT_MS_PER_S  # середина врёт, решают границы


def test_check_clock_polls_for_the_whole_span_and_sees_a_step_in_the_middle():
    fake = {"t": T0, "skew": 0.0}

    def read():
        fake["t"] += 0.001
        if fake["t"] > T0 + 0.5:
            fake["skew"] = -0.18
        return fake["t"] - 0.0005, fake["t"] + fake["skew"], fake["t"] + 0.0005

    def sleep(seconds):
        fake["t"] += seconds

    result = clock.check_clock(read, span=1.0, burst=5, poll=0.02, now=lambda: fake["t"], sleep=sleep)
    assert result.span_s == pytest.approx(1.0, abs=0.1)
    assert (result.steady, len(result.backward_jumps_ms)) == (False, 1)
