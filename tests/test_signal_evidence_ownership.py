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


def _candidate(key, title, urls, theme=INFRA):
    signal = signal_discovery._normalize_signal_payload(
        {"title": title, "title_ru": title, "summary": title, "maturity": "shortlist", "score": 70, "companies": ["SLB"]},
        theme,
    )
    signal.update({"signal_key": key, "evidence_count": len(urls),
                   "evidence": [{"source_url": url, "title": title} for url in urls]})
    return signal


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


def test_wrong_batch_duplicate_does_not_empty_the_card_that_owns_the_link(isolated_db):
    # Ровно сценарий 28.09: карточка темы «инфраструктура» записана первой, в теме
    # «энергетика» тот же материал помечен дублем другого события (MoU SLB—PDVSA).
    run = [
        {"topic": INFRA, "candidates": [{"signal": _candidate("infra", "SLB: цифровые инструменты", [URL]), "rejected": False}]},
        {"topic": ENERGY, "candidates": [
            {"signal": _candidate("mou", "MoU SLB и PDVSA", ["https://slb.com/pdvsa-mou"], ENERGY), "rejected": False},
            {"signal": _candidate("energy-dup", "SLB digital update", [URL], ENERGY), "rejected": False,
             "duplicate_of": {"signal_key": "mou"}, "duplicate_reason": "тот же вендор"},
        ]},
    ]

    _apply(run)

    infra_id = repository.signal_key_owners(["infra"])["infra"]["id"]
    mou_id = repository.signal_key_owners(["mou"])["mou"]["id"]
    assert _urls(infra_id) == {URL}
    assert _count(infra_id) == 1
    assert _urls(mou_id) == {"https://slb.com/pdvsa-mou"}


def test_candidate_whose_links_all_belong_to_another_card_does_not_create_an_empty_card(isolated_db):
    # Тот же материал найден снова с новым ключом (fact:-ключ зависит от текста модели).
    owner = repository.upsert_signal(_row("owner", "SLB: цифровые инструменты"))
    repository.upsert_signal_evidence(owner, {"source_url": URL, "title": "t"})
    repository.refresh_signal_evidence_count(owner)

    result = _apply([{"topic": ENERGY, "candidates": [
        {"signal": _candidate("new-key", "SLB цифровое обновление", [URL], ENERGY), "rejected": False},
    ]}])

    assert repository.signal_key_owners(["new-key"]) == {}
    assert _urls(owner) == {URL}
    assert result["signals"][0]["id"] == owner


def test_candidate_with_some_new_links_keeps_only_its_own(isolated_db):
    owner = repository.upsert_signal(_row("owner", "SLB: цифровые инструменты"))
    repository.upsert_signal_evidence(owner, {"source_url": URL, "title": "t"})

    _apply([{"topic": ENERGY, "candidates": [
        {"signal": _candidate("fresh", "SLB расширяет автономные операции", [URL, "https://slb.com/autonomy"], ENERGY),
         "rejected": False},
    ]}])

    fresh_id = repository.signal_key_owners(["fresh"])["fresh"]["id"]
    assert _urls(owner) == {URL}
    assert _urls(fresh_id) == {"https://slb.com/autonomy"}
    assert _count(fresh_id) == 1


def test_same_card_refound_updates_its_own_links(isolated_db):
    owner = repository.upsert_signal(_row("owner", "SLB: цифровые инструменты"))
    repository.upsert_signal_evidence(owner, {"source_url": URL, "title": "t"})

    _apply([{"topic": INFRA, "candidates": [
        {"signal": _candidate("owner", "SLB: цифровые инструменты", [URL]), "rejected": False},
    ]}])

    assert _urls(owner) == {URL}
    assert repository.signal_key_owners(["owner"])["owner"]["id"] == owner


def test_refresh_all_counts_fixes_cards_left_with_stale_number(isolated_db):
    emptied = repository.upsert_signal(_row("emptied", "Карточка, чья ссылка уехала"))
    with repository.get_connection() as conn:
        conn.execute("UPDATE signals SET evidence_count = 1 WHERE id = %s", (emptied,))
        conn.commit()

    assert repository.refresh_all_signal_evidence_counts() == 1
    assert _count(emptied) == 0
    assert repository.refresh_all_signal_evidence_counts() == 0
