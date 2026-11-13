"""Отзывы из таблицы заказчика (CSV) идут в журнал ОС тем же путём, что форма (#188): память агента не пишется."""

from oiltech_digest import signal_feedback
from oiltech_digest.db import repository

CSV = (
    "#,Сигнал,Суть,Почему сейчас,Перенос в нефтесервис,Словарь,Источник,URL,Комментарий\n"
    "1,ADNOC rig,,,,,ADNOC,https://adnoc.example/news,"
    "\"walking capability -> возможность перемещения между скважинами без демонтажа\"\n"
)


def _journal():
    with repository.get_connection() as conn:
        events = conn.execute("SELECT signal_title, source_url, comment FROM signal_feedback_events").fetchall()
        memory = conn.execute("SELECT count(*) FROM signal_agent_memory").fetchone()[0]
    return [tuple(row) for row in events], memory


def test_dry_run_of_the_csv_import_counts_the_feedback_and_writes_nothing(tmp_path, isolated_db):
    path = tmp_path / "feedback.csv"
    path.write_text(CSV, encoding="utf-8")

    result = signal_feedback.import_signal_feedback_csv(path, dry_run=True)

    assert result == {"rows": 1, "feedback_events": 1, "dry_run": True}
    assert _journal() == ([], 0)


def test_csv_import_writes_the_feedback_journal_and_no_agent_memory(tmp_path, isolated_db):
    path = tmp_path / "feedback.csv"
    path.write_text(CSV, encoding="utf-8")

    result = signal_feedback.import_signal_feedback_csv(path, dry_run=False)

    assert result == {"rows": 1, "feedback_events": 1, "dry_run": False}
    # Стрелка в комментарии остаётся текстом отметки: правилом замены она не становится.
    assert _journal() == ([("ADNOC rig", "https://adnoc.example/news",
                            "walking capability -> возможность перемещения между скважинами без демонтажа")], 0)
