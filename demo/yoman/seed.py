"""Synthetic task_log rows for the demo: a fictional house, generated fresh at every start.

Nothing here is copied from a real log. Topics are generic, devices are classes rather than names,
and times are drawn at random rather than taken from any real schedule, so the rows say nothing
about when a real house is empty or what runs in it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

DAYS = 30


@dataclass(frozen=True, slots=True)
class Row:
    ts: datetime
    topic: str
    task: str
    status: str
    severity: str | None
    summary: str
    detail: dict


# (topic, task, runs per day, summaries by status). Round, invented numbers only.
UNITS = (
    (
        "dns-watch",
        "dns-watch",
        12,
        {
            "ok": [
                "{n} new destinations, none flagged",
                "{n} destinations without a DNS answer, all attributed",
            ],
            "error": ["model timed out, digest kept for the next run"],
        },
    ),
    (
        "isp",
        "isp-watch",
        12,
        {
            "ok": ["speed test within contract", "line up, all anchors answering"],
            "error": ["speed test failed, retried next slot"],
        },
    ),
    (
        "backup",
        "backup-watch",
        1,
        {
            "ok": ["all snapshots fresh, newest {n} h old"],
            "error": ["snapshot older than budget"],
        },
    ),
    (
        "certs",
        "certcopy-watch",
        1,
        {"ok": ["certificates copied, {n} days to expiry"], "error": ["copy target unreachable"]},
    ),
    (
        "drift",
        "drift-check",
        1,
        {"ok": ["deployed files match the repo"], "error": ["{n} files differ from the repo"]},
    ),
)

STALE_TASK = "certcopy-watch"

# Hours of silence before a unit is stale, as yoman's own expectation table has it.
CADENCE = {
    ("dns-watch", "dns-watch"): 8,
    ("isp", "isp-watch"): 6,
    ("backup", "backup-watch"): 30,
    ("certs", "certcopy-watch"): 30,
    ("drift", "drift-check"): 30,
    ("note", "homelab"): None,
}

# (host, verdict, headline, detail). A fixed snapshot: the demo has no collector to go stale.
HOSTS = (
    ("server-1", "good", "ok", "disk 41% · ram 38% · temp 52"),
    ("nas", "good", "ok", "disk 67% · ram 22%"),
    ("workstation", "", "asleep", "awake 0"),
    ("laptop-1", "", "2 h ago", "last heard from"),
)

LAN = "10.20.0"
DEVICES = (
    ("workstation", 11, 38.0),
    ("laptop-1", 21, 9.0),
    ("laptop-2", 22, 6.5),
    ("phone-1", 31, 4.0),
    ("phone-2", 32, 3.2),
    ("tv", 41, 14.0),
    ("server-1", 2, 7.5),
    ("sensor-1", 51, 0.02),
    ("sensor-2", 52, 0.02),
    ("sensor-3", 53, 0.03),
    (None, 1, 0.4),
)

ISP_NAME = "ExampleNet"
CONTRACT = (500, 250)

NOTES = (
    "replaced the switch in the cupboard",
    "moved the sensor-3 node to a sealed case",
    "raised the backup retention to 30 days",
    "reflashed laptop-2's dock firmware",
)


def _detail(topic: str, rng: random.Random) -> dict:
    """What a run of that unit would have recorded, in round invented numbers."""
    if topic == "dns-watch":
        seen = rng.randrange(200, 400)
        unexplained = seen // rng.randrange(7, 10)

        return {
            "destinations": seen,
            "without_dns_answer": unexplained,
            "attributed": unexplained,
            "flagged": [],
            "model": "local",
        }

    if topic == "isp":
        return {"down_mbps": rng.randrange(520, 600), "up_mbps": rng.randrange(260, 300)}

    if topic == "backup":
        return {"snapshots": ["photos", "documents", "config"], "newest_h": rng.randrange(1, 20)}

    return {"checked": rng.randrange(20, 60)}


def generate(now: datetime | None = None, rng_seed: int = 7) -> list[Row]:
    """Newest first, the order the log page reads."""
    now = now or datetime.now(UTC)
    rng = random.Random(rng_seed)
    rows: list[Row] = []

    for topic, task, per_day, summaries in UNITS:
        for day in range(DAYS):
            # One unit goes quiet for two days, so the dashboard has a stale row to show.
            if task == STALE_TASK and day < 2:
                continue

            for _ in range(per_day):
                # At least a minute old, so the push two seconds after it is never in the future.
                ts = now - timedelta(days=day, seconds=rng.randrange(60, 86_400))
                failed = rng.random() < 0.04
                status = "error" if failed else "ok"
                summary = rng.choice(summaries[status]).format(n=rng.randrange(1, 40))
                severity = "action" if failed else "clear"
                rows.append(Row(ts, topic, task, status, severity, summary, _detail(topic, rng)))

                if failed:
                    push = Row(
                        ts + timedelta(seconds=2),
                        topic,
                        task,
                        "push",
                        "action",
                        f"{task}: {summary}",
                        {"priority": "high"},
                    )
                    rows.append(push)

    for text in NOTES:
        ts = now - timedelta(days=rng.randrange(DAYS), seconds=rng.randrange(86_400))
        rows.append(Row(ts, "note", "homelab", "ok", None, text, {}))

    # One alarm left standing, so the dashboard has something to stand down.
    rows.append(
        Row(
            now - timedelta(hours=3),
            "backup",
            "backup-watch",
            "error",
            "alarm",
            "no snapshot for 36 h",
            {"budget_h": 30, "newest_h": 36},
        )
    )

    return sorted(rows, key=lambda row: row.ts, reverse=True)


def traffic(days: int, now: datetime | None = None, rng_seed: int = 11) -> list[tuple]:
    """(address, name, out_bytes, in_bytes, last_seen) per device for a window, busiest first."""
    now = now or datetime.now(UTC)
    rng = random.Random(rng_seed + days)
    rows = []

    for name, host, gib_per_day in DEVICES:
        total = gib_per_day * days * rng.uniform(0.7, 1.3) * 2**30
        share_out = rng.uniform(0.04, 0.2)
        seen = now - timedelta(minutes=rng.randrange(5, 30))
        rows.append(
            (f"{LAN}.{host}", name, int(total * share_out), int(total * (1 - share_out)), seen)
        )

    return sorted(rows, key=lambda row: row[2] + row[3], reverse=True)


def isp_month(month_start: datetime, now: datetime, rng_seed: int = 13) -> list[dict]:
    """One dict per day of the month up to `now`, newest first: probe minutes and speed tests."""
    rng = random.Random(rng_seed + month_start.month)
    days = []
    day = month_start

    while day <= now:
        minutes = (
            min(1440, int((now - day).total_seconds() // 60)) if day.date() == now.date() else 1440
        )
        # Clamped to the minutes that have passed, or today's row counts outages still to come.
        lan_down = min(rng.choice([0] * 12 + [9, 14]), minutes)
        link_down = min(rng.choice([0] * 20 + [3]), minutes - lan_down)
        tests = [(rng.gauss(560, 25), rng.gauss(280, 8)) for _ in range(max(1, minutes // 120))]
        failed = 1 if rng.random() < 0.05 else 0
        days.append(
            {
                "day": day.date(),
                "counted": minutes - lan_down,
                "ok": minutes - lan_down - link_down,
                "isp_down": 0,
                "link_down": link_down,
                "lan_down": lan_down,
                "down": sorted(d for d, _ in tests),
                "up": sorted(u for _, u in tests),
                "failed": failed,
                "outage_at": day + timedelta(minutes=rng.randrange(max(1, minutes))),
            }
        )
        day += timedelta(days=1)

    return list(reversed(days))
