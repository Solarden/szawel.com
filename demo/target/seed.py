"""A synthetic set of scored paper targets, for the target-analyzer demo.

Nothing here comes from a real target. The one exception is by construction, not by copying: the
held-out session reproduces the counts the case study reports (37 holes; cv_blob 23 matched, 14
missed, 0 spurious; yolo 34/3/10 at a 0.10 threshold and 32/5/0 at 0.25), over invented positions.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, timedelta

from demo.target.agreement import MATCH_TOL_MM

Point = tuple[float, float]

CANON = 1000
RING_RADII = (50, 100, 150, 200, 250, 300, 350, 400, 450, 500)
TARGET_DIAM_MM = 155.5
MM_PER_PX = TARGET_DIAM_MM / (2 * RING_RADII[-1])
HOLE_MM = 5.6

# Every proposal lands within MAX_OFFSET of its hole, and holes sit at least MIN_SPACING apart, so
# a proposal can never be closer to a neighbouring hole than to its own. That is what lets the
# held-out counts be pinned exactly under closest-first matching.
MAX_OFFSET = 12.0
MIN_SPACING = 40.0
# Well outside the match tolerance, so a spurious proposal can never be matched to a real hole.
SPURIOUS_CLEARANCE = 2 * MATCH_TOL_MM / MM_PER_PX
ON_PAPER = 470.0

HELD_OUT = 7


@dataclass(frozen=True, slots=True)
class Session:
    id: int
    day: date
    shooter: str
    distance_m: int
    truth: list[Point]
    cv_blob: list[Point]
    yolo: list[tuple[Point, float]]
    label: str = ""


def _near(rng: random.Random, point: Point, spread: float) -> Point:
    angle = rng.uniform(0, 2 * math.pi)
    radius = min(abs(rng.gauss(0, spread)), MAX_OFFSET)

    return point[0] + radius * math.cos(angle), point[1] + radius * math.sin(angle)


def _group(rng: random.Random, n: int, bias: Point, sigma: float) -> list[Point]:
    centre = CANON / 2 + bias[0], CANON / 2 + bias[1]
    holes: list[Point] = []

    while len(holes) < n:
        x, y = rng.gauss(centre[0], sigma), rng.gauss(centre[1], sigma)

        if math.hypot(x - CANON / 2, y - CANON / 2) > ON_PAPER:
            continue

        if all(math.dist((x, y), hole) >= MIN_SPACING for hole in holes):
            holes.append((x, y))

    return holes


def _spurious(rng: random.Random, truth: list[Point], n: int) -> list[Point]:
    found: list[Point] = []

    while len(found) < n:
        angle, radius = rng.uniform(0, 2 * math.pi), ON_PAPER * math.sqrt(rng.random())
        point = CANON / 2 + radius * math.cos(angle), CANON / 2 + radius * math.sin(angle)

        if all(math.dist(point, hole) >= SPURIOUS_CLEARANCE for hole in truth):
            found.append(point)

    return found


def _held_out(rng: random.Random, day: date) -> Session:
    truth = _group(rng, 37, (14.0, 22.0), 110.0)
    order = rng.sample(range(len(truth)), len(truth))
    cv_found = set(order[:23])
    rng.shuffle(order)

    # 3 holes yolo never proposes, 2 it proposes weakly (gone by 0.25), 32 it proposes firmly.
    confs = [rng.uniform(0.10, 0.15), rng.uniform(0.15, 0.25)]
    confs += [rng.uniform(0.25, 0.40) for _ in range(8)]
    confs += [rng.uniform(0.40, 0.97) for _ in range(24)]
    yolo = [(_near(rng, truth[i], 3.5), conf) for i, conf in zip(order[3:], confs, strict=True)]

    # The spurious half of 34/3/10 at 0.10 and 32/5/0 at 0.25; the 7 weakest show only at 0.05.
    weak = [rng.uniform(0.10, 0.15) for _ in range(4)] + [rng.uniform(0.15, 0.25) for _ in range(6)]
    weak += [rng.uniform(0.05, 0.10) for _ in range(7)]
    yolo += list(zip(_spurious(rng, truth, len(weak)), weak, strict=True))

    return Session(
        id=HELD_OUT,
        day=day,
        shooter="A",
        distance_m=10,
        truth=truth,
        cv_blob=[_near(rng, truth[i], 6.5) for i in sorted(cv_found)],
        yolo=yolo,
        label="held-out target",
    )


def _string(rng: random.Random, sid: int, day: date, progress: float) -> Session:
    # progress runs 0 -> 1 over the season: the group tightens and drifts toward the centre.
    bias = (rng.uniform(-10, 40) * (1 - progress), rng.uniform(10, 50) * (1 - progress))
    truth = _group(rng, 10, bias, 75 - 35 * progress + rng.uniform(-8, 8))
    yolo = []

    for hole in truth:
        draw = rng.random()

        if draw < 0.85:
            yolo.append((_near(rng, hole, 3.5), rng.uniform(0.25, 0.97)))

        elif draw < 0.95:
            yolo.append((_near(rng, hole, 3.5), rng.uniform(0.10, 0.25)))

    extra = rng.randint(0, 3)
    yolo += list(
        zip(
            _spurious(rng, truth, extra),
            [rng.uniform(0.05, 0.25) for _ in range(extra)],
            strict=True,
        )
    )
    cv_blob = [_near(rng, hole, 6.5) for hole in truth if rng.random() < 0.7]
    cv_blob += _spurious(rng, truth, rng.randint(0, 2))

    return Session(
        id=sid,
        day=day,
        shooter=rng.choice("AB"),
        distance_m=rng.choice((10, 25)),
        truth=truth,
        cv_blob=cv_blob,
        yolo=yolo,
    )


def generate(today: date, n: int = 12) -> list[Session]:
    """Sessions oldest first, on days drawn at random over the last half year."""
    rng = random.Random(20260925)
    days = sorted(today - timedelta(days=back) for back in rng.sample(range(2, 180), n))

    return [
        _held_out(rng, day) if sid == HELD_OUT else _string(rng, sid, day, sid / n)
        for sid, day in enumerate(days, start=1)
    ]
