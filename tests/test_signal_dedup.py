"""Дедуп радара: одно событие — одна карточка (перенос механизма перепечаток MVP-1).

Данные — с прода 18.09: четыре карточки про покупку Velocity Geomatics компанией
ZenaTech, две — про завод сорбентов «Татнефти», пять обзоров лития из пластовых вод."""

import json

import pytest

from oiltech_digest import signal_dedup, signal_discovery
from oiltech_digest.db import repository
from oiltech_digest.db import marks
from oiltech_digest.processing.openai_client import AIResponse

GEO = "Геологоразведка, сейсморазведка, геофизика и изучение недр"
MARKET = "Рынок, бизнес-модели, сервисные модели, партнёрства и M&A"


def _existing(signal_id, title, companies, *, verdict=None, fresh=True, score=60, urls=()):
    return {
        "kind": "existing", "id": signal_id, "reviewed": bool(verdict), "verdict": verdict,
        "fresh": fresh, "urls": list(urls),
        "signal": {"id": signal_id, "title_ru": title, "companies": companies, "score": score,
                   "evidence_count": len(urls) or 1, "signal_key": f"key-{signal_id}"},
    }


def _new(title, companies, *, score=60, key="k", order=1, urls=()):
    return {
        "kind": "new", "reviewed": False, "fresh": True, "order": order, "urls": list(urls),
        "signal": {"title_ru": title, "companies": companies, "score": score,
                   "evidence_count": len(urls) or 1, "signal_key": key},
    }


class _Judge:
    """Судья-заглушка: «одно событие», если обе карточки содержат маркер."""

    def __init__(self, marker, *, fail_on=None):
        self.marker = marker
        self.fail_on = fail_on
        self.prompts = []

    def complete_json(self, instructions, prompt, schema, max_output_tokens=900, **kwargs):
        assert schema["name"] == "signal_duplicate_verdict"
        self.prompts.append(prompt)
        if self.fail_on and self.fail_on in prompt:
            raise RuntimeError("OpenAI 500")
        card_a, card_b = prompt.split("Карточка B")
        same = self.marker in card_a and self.marker in card_b
        return AIResponse(data={"same_event": same, "reason": "одна сделка" if same else "разные факты"},
                          model="gpt-5-mini", input_tokens=500, output_tokens=40)


def test_pairs_found_by_title_or_company_but_never_between_two_reviewed_cards():
    nodes = [
        _existing(18, "Acquisition of Velocity Geomatics by ZenaTech", ["ZenaTech", "Velocity Geomatics (Velocity Group)"],
                  verdict="approved", fresh=False),
        _existing(19, "ZenaTech покупает Velocity Geomatics", ["ZenaTech"], verdict="reject", fresh=False),
        _existing(40, "Приобретение Velocity Geomatics компанией ZenaTech — DaaS", ["ZenaTech", "Velocity Geomatics"]),
        # Старые неразобранные между собой не сверяются: иначе пара оплачивалась бы каждый день.
        _existing(7, "Литий из пластовых вод: перспективы", [], fresh=False),
        _existing(8, "Перспективы лития из пластовых вод", [], fresh=False),
        # Издатель вместо компании — не общий участник.
        _existing(50, "Беспилотники над трубопроводом", ["bigchallenges.ru (источник)"]),
        _new("Дроны на буровой площадке", ["bigchallenges.ru (источник)"], key="drone"),
    ]
    pairs = {(nodes[i]["id"] if "id" in nodes[i] else "new", nodes[j].get("id", "new")) for i, j, _ in signal_dedup.find_pairs(nodes)}

    assert (18, 40) in pairs and (19, 40) in pairs
    assert (18, 19) not in pairs  # оба разобраны экспертом
    assert (7, 8) not in pairs  # оба старые и неразобранные
    assert (50, "new") not in pairs  # общий только издатель


def test_star_not_chain_and_reviewed_card_is_never_hidden():
    nodes = [
        _existing(18, "A", ["ZenaTech"], verdict="approved", fresh=False, score=30),
        _existing(40, "B", ["ZenaTech"], score=70),
        _existing(41, "C", ["ZenaTech"], score=60),
        _existing(26, "D", ["Татнефть"], verdict="reject", fresh=False),
        _new("E", ["Татнефть"], key="e"),
    ]
    edges = [(0, 1, "та же сделка"), (1, 2, "та же сделка"), (3, 4, "тот же завод"), (0, 3, "ошибка судьи")]

    assigned = signal_dedup.assign_duplicates(nodes, edges)

    # Одобренная — главная, даже с меньшим баллом; 41 связана с ней только через 40 — не склеена.
    assert assigned[1] == (0, "та же сделка")
    assert 2 not in assigned
    # Отклонённая экспертом остаётся на месте, свежий повтор уходит в неё.
    assert 3 not in assigned
    assert assigned[4] == (3, "тот же завод")


def test_judge_failure_leaves_pair_unmerged_and_run_goes_on():
    nodes = [
        _new("ZenaTech покупает Velocity Geomatics", ["ZenaTech"], key="a", order=1),
        _new("Velocity Geomatics куплена ZenaTech", ["ZenaTech"], key="b", order=2),
        _new("Татнефть строит завод сорбентов", ["Татнефть"], key="c", order=3),
        _new("Завод сорбентов Татнефти", ["Татнефть"], key="d", order=4),
    ]
    judge = _Judge("ZenaTech", fail_on="Татнефть строит")
    made = []

    result = signal_dedup.dedupe(nodes, client_factory=lambda: made.append(1) or judge)

    assert made == [1]
    assert result["stats"]["errors"] == 1 and result["stats"]["same"] == 1
    assert set(result["assigned"]) == {1}  # Татнефть без решения судьи не склеена
    assert result["stats"]["cost_usd"] > 0


