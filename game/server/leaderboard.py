"""The leaderboard: a rolling weekly top ten, on disk beside the kill switch.

Nothing here knows the rules or the socket — `record` takes an integer. What the table holds
is a generated handle, a score and the week it belongs to; an address and a match id are the
two things it must never hold, because both would make a row point back at a visitor.
"""

import os
import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

# A blank reads as unset, as it does for the other PLAY_ values: blanking a line in an
# EnvironmentFile must not quietly move the board to a new empty file beside the real one.
DB_PATH = Path(os.environ.get("PLAY_DB_FILE", "").strip() or "/var/lib/play/leaderboard.db")

# No free text anywhere near this, so there is no moderation surface. 576 combinations is
# enough that a collision inside one week is a curiosity rather than a bug.
ADJECTIVES = tuple(
    "brisk calm candid civil clear crisp deft eager even fleet glad keen "
    "lucid mild neat nimble plain prompt quiet solid spare steady swift tidy".split()
)
ANIMALS = tuple(
    "badger crane falcon ferret heron ibex jackal kestrel lemur marten moose otter "
    "owl puffin raven shrew stoat tapir tern viper vole walrus weasel wombat".split()
)


def _connect() -> sqlite3.Connection:
    """A connection per call, opened lazily.

    Importing this module must not touch the disk: the default path is the box's state
    directory, and reaching for it during pytest collection would take the suite down on any
    machine that is not the box, before a fixture could redirect `DB_PATH`.
    """
    db = sqlite3.connect(DB_PATH, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS scores "
        "(week TEXT NOT NULL, handle TEXT NOT NULL, score INTEGER NOT NULL)"
    )
    # Rows are never deleted, so the table only grows, and `top` runs on every handshake. A
    # year of rows scans in about four milliseconds without this and nine microseconds with
    # it — and adding it later is the migration this schema deliberately has no tool for.
    db.execute("CREATE INDEX IF NOT EXISTS week_score ON scores(week, score DESC)")

    return db


def week_of(moment: datetime) -> str:
    """The ISO week a moment belongs to, in the form the rows carry.

    %G, not %Y: 2027-01-01 is a Friday inside 2026-W53, and %Y-%V would file it under a week
    that has not started yet.
    """
    return moment.strftime("%G-W%V")


def current_week() -> str:
    # UTC, not local. TZ is a config value that can change under the box, and a DST
    # transition makes one local week 167 or 169 hours long — either one re-partitions the
    # board without anything saying so.
    return week_of(datetime.now(UTC))


def handle() -> str:
    return f"{secrets.choice(ADJECTIVES)}-{secrets.choice(ANIMALS)}"


def record(points: int) -> str:
    """File a finished match under a fresh handle, and return the handle it went on under."""
    name = handle()

    # ponytail: sqlite on the event loop, inside the match lock; a WAL checkpoint can stall
    # every socket. Move it off the loop once someone has measured that.
    with closing(_connect()) as db:
        db.execute("INSERT INTO scores VALUES (?, ?, ?)", (current_week(), name, points))

    return name


def top(limit: int = 10) -> list[dict]:
    """This week's best, best first.

    Last week's rows are still in the table and simply stop being selected. A reset that is a
    query cannot fail to run, which is what a scheduled delete would do silently.
    """
    with closing(_connect()) as db:
        rows = db.execute(
            # rowid breaks a tie in favour of whoever reached the score first, and costs no
            # column to store.
            "SELECT handle, score FROM scores WHERE week = ? ORDER BY score DESC, rowid LIMIT ?",
            (current_week(), limit),
        ).fetchall()

    return [{"handle": name, "score": points} for name, points in rows]
