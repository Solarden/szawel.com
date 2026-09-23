import importlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest

from game.server import leaderboard


@pytest.fixture(autouse=True)
def a_scratch_board(tmp_path, monkeypatch):
    monkeypatch.setattr(leaderboard, "DB_PATH", tmp_path / "leaderboard.db")


def rows(path):
    return closing(sqlite3.connect(path))


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 9, 20, 23, 59, 59, tzinfo=UTC), "2026-W38"),
        (datetime(2026, 9, 21, 0, 0, 0, tzinfo=UTC), "2026-W39"),
        (datetime(2027, 1, 1, tzinfo=UTC), "2026-W53"),
        (datetime(2026, 1, 1, tzinfo=UTC), "2026-W01"),
    ],
)
def test_the_week_key_turns_over_on_monday_morning(moment, expected):
    assert leaderboard.week_of(moment) == expected


def test_a_recorded_score_reaches_the_board():
    name = leaderboard.record(41)

    assert leaderboard.top() == [{"handle": name, "score": 41}]


def test_a_row_from_last_week_stops_being_selected_rather_than_deleted():
    leaderboard.record(41)

    with rows(leaderboard.DB_PATH) as db:
        db.execute("INSERT INTO scores VALUES ('2001-W01', 'ancient-mariner', 48)")
        db.commit()
        held = db.execute("SELECT count(*) FROM scores").fetchone()[0]

    # The higher score is the older row, so a board that ignored the week would lead with it.
    assert [row["score"] for row in leaderboard.top()] == [41]
    assert held == 2


def test_the_board_is_the_ten_best_of_the_week():
    for points in range(1, 16):
        leaderboard.record(points)

    assert [row["score"] for row in leaderboard.top()] == list(range(15, 5, -1))


def test_a_tie_goes_to_whoever_reached_it_first():
    first = leaderboard.record(30)
    leaderboard.record(30)

    assert leaderboard.top()[0]["handle"] == first


def test_a_handle_is_two_words_from_the_lists():
    handles = {leaderboard.handle() for _ in range(50)}

    for name in handles:
        adjective, animal = name.split("-")

        assert adjective in leaderboard.ADJECTIVES
        assert animal in leaderboard.ANIMALS

    assert len(handles) > 1


def test_the_database_holds_one_table_and_nothing_pointing_at_anybody():
    leaderboard.record(41)

    with rows(leaderboard.DB_PATH) as db:
        tables = {
            name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        columns = {column[1] for column in db.execute("PRAGMA table_info(scores)")}

    assert tables == {"scores"}
    assert columns == {"week", "handle", "score"}


def test_a_restart_finds_the_board_where_it_left_it(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAY_DB_FILE", str(tmp_path / "restarted.db"))
    importlib.reload(leaderboard).record(41)
    # A restart is a second import: the path is read again and the schema re-applied against
    # a file that already exists.
    restarted = importlib.reload(leaderboard)

    assert [row["score"] for row in restarted.top()] == [41]