def test_no_pairs_no_client():
    made = []
    result = signal_dedup.dedupe([_new("Одна карточка", ["X"])], client_factory=lambda: made.append(1))
    assert made == [] and result["stats"]["pairs"] == 0


def _candidate(title, companies, *, key, url):
    """Находка поиска, как её отдаёт воркер (_finding_candidate): в сигнале заголовок страницы и ссылка, черновые
    данные модели поиска (русский заголовок, компания) лежат в ссылке, их видит «Дедуп»."""
    return _otbor_finding(key, url, title, title, ", ".join(companies))["signal"]


def test_worker_marks_repeats_of_existing_and_in_run_duplicates(monkeypatch):
    existing = [{"id": 18, "signal_key": "old", "title": "Acquisition of Velocity Geomatics by ZenaTech",
                 "title_ru": "Покупка Velocity Geomatics компанией ZenaTech", "theme": MARKET,
                 "summary": "", "companies": ["ZenaTech"], "score": 30.0, "evidence_count": 1,
                 "fresh": False, "verdict": "approved", "evidence_urls": ["https://old.example/zena"]}]
    snapshot = {"topics": [{"name": GEO}, {"name": MARKET}], "known_urls": [], "existing_signals": existing}
    found = {
        GEO: [_candidate("ZenaTech покупает Velocity Geomatics", ["ZenaTech"], key="z1", url="https://a.example/1"),
              _candidate("Татнефть строит завод сорбентов лития", ["Татнефть"], key="t1", url="https://a.example/2")],
        MARKET: [_candidate("Завод сорбентов Татнефти для лития", ["Татнефть"], key="t2", url="https://b.example/3")],
    }
    monkeypatch.setattr(signal_discovery, "_topic_web_evidence", lambda topic, topic_name, config, **kwargs: {
        "status": "ok", "provider": "openai_web_search", "research": {},
        "evidence": [item for signal in found[topic_name] for item in signal["evidence"]]})
    judge = _Judge("ZenaTech")
    judge_tatneft = _Judge("сорбент")

    class _Both:
        def complete_json(self, *args, **kwargs):
            first = judge.complete_json(*args, **kwargs)
            return first if first.data["same_event"] else judge_tatneft.complete_json(*args, **kwargs)

    monkeypatch.setattr(signal_discovery, "make_client", lambda offline: _Both())
    config = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=False)

    run = signal_discovery.run_discovery(config, snapshot)

    json.dumps(run)  # уходит ядру по HTTP
    geo, market = run["topics"]
    assert geo["candidates"][0]["duplicate_of"] == {"signal_id": 18}  # повтор одобренного
    assert "duplicate_of" not in geo["candidates"][1]  # главная Татнефти найдена первой
    assert market["candidates"][0]["duplicate_of"] == {"signal_key": geo["candidates"][1]["signal"]["signal_key"]}
    assert run["dedup"]["duplicates_new"] == 2 and run["dedup"]["existing_merges"] == []


def test_worker_offline_skips_dedup():
    config = signal_discovery.SignalDiscoveryConfig(offline=True)
    run = signal_discovery.run_discovery(config, {"topics": [], "existing_signals": []})
    assert run["dedup"] == {"skipped": "offline"}


def test_core_writes_duplicate_links_into_main_card_and_hides_old_repeats(isolated_db):
    zena = repository.upsert_signal(_signal_row("zena", "Покупка Velocity Geomatics"))
    repository.upsert_signal_evidence(zena, {"source_url": "https://old.example/zena", "title": "t"})
    deal = repository.upsert_signal(_signal_row("deal", "ZenaTech: сделка с Velocity"))
    repeat_card = repository.upsert_signal(_signal_row("deal-repeat", "Сделка ZenaTech и Velocity"))
    main = _candidate("Татнефть строит завод сорбентов", ["Татнефть"], key="t1", url="https://a.example/2")
    repeat = _candidate("Завод сорбентов Татнефти", ["Татнефть"], key="t2", url="https://b.example/3")
    old = _candidate("ZenaTech покупает Velocity", ["ZenaTech"], key="z1", url="https://a.example/1")
    run = {
        "topics": [
            # Дубль стоит РАНЬШЕ своей главной карточки — порядок записи не должен это ломать.
            {"topic": MARKET, "candidates": [
                {"signal": repeat, "duplicate_of": {"signal_key": "t1"}, "duplicate_reason": "тот же завод"},
                {"signal": old, "duplicate_of": {"signal_id": zena}, "duplicate_reason": "та же сделка"},
            ]},
            {"topic": GEO, "candidates": [{"signal": main}]},
        ],
        "dedup": {"pairs": 3, "judged": 3, "same": 2,
                  "existing_merges": [{"signal_id": repeat_card, "into": deal, "reason": "та же сделка"}]},
    }

    result = _apply(run)

    owners = repository.signal_key_owners(["t1", "t2", "z1"])
    assert set(owners) == {"t1"}  # новой карточки у дублей нет: главная стала находкой для «Отбора»
    assert _urls(owners["t1"]["id"]) == {"https://a.example/2", "https://b.example/3"}
    assert "https://a.example/1" in _urls(zena)
    assert repository.resolve_signal_merge_root(repeat_card) == deal
    assert result["dedup"]["merged_new"] == 2 and result["dedup"]["merged_existing"] == 1
    assert result["all_signals"] == 1 and result["topic_results"][0]["duplicates"] == 2


