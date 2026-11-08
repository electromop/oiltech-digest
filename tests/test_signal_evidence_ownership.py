"""Ссылка карточки радара не уезжает в чужую карточку (сигнал 97, замечание 22.09).

Прогон 28.09 воспроизвёл это на данных: одна статья нашлась в трёх темах, ревью пачки
сочло её дублем другого события, и карточка, у которой это была единственная ссылка,
осталась без источника со счётчиком «1 ссылка».
"""

from oiltech_digest import signal_discovery
from oiltech_digest.db import repository

INFRA = "Промысловая инфраструктура, трубопроводы, диагностика и целостность"
ENERGY = "Энергетика и промышленные энергосистемы"
URL = "https://energiesmedia.com/slb-2026-digital-oilfield-update-energy/"


def _row(key, title, theme=INFRA):
    return {"signal_key": key, "title": title, "title_ru": title, "theme": theme, "maturity": "watch", "score": 60}


def _finding(key, title, url, theme=INFRA):
    """Находка поиска, как её отдаёт воркер (signal_discovery._finding_candidate): одна ссылка, решения нет."""
    return {"signal": {"title": title, "theme": theme, "summary": "", "companies": [], "maturity": "watch", "score": 0,
                       "signal_key": key, "evidence_count": 1, "evidence": [{"source_url": url, "title": title}]}}


def _apply(topics):
    config = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=False, persist_training_examples=False)
    return signal_discovery.apply_discovery(config, {"topics": topics})


def _urls(signal_id):
    with repository.get_connection() as conn:
        return {row[0] for row in conn.execute("SELECT source_url FROM signal_evidence WHERE signal_id = %s", (signal_id,))}


def _count(signal_id):
    with repository.get_connection() as conn:
        return conn.execute("SELECT evidence_count FROM signals WHERE id = %s", (signal_id,)).fetchone()[0]


def test_link_of_visible_card_does_not_move_to_another_card(isolated_db):
    owner = repository.upsert_signal(_row("owner", "SLB: цифровые инструменты на месторождениях"))
    other = repository.upsert_signal(_row("other", "MoU SLB и PDVSA", ENERGY))
    repository.upsert_signal_evidence(owner, {"source_url": URL, "title": "SLB digital update"})

    repository.upsert_signal_evidence(other, {"source_url": URL, "title": "SLB digital update (refetched)"})

    assert _urls(owner) == {URL}
    assert _urls(other) == set()


def test_link_of_hidden_duplicate_still_moves_to_main_card(isolated_db):
    # Звезда дедупа: ссылки скрытого дубля собираются в главной — это не ломаем.
    main = repository.upsert_signal(_row("main", "SLB: цифровые инструменты"))
    hidden = repository.upsert_signal(_row("hidden", "SLB цифровое обновление"))
    repository.upsert_signal_evidence(hidden, {"source_url": URL, "title": "t"})
    assert repository.mark_signal_merged(hidden, main, "то же событие")

    repository.upsert_signal_evidence(main, {"source_url": URL, "title": "t"})

    assert _urls(main) == {URL}


def test_finding_glued_to_a_wrong_card_does_not_empty_the_card_that_owns_the_link(isolated_db):
    # Сценарий 28.09 на пути находки: «Дедуп» склеил находку с другим событием того же вендора (MoU SLB и PDVSA),
    # а её ссылка уже у видимой карточки. Ссылка остаётся у хозяйки, чужая карточка её не получает.
    owner = repository.upsert_signal(_row("infra", "SLB: цифровые инструменты"))
    repository.upsert_signal_evidence(owner, {"source_url": URL, "title": "SLB digital update"})
    repository.refresh_signal_evidence_count(owner)
    mou = repository.upsert_signal(_row("mou", "MoU SLB и PDVSA", ENERGY))
    repository.upsert_signal_evidence(mou, {"source_url": "https://slb.com/pdvsa-mou", "title": "MoU"})

    _apply([{"topic": ENERGY, "candidates": [
        {**_finding("energy-dup", "SLB digital update", URL, ENERGY),
         "duplicate_of": {"signal_id": mou}, "duplicate_reason": "тот же вендор"},
    ]}])

    assert _urls(owner) == {URL}
    assert _count(owner) == 1
    assert _urls(mou) == {"https://slb.com/pdvsa-mou"}


def test_finding_whose_link_belongs_to_another_card_does_not_create_an_empty_card(isolated_db):
    # Тот же материал найден снова, а ссылка уже у видимой карточки: отмечается она, своей находки нет.
    owner = repository.upsert_signal(_row("owner", "SLB: цифровые инструменты"))
    repository.upsert_signal_evidence(owner, {"source_url": URL, "title": "t"})
    repository.refresh_signal_evidence_count(owner)

    result = _apply([{"topic": ENERGY, "candidates": [_finding("new-key", "SLB цифровое обновление", URL, ENERGY)]}])

    assert repository.signal_key_owners(["new-key"]) == {}
    assert _urls(owner) == {URL}
    [found] = result["topic_results"][0]["signals"]
    assert (found["id"], found["found_again"]) == (owner, True)


def test_finding_with_the_key_of_a_stored_card_is_only_marked_found_again(isolated_db):
    # Та же ссылка даёт тот же ключ: решённое второй раз не решается, ссылки карточки не меняются.
    owner = repository.upsert_signal(_row("owner", "SLB: цифровые инструменты"))
    repository.upsert_signal_evidence(owner, {"source_url": URL, "title": "t"})

    result = _apply([{"topic": INFRA, "candidates": [_finding("owner", "SLB: цифровые инструменты", URL)]}])

    assert _urls(owner) == {URL}
    assert repository.signal_key_owners(["owner"])["owner"]["id"] == owner
    [found] = result["topic_results"][0]["signals"]
    assert (found["id"], found["found_again"]) == (owner, True)


def test_refresh_all_counts_fixes_cards_left_with_stale_number(isolated_db):
    emptied = repository.upsert_signal(_row("emptied", "Карточка, чья ссылка уехала"))
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET evidence_count = 1 WHERE id = %s", (emptied,))
        conn.commit()

    assert repository.refresh_all_signal_evidence_counts() == 1
    assert _count(emptied) == 0
    assert repository.refresh_all_signal_evidence_counts() == 0
