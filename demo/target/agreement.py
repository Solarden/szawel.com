"""Comparing a detector's reading of a target against a person's.

Copied unchanged from target-analyzer's `ta_shared/agreement.py`, so matched / missed / spurious
mean here exactly what they mean in the real app.
"""

from dataclasses import dataclass
from math import hypot

Point = tuple[float, float]

# Two readings within this of each other are the same hole. In millimetres because the
# canonical square spans a different physical target per profile, so one pixel count
# would mean a different distance on each.
MATCH_TOL_MM = 4.0


@dataclass(frozen=True)
class Agreement:
    matched: int
    missed: int  # in the truth, and the detector did not find it
    spurious: int  # the detector found it, and it is not in the truth
    mean_offset_px: float | None  # over the matched pairs; None when nothing matched


def agreement(truth: list[Point], other: list[Point], tol_px: float) -> Agreement:
    """Match ``other`` against ``truth``, closest pair first, within ``tol_px``.

    Closest-first rather than in order: two holes that touch would otherwise pair with
    each other's detection and report two large offsets instead of two small ones. Greedy
    is not optimal, but the alternative is an assignment solver for a handful of points
    that are metres apart in practice.
    """
    pairs = sorted(
        (hypot(tx - ox, ty - oy), t, o)
        for t, (tx, ty) in enumerate(truth)
        for o, (ox, oy) in enumerate(other)
    )
    taken_truth: set[int] = set()
    taken_other: set[int] = set()
    offsets = []

    for distance, t, o in pairs:
        if distance > tol_px:
            break

        if t not in taken_truth and o not in taken_other:
            taken_truth.add(t)
            taken_other.add(o)
            offsets.append(distance)

    return Agreement(
        matched=len(offsets),
        missed=len(truth) - len(offsets),
        spurious=len(other) - len(offsets),
        mean_offset_px=(sum(offsets) / len(offsets)) if offsets else None,
    )