def test_core_keeps_signal_when_main_card_is_gone(isolated_db):
    repeat = _candidate("ZenaTech покупает Velocity", ["ZenaTech"], key="z1", url="https://a.example/1")
    run = {"topics": [{"topic": GEO, "candidates": [{"signal": repeat, "duplicate_of": {"signal_id": 999}}]}],
           "dedup": {"pairs": 1}}

    result = _apply(run)

    # Главной нет: находка идёт своей карточкой, её решит «Отбор».
    assert set(repository.signal_key_owners(["z1"])) == {"z1"} and result["all_signals"] == 1


def _signal_row(key, title):
    return {"signal_key": key, "title": title, "title_ru": title, "theme": GEO, "maturity": "watch",
            "score": 60, "companies": ["ZenaTech"]}


def test_merge_hides_repeat_counts_it_and_spares_reviewed(isolated_db):
    main = repository.upsert_signal(_signal_row("main", "Покупка Velocity Geomatics"))
    repeat = repository.upsert_signal(_signal_row("repeat", "ZenaTech купила Velocity Geomatics"))
    reviewed = repository.upsert_signal(_signal_row("reviewed", "ZenaTech и Velocity"))
    later = repository.upsert_signal(_signal_row("later", "Сделка ZenaTech"))
    repository.upsert_signal_evidence(repeat, {"source_url": "https://a.example/1", "title": "t"})
    # Карточка без единой ссылки в список не попадает (сигнал 97) — у разобранной ссылка есть.
    repository.upsert_signal_evidence(reviewed, {"source_url": "https://a.example/reviewed", "title": "t"})
    with repository.get_connection() as conn:
        conn.execute("INSERT INTO signal_feedback_events (signal_id, event_type, verdict) VALUES (%s, 'verdict', 'approved')",
                     (reviewed,))
        conn.commit()

    assert repository.mark_signal_merged(repeat, main, "та же сделка") is True
    assert repository.mark_signal_merged(repeat, main) is False  # уже скрыта
    assert repository.mark_signal_merged(reviewed, main) is False  # вердикт эксперта — не прячем
    assert repository.mark_signal_merged(main, repeat) is False  # в свою же группу не уходит
    # Главная, указанная через скрытую карточку, — корень группы.
    assert repository.mark_signal_merged(later, repeat) is True
    assert repository.resolve_signal_merge_root(later) == main

    visible = {row["id"]: row for row in repository.list_signals(limit=10)}
    assert set(visible) == {main, reviewed}
    assert visible[main]["merged_count"] == 2

    snapshot = {row["id"]: row for row in repository.list_signals_for_dedup()}
    assert set(snapshot) == {main, reviewed}
    assert snapshot[reviewed]["verdict"] == "approved" and snapshot[main]["fresh"] is True
    json.dumps(snapshot, default=str)


# --- Регрессии ревью 18.09: повторная находка, ссылки скрытых дублей, чужая ссылка, дайджест ---


def _apply(run):
    config = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=False, persist_training_examples=False)
    return signal_discovery.apply_discovery(config, run)


def _verdict(signal_id, verdict="approved"):
    with repository.get_connection() as conn:
        conn.execute("INSERT INTO signal_feedback_events (signal_id, event_type, verdict) VALUES (%s, 'verdict', %s)",
                     (signal_id, verdict))
        conn.commit()


def _urls(signal_id):
    return {row["source_url"] for row in repository.list_signal_evidence(signal_id)}


def test_refound_link_of_hidden_card_goes_to_main_card_not_into_hidden_row(isolated_db):
    main = repository.upsert_signal(_signal_row("main", "Покупка Velocity Geomatics"))
    hidden = repository.upsert_signal(_signal_row("hidden", "ZenaTech купила Velocity"))
    repository.upsert_signal_evidence(hidden, {"source_url": "https://a.example/1", "title": "t"})
    assert repository.mark_signal_merged(hidden, main, "та же сделка")
    again = _candidate("ZenaTech купила Velocity Geomatics", ["ZenaTech"], key="hidden", url="https://a.example/9")

    # Результат без дедупа (старая сборка воркера, офлайн, судья ошибся) — ключ скрытой карточки.
    result = _apply({"topics": [{"topic": GEO, "candidates": [{"signal": again}]}]})

    assert {row["id"] for row in repository.list_signals(limit=10)} == {main}
    assert {"https://a.example/1", "https://a.example/9"} <= _urls(main)
    assert result["all_signals"] == 0 and result["dedup"]["merged_new"] == 1


def test_hidden_duplicate_links_show_in_main_card_and_count_as_reviewed(isolated_db):
    main = repository.upsert_signal(_signal_row("main", "Покупка Velocity Geomatics"))
    dup = repository.upsert_signal(_signal_row("dup", "ZenaTech купила Velocity"))
    repository.upsert_signal_evidence(main, {"source_url": "https://m.example/1", "title": "t"})
    for url in ("https://a.example/1", "https://a.example/2"):
        repository.upsert_signal_evidence(dup, {"source_url": url, "title": "t"})
    assert repository.mark_signal_merged(dup, main)

    assert _urls(main) == {"https://m.example/1", "https://a.example/1", "https://a.example/2"}
    _verdict(main)
    assert {"https://a.example/1", "https://a.example/2"} <= set(repository.list_reviewed_signal_urls())


def test_duplicate_carrying_key_of_visible_card_hides_that_card_too(isolated_db):
    target = repository.upsert_signal(_signal_row("target", "Покупка Velocity Geomatics"))
    _verdict(target)
    owner = repository.upsert_signal(_signal_row("owner", "ZenaTech купила Velocity"))
    repository.upsert_signal_evidence(owner, {"source_url": "https://a.example/u", "title": "t"})
    again = _candidate("ZenaTech купила Velocity", ["ZenaTech"], key="owner", url="https://a.example/u")

    _apply({"topics": [{"topic": GEO, "candidates": [
        {"signal": again, "duplicate_of": {"signal_id": target}, "duplicate_reason": "та же сделка"}]}],
        "dedup": {"pairs": 1}})

    # Карточка-владелец ссылки не остаётся видимой пустышкой: она тот же материал.
    assert {row["id"] for row in repository.list_signals(limit=10)} == {target}
    assert "https://a.example/u" in _urls(target)


def test_duplicate_carrying_key_of_reviewed_card_updates_that_card(isolated_db):
    target = repository.upsert_signal(_signal_row("target", "Покупка Velocity Geomatics"))
    owner = repository.upsert_signal(_signal_row("owner", "ZenaTech купила Velocity"))
    repository.upsert_signal_evidence(target, {"source_url": "https://a.example/target", "title": "t"})
    repository.upsert_signal_evidence(owner, {"source_url": "https://a.example/u", "title": "t"})
    _verdict(owner, "too_generic")
    again = _candidate("ZenaTech купила Velocity", ["ZenaTech"], key="owner", url="https://a.example/u")

    result = _apply({"topics": [{"topic": GEO, "candidates": [
        {"signal": again, "duplicate_of": {"signal_id": target}}]}], "dedup": {"pairs": 1}})

    # Разобранная остаётся собой и при ссылке — судья не переспорит разбор.
    assert {row["id"] for row in repository.list_signals(limit=10)} == {target, owner}
    assert _urls(owner) == {"https://a.example/u"} and "https://a.example/u" not in _urls(target)
    assert result["all_signals"] == 1


def test_card_selected_for_digest_is_never_hidden(isolated_db):
    user = repository.create_user("digest-editor@example.com", "password123", role="admin")
    main = repository.upsert_signal(_signal_row("main", "Покупка Velocity Geomatics"))
    chosen = repository.upsert_signal(_signal_row("chosen", "ZenaTech купила Velocity"))
    marks.set_signal_mark(chosen, status="digest", user_id=int(user["id"]))

    assert repository.mark_signal_merged(chosen, main) is False
    snapshot = {row["id"]: row for row in repository.list_signals_for_dedup()}
    assert snapshot[chosen]["reviewed"] is True and snapshot[main]["reviewed"] is False


def test_shared_company_pair_is_judged_before_title_only_pairs():
    """19.09: при потолке пар английский заголовок одобренной ZenaTech против русского
    дал почти ноль общих основ, и пара уходила в хвост под срез."""
    nodes = [
        _existing(18, "Acquisition of Velocity Geomatics by ZenaTech accelerates geomatics", ["ZenaTech"],
                  verdict="approved", fresh=False),
        _existing(60, "Татнефть строит завод сорбентов для извлечения лития из попутных вод", ["Татнефть"]),
        _new("Приобретение компании ZenaTech усиливает сервис беспилотников", ["ZenaTech"], key="zena"),
        _new("Строит завод сорбентов для извлечения лития из попутных вод", [], key="lith"),
    ]

    order = [(nodes[i].get("id", nodes[i]["signal"]["signal_key"]), nodes[j].get("id", nodes[j]["signal"]["signal_key"]))
             for i, j, _ in signal_dedup.find_pairs(nodes)]

    assert order[0] == (18, "zena")
    assert (60, "lith") in order


def test_core_sets_dedup_cap_for_worker(monkeypatch):
    monkeypatch.setattr(signal_discovery.app_config, "SIGNAL_DEDUP_MAX_PAIRS", 7)
    monkeypatch.setattr(repository, "list_reviewed_signal_urls", lambda: [])
    monkeypatch.setattr(repository, "list_signals_for_dedup", lambda: [])
    monkeypatch.setattr(repository, "list_feed_signals_for_dedup", lambda **kwargs: [])
    monkeypatch.setattr(repository, "collected_url_keys_since", lambda **kwargs: [])
    monkeypatch.setattr(repository, "covered_collection_sites", lambda **kwargs: [])
    monkeypatch.setattr(signal_discovery, "_selected_topics", lambda topic: [{"name": GEO}])
    snapshot = signal_discovery.build_discovery_snapshot(signal_discovery.SignalDiscoveryConfig())
    assert snapshot["dedup_max_pairs"] == 7

    seen = {}
    monkeypatch.setattr(signal_dedup, "dedupe", lambda nodes, **kwargs: seen.update(kwargs) or {"assigned": {}, "stats": {}})
    config = signal_discovery.SignalDiscoveryConfig(offline=False)
    signal_discovery._dedupe_run(config, {"existing_signals": [], "dedup_max_pairs": 7}, [], lambda: None)
    assert seen["max_pairs"] == 7


def test_earlier_unreviewed_card_stays_main_even_with_fewer_links():
    """19.09: вчерашняя №55 FleetRabbit ушла в сегодняшнюю №99 — у той было на одну
    ссылку больше, и номер карточки, на который ссылается эксперт, сменился."""
    older = _existing(55, "FleetRabbit: диспетчеризация", ["FleetRabbit"], score=55, urls=["u1"])
    newer = _existing(99, "FleetRabbit: оптимизация диспетчеризации", ["FleetRabbit"], score=55, urls=["u2", "u3"])
    older["signal"]["first_seen_day"] = "2026-09-18"
    newer["signal"]["first_seen_day"] = "2026-09-19"

    assigned = signal_dedup.assign_duplicates([older, newer], [(0, 1, "одна платформа")])

    assert assigned == {1: (0, "одна платформа")}


def test_company_written_as_list_pairs_with_single_company():
    """№107 «EDF / MethaneSAT» и №3 «MethaneSAT» — одно событие, но пара не находилась:
    строка «edf / methanesat» не равна «methanesat» (20.09)."""
    nodes = [
        _existing(3, "Спутник MethaneSAT потерян на орбите", ["MethaneSAT"]),
        _new("Потеря связи со спутником мониторинга метана", ["EDF / MethaneSAT"], key="k107"),
    ]

    assert signal_dedup.company_keys(nodes[1]["signal"]) == {"edf", "methanesat"}
    assert [(i, j) for i, j, _ in signal_dedup.find_pairs(nodes)] == [(0, 1)]


# --- Ревью #89 (02.10): скрытая качеством или архивом карточка не забирает новую находку ---------


def _hidden(node):
    return {**node, "hidden": True}


def test_hidden_card_does_not_absorb_a_fresh_finding_unless_it_was_rejected():
    nodes = [
        _hidden(_existing(5, "A", ["ZenaTech"], fresh=False, score=80)),
        _new("A2", ["ZenaTech"], key="a2", order=1),
        _hidden(_existing(6, "B", ["Татнефть"], verdict="reject", fresh=False)),
        _new("B2", ["Татнефть"], key="b2", order=2),
        _hidden(_existing(7, "C", ["Shell"], verdict="approved", fresh=False)),
        _new("C2", ["Shell"], key="c2", order=3),
    ]
    edges = [(0, 1, "та же сделка"), (2, 3, "тот же завод"), (4, 5, "тот же пилот")]

    assigned = signal_dedup.assign_duplicates(nodes, edges)

    # Скрытая без отказа — не главная: свежая находка останется своей карточкой на экране. Сама
    # скрытая числится дублем новой, но склейку «сохранённая → новая» ядро не пишет (_dedupe_run).
    assert 1 not in assigned
    assert assigned[0] == (1, "та же сделка")
    # Отклонённая экспертом — главная, хоть и скрыта: её повтор не возвращается.
    assert assigned[3] == (2, "тот же завод")
    # Одобренная, но убранная в архив, свежую не поглощает и сама дублем не становится.
    assert 4 not in assigned and 5 not in assigned


def test_hidden_unreviewed_card_does_not_stay_main_over_a_visible_one():
    nodes = [
        _hidden(_existing(3, "A", ["ZenaTech"], fresh=False)),  # раньше по дате — была бы главной
        _existing(9, "A2", ["ZenaTech"]),
    ]
    nodes[0]["signal"]["first_seen_day"] = "2026-09-13"
    nodes[1]["signal"]["first_seen_day"] = "2026-09-30"

    assert signal_dedup.assign_duplicates(nodes, [(0, 1, "та же сделка")]) == {0: (1, "та же сделка")}


def test_dedup_snapshot_marks_cards_hidden_by_quality_or_archive(isolated_db):
    visible = repository.upsert_signal(_signal_row("visible", "Покупка Velocity Geomatics"))
    # Бизнес-находка видна в витрине «бизнес» (#95) — не скрытая; «другое» не видно нигде.
    business = repository.upsert_signal({**_signal_row("business", "Сделка ZenaTech"), "signal_category": "business"})
    other = repository.upsert_signal({**_signal_row("other", "Отчёт ZenaTech"), "signal_category": "other"})
    archived = repository.upsert_signal(_signal_row("archived", "ZenaTech и Velocity"))
    for signal_id in (visible, business, other, archived):
        repository.upsert_signal_evidence(signal_id, {"source_url": f"https://a.example/{signal_id}", "title": "t"})
    repository.archive_signals([archived], reason="test")

    snapshot = {row["id"]: row["hidden"] for row in repository.list_signals_for_dedup()}

    assert snapshot == {visible: False, business: False, other: True, archived: True}


def test_worker_takes_hidden_flag_from_the_core_snapshot(monkeypatch):
    seen = {}

    def _dedupe(nodes, **kwargs):
        seen["nodes"] = nodes
        return {"assigned": {}, "stats": {"judged": 0, "input_tokens": 0, "output_tokens": 0}}

    monkeypatch.setattr(signal_dedup, "dedupe", _dedupe)
    config = signal_discovery.SignalDiscoveryConfig(offline=False, dry_run=True)
    rows = [{"id": 1, "signal_key": "a", "hidden": True}, {"id": 2, "signal_key": "b"}]

    signal_discovery._dedupe_run(config, {"existing_signals": rows}, [], lambda: None)

    assert [node["hidden"] for node in seen["nodes"]] == [True, False]


# Бизнес-карточку видно в витрине «бизнес» (#95): найденная снова её ссылка — её повтор
# (test_signal_unified_store), а не новая карточка. «Другое» не видно нигде.
@pytest.mark.parametrize("hide", ["archive", "other"])
def test_refound_link_of_a_hidden_unreviewed_card_makes_a_new_finding_for_otbor(isolated_db, hide):
    hidden = repository.upsert_signal(_signal_row("hidden", "Velocity Geomatics: первые данные"))
    repository.upsert_signal_evidence(hidden, {"source_url": "https://a.example/1", "title": "t"})
    assert repository.refresh_signal_evidence_count(hidden) == 1
    if hide == "archive":
        repository.archive_signals([hidden], reason="early-free-theme-2026-09")
    else:
        repository.upsert_signal({**_signal_row("hidden", "Velocity Geomatics: первые данные"),
                                  "signal_category": "other"})
    again = _candidate("ZenaTech купила Velocity Geomatics", ["ZenaTech"], key="fresh", url="https://a.example/1")

    result = _apply({"topics": [{"topic": GEO, "candidates": [{"signal": again}]}]})

    # Скрытая неразобранная ссылку не держит: она уходит в новую находку, а находку до решения «Отбора» не видно нигде.
    fresh = repository.signal_key_owners(["fresh"])["fresh"]["id"]
    assert repository.list_signals(limit=10) == []
    assert _urls(fresh) == {"https://a.example/1"} and _urls(hidden) == set()
    assert result["all_signals"] == 1
    # Счётчик отдавшей ссылку пересчитан — в «Скрытых» не висит прежнее число.
    with repository.get_connection() as conn:
        assert conn.execute("SELECT evidence_count FROM signals WHERE id = %s", (hidden,)).fetchone()[0] == 0


def test_refound_archived_card_with_its_own_key_stays_archived(isolated_db):
    # Тот же ключ (у одной ссылки ключ — от адреса) — это она же: архив — решение человека о
    # карточке, повторная находка его не отменяет. Другой ключ — новая карточка (тест выше).
    archived = repository.upsert_signal(_signal_row("same", "Velocity Geomatics: первые данные"))
    repository.upsert_signal_evidence(archived, {"source_url": "https://a.example/1", "title": "t"})
    repository.archive_signals([archived], reason="test")
    again = _candidate("ZenaTech купила Velocity Geomatics", ["ZenaTech"], key="same", url="https://a.example/1")

    _apply({"topics": [{"topic": GEO, "candidates": [{"signal": again}]}]})

    assert repository.list_signals(limit=10) == []
    assert _urls(archived) == {"https://a.example/1"}


def test_hidden_reviewed_card_keeps_its_links(isolated_db):
    rejected = repository.upsert_signal(_signal_row("rejected", "Velocity Geomatics: первые данные"))
    repository.upsert_signal_evidence(rejected, {"source_url": "https://a.example/1", "title": "t"})
    _verdict(rejected, "reject")
    repository.archive_signals([rejected], reason="test")
    other = repository.upsert_signal(_signal_row("other", "ZenaTech купила Velocity Geomatics"))

    repository.upsert_signal_evidence(other, {"source_url": "https://a.example/1", "title": "t"})

    assert _urls(rejected) == {"https://a.example/1"} and _urls(other) == set()
    assert repository.visible_evidence_owners(["https://a.example/1"])["https://a.example/1"]["id"] == rejected


# --- Блок А5 схемы агентов (04.11): одно событие из ленты и поиска — одна карточка; черновые данные находки -----

HALLIBURTON_PAGE = "Halliburton acquires Keystone AS to strengthen digital well construction capabilities"
HALLIBURTON_PAGE_2 = "Halliburton Buys Keystone AS for AI Software in Norway 2026"
# Русские заголовки модели поиска — как у этих находок после «Сути и перевода» (прогон 51351, 04.11).
HALLIBURTON_RU = "Halliburton приобрела Keystone AS для развития цифрового строительства скважин"
HALLIBURTON_RU_2 = "Halliburton купила норвежскую Keystone AS для развития ПО с ИИ"


def _feed(signal_id, title, title_ru=None, companies=(), *, fresh=True, urls=()):
    """Сигнал ленты из снимка ядра (list_feed_signals_for_dedup): исходный и русский заголовок, участники."""
    node = _existing(signal_id, title_ru, list(companies), fresh=fresh,
                     urls=urls or [f"https://feed.example/{signal_id}"])
    node["origin"] = signal_dedup.FEED_ORIGIN
    node["signal"].update({"title": title, "origin": signal_dedup.FEED_ORIGIN})
    return node


def _finding_evidence(url, page_title, research_title, company, summary="Суть модели поиска"):
    """Ссылка находки после докачки страницы: в title заголовок страницы, черновые данные поиска — в raw_payload."""
    return {"source_url": url, "title": page_title, "title_ru": page_title, "strength": 0.9,
            "raw_payload": {"evidence_source": "openai_web_search", "research_title": research_title,
                            "company": company, "research_summary": summary, "full_text_fetched": True}}


def _otbor_finding(key, url, page_title, research_title, company):
    """Находка поиска, как её отдаёт _finding_candidate: в сигнале только заголовок страницы."""
    evidence = _finding_evidence(url, page_title, research_title, company)
    return {"signal": {"title": page_title, "theme": GEO, "summary": "", "companies": [], "maturity": "watch",
                       "score": 0, "signal_key": key, "evidence_count": 1, "evidence": [evidence]}}


def test_company_written_with_comma_or_legal_form_is_the_same_company():
    # Черновая компания поиска (прогон 51351): «Halliburton; Keystone AS» и «Halliburton, Keystone AS» — одни
    # участники; «Inc.» после запятой — не компания, иначе общим участником стали бы все «Inc.».
    assert signal_dedup.company_keys({"companies": ["Halliburton; Keystone AS"]}) == \
        signal_dedup.company_keys({"companies": ["Halliburton, Keystone AS"]}) == {"halliburton", "keystone"}
    assert signal_dedup.company_keys({"companies": ["Select Water Solutions, Inc."]}) == {"select water solutions"}
    assert signal_dedup.company_keys({"companies": ["CVR Energy, Inc.", "Equinor ASA"]}) == {"cvr energy", "equinor"}


def test_drafts_of_the_search_catch_the_pair_its_page_titles_miss():
    # Пара прогона 51351: заголовки страниц сходятся на 0,18 при пороге 0,25, компаний у находки нет — пара не
    # дошла до судьи, и событие стало двумя принятыми карточками (30812 и 30836).
    stored = _otbor_finding("a", "https://halliburton.example/keystone", HALLIBURTON_PAGE, HALLIBURTON_RU,
                            "Halliburton; Keystone AS")["signal"]
    first = signal_discovery._finding_dedup_view(stored)
    second = signal_discovery._finding_dedup_view(_otbor_finding(
        "b", "https://norwaypost.example/keystone", HALLIBURTON_PAGE_2, HALLIBURTON_RU_2,
        "Halliburton, Keystone AS")["signal"])
    pages = [{"kind": "new", "reviewed": False, "fresh": True, "urls": [],
              "signal": {"title": title, "companies": []}} for title in (HALLIBURTON_PAGE, HALLIBURTON_PAGE_2)]
    assert signal_dedup.find_pairs(pages) == []

    nodes = [{"kind": "new", "reviewed": False, "fresh": True, "urls": [], "signal": view} for view in (first, second)]
    [(i, j, overlap)] = signal_dedup.find_pairs(nodes)
    assert (i, j) == (0, 1) and round(overlap, 2) == 0.33  # русские заголовки поиска
    # Черновые данные — только у «Дедупа»: в сигнал (и во вход «Отбора») они не идут.
    assert first["title_ru"] == HALLIBURTON_RU and first["companies"] == ["Halliburton; Keystone AS"]
    assert first["summary"] == "Суть модели поиска"
    assert (stored["companies"], stored["summary"], "title_ru" in stored, "research_titles" in stored) == (
        [], "", False, False)


def test_feed_signal_is_paired_with_findings_and_fresh_radar_cards_but_not_with_the_feed():
    nodes = [
        _feed(1, "Halliburton buys Keystone to link well planning with rig execution",
              "Halliburton купила Keystone для связи планирования и бурения", ["Halliburton"]),
        _feed(2, "Halliburton acquires Norway's Keystone AS to expand digital well construction",
              "Halliburton приобрела норвежскую Keystone AS", ["Halliburton", "Keystone AS"]),
        # Свежая неразобранная карточка радара прошлого прогона того же события, старая и разобранная.
        _existing(30, "Halliburton приобрела Keystone AS", ["Halliburton"], fresh=True),
        _existing(31, "Halliburton: сделка с Keystone", ["Halliburton"], fresh=False),
        _existing(32, "Halliburton купила Keystone", ["Halliburton"], verdict="approved", fresh=True),
        _new("Halliburton приобрела Keystone AS", ["Halliburton"], key="new"),
        _hidden(_existing(33, "Halliburton и Keystone: отказ «Отбора»", ["Halliburton"], fresh=True)),
    ]

    pairs = {(i, j) for i, j, _ in signal_dedup.find_pairs(nodes)}

    # Лента с лентой — перепечатки; со старой или разобранной карточкой радара лента не сверяется: дублем в такой
    # паре стала бы лента, а её прогон не прячет; со скрытой — склейка экран не изменит.
    assert {pair for pair in pairs if 5 not in pair and pair[0] < 2} == {(0, 2), (1, 2)}
    assert {(0, 5), (1, 5), (2, 5)} <= pairs


def test_feed_signal_is_the_main_card_and_never_a_duplicate():
    # Статья ленты пришла после находки прошлого прогона: главная — всё равно сигнал ленты, иначе (раньше — главная)
    # событие осталось бы двумя карточками: ленту прогон не прячет.
    feed = _feed(1, "Halliburton buys Keystone", "Halliburton купила Keystone", ["Halliburton"])
    feed["signal"]["first_seen_day"] = "2026-11-05"
    radar = _existing(30, "Halliburton приобрела Keystone AS", ["Halliburton"])
    radar["signal"]["first_seen_day"] = "2026-11-04"
    approved = _existing(32, "Halliburton купила Keystone", ["Halliburton"], verdict="approved", fresh=False)

    assert signal_dedup.assign_duplicates([feed, radar], [(0, 1, "та же сделка")]) == {1: (0, "та же сделка")}
    # Разобранная человеком карточка радара — главная для находки, но сигнал ленты её дублем не становится.
    found = _new("Halliburton приобрела Keystone", ["Halliburton"], key="new")
    assigned = signal_dedup.assign_duplicates([feed, approved, found], [(0, 1, "та же"), (1, 2, "та же")])
    assert assigned == {2: (1, "та же")}


def test_judge_sees_both_titles_of_a_card():
    prompt = signal_dedup.pair_prompt(
        _feed(1, "Consortium to invest $3.1 billion for slice of Mitsubishi's stake in LNG Canada",
              "Консорциум вложит $3,1 млрд в часть доли Mitsubishi в LNG Canada"),
        _new("Mitsubishi продаст Blackstone почти половину холдинга с долей в LNG Canada", []))

    card_a, card_b = prompt.split("Карточка B")
    assert "заголовок: Консорциум вложит" in card_a and "исходный заголовок: Consortium to invest" in card_a
    assert "исходный заголовок" not in card_b  # у карточки один заголовок — одна строка


def test_worker_sends_a_finding_of_an_event_already_in_the_feed_into_the_feed_signal(monkeypatch):
    snapshot = {"existing_signals": [], "feed_signals": [{
        "id": 26009, "origin": "feed", "signal_key": "feed:7350937",
        "title": "Halliburton acquires Norway’s Keystone AS",
        "title_ru": "Halliburton приобрела норвежскую Keystone AS для цифрового строительства скважин",
        "theme": MARKET, "summary": "Сделка", "companies": ["Halliburton", "Keystone AS"], "score": 62.4,
        "evidence_count": 1, "fresh": True, "first_seen_day": "2026-11-03", "verdict": None, "reviewed": False,
        "hidden": False, "evidence_urls": ["https://worldoil.example/keystone"]}]}
    topics = [{"topic": GEO, "candidates": [
        _otbor_finding("k1", "https://halliburton.example/keystone", HALLIBURTON_PAGE, HALLIBURTON_RU,
                       "Halliburton; Keystone AS"),
        _otbor_finding("k2", "https://other.example/drone", "Drone inspects a pipeline", "Дрон осмотрел трубопровод",
                       "DroneCo")]}]
    judge = _Judge("Keystone")
    monkeypatch.setattr(signal_discovery, "_radar_client", lambda: judge)
    config = signal_discovery.SignalDiscoveryConfig(offline=False)

    stats = signal_discovery._dedupe_run(config, snapshot, topics, lambda: None)

    found, other = topics[0]["candidates"]
    assert found["duplicate_of"] == {"signal_id": 26009} and "duplicate_of" not in other
    assert (stats["feed_signals"], stats["duplicates_of_feed"], stats["existing_merges"]) == (1, 1, [])
    # Судья видит русский заголовок поиска рядом с заголовком страницы, компании и суть поиска.
    [prompt] = judge.prompts
    assert HALLIBURTON_RU in prompt and HALLIBURTON_PAGE in prompt and "Halliburton; Keystone AS" in prompt
    assert "Суть модели поиска" in prompt


def test_findings_of_the_next_run_meet_the_cards_of_the_previous_one(monkeypatch):
    """Прогон 51751 (04.11, окно 01-04.11) завёл вторые карточки событий прогона 51351: 31019 к 30830 (итоги TETRA) и
    31041 к 30805 (ADNOC и OpenAI). У сохранённой карточки к тому времени русский заголовок, а правило пар брало только
    его: против английского заголовка страницы новой находки основы почти не сходятся. Черновая компания поиска у
    ADNOC при этом ошибочна («BP»): пару держат заголовки."""
    existing = [
        {"id": 30805, "origin": "radar", "signal_key": "k30805",
         "title": "ADNOC Signs Landmark Partnership Agreement with OpenAI and G42, Becoming First Energy Company to Deploy "
                  "OpenAI Frontier Technology",
         "title_ru": "ADNOC первой среди энергокомпаний внедрит OpenAI Frontier в партнёрстве с OpenAI и G42",
         "theme": MARKET, "summary": "", "companies": ["ADNOC", "OpenAI", "G42"], "score": 62.37, "evidence_count": 2,
         "fresh": True, "first_seen_day": "2026-11-04", "verdict": None, "reviewed": False, "hidden": False,
         "evidence_urls": ["https://www.adnoc.ae/media-center/news/2026/11/adnoc-signs-landmark-partnership"]},
        {"id": 30830, "origin": "radar", "signal_key": "k30830",
         "title": "TETRA TECHNOLOGIES, INC. REPORTS STRONG THIRD-QUARTER 2026 RESULTS",
         "title_ru": "TETRA Technologies представила сильные результаты за третий квартал 2026 года",
         "theme": MARKET, "summary": "", "companies": ["TETRA Technologies, Inc."], "score": 57.87, "evidence_count": 1,
         "fresh": True, "first_seen_day": "2026-11-04", "verdict": None, "reviewed": False, "hidden": False,
         "evidence_urls": ["https://ir.onetetra.com/news/news-details/2026/tetra-technologies-inc-reports-strong"]},
    ]
    adnoc = _otbor_finding("k31041", "https://fortune.example/adnoc-openai",
                           "ADNOC to become first energy company to deploy OpenAI’s Frontier technology | Fortune",
                           "ADNOC первой среди энергокомпаний внедрит технологию OpenAI Frontier", "BP")
    tetra = _otbor_finding("k31019", "https://prnewswire.example/tetra-q3",
                           "TETRA TECHNOLOGIES, INC. REPORTS STRONG THIRD-QUARTER 2026 RESULTS",
                           "TETRA Technologies отчиталась о высоких результатах за третий квартал 2026 года",
                           "TETRA Technologies")
    # Правило до 04.11 (один заголовок: русский у карточки, страницы у находки) пары не видело.
    for row, finding in ((existing[0], adnoc), (existing[1], tetra)):
        assert signal_dedup._overlap(signal_dedup.title_stems(row), signal_dedup.title_stems(finding["signal"])) < 0.25
    judge = _Judge("")
    monkeypatch.setattr(signal_discovery, "_radar_client", lambda: judge)
    config = signal_discovery.SignalDiscoveryConfig(offline=False)
    topics = [{"topic": MARKET, "candidates": [adnoc, tetra]}]

    stats = signal_discovery._dedupe_run(config, {"existing_signals": existing}, topics, lambda: None)

    assert adnoc["duplicate_of"] == {"signal_id": 30805} and tetra["duplicate_of"] == {"signal_id": 30830}
    assert (stats["pairs"], stats["judged"]) == (2, 2)  # только эти две пары: ADNOC и TETRA между собой не сверяются
