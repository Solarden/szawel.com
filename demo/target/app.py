"""A clickable copy of target-analyzer's dashboard, over synthetic targets.

The real app scores photos of paper targets and keeps every detector's reading beside the one a
person confirmed. This renders its three views from `seed.generate()`, read-only, in its own
process. The one control is the threshold a detector's proposals must clear, because that is the
number the case study is about.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from demo.target import seed
from demo.target.agreement import MATCH_TOL_MM, agreement

BASE = "/demo/target-analyzer"
CASE_STUDY = "https://www.szawel.com/work/target-analyzer.html"
CONFS = (0.05, 0.10, 0.15, 0.25, 0.40)
CONF_DEFAULT = 0.10
TOL_PX = MATCH_TOL_MM / seed.MM_PER_PX
HOLE_R = seed.HOLE_MM / 2 / seed.MM_PER_PX

SESSIONS = {session.id: session for session in seed.generate(date.today())}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount(f"{BASE}/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
# The dark skin is the yoman demo's, served from its directory so the two demos cannot drift apart.
# check_dir=False: without that directory the pages lose their skin, not the process its start.
app.mount(
    f"{BASE}/skin",
    StaticFiles(directory=Path(__file__).parent.parent / "yoman" / "static", check_dir=False),
    name="skin",
)


@dataclass(frozen=True, slots=True)
class Score:
    n: int
    total: int
    avg: float | None
    centroid: seed.Point | None
    bias_mm: float | None
    direction: str
    mean_radius_mm: float | None
    spread_mm: float | None


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _ring(point: seed.Point) -> int:
    distance = math.dist(point, (seed.CANON / 2, seed.CANON / 2))
    inside = [radius for radius in seed.RING_RADII if distance <= radius]

    return len(inside)


def _direction(dx: float, dy: float) -> str:
    # The canonical frame is image convention: +y is down, which a shooter calls low.
    vertical = "low" if dy > 0.5 else "high" if dy < -0.5 else ""
    horizontal = "right" if dx > 0.5 else "left" if dx < -0.5 else ""

    return "-".join(part for part in (vertical, horizontal) if part) or "centered"


def score(points: list[seed.Point]) -> Score:
    if not points:
        return Score(0, 0, None, None, None, "", None, None)

    n = len(points)
    rings = [_ring(point) for point in points]
    centroid = sum(x for x, _ in points) / n, sum(y for _, y in points) / n
    dx, dy = centroid[0] - seed.CANON / 2, centroid[1] - seed.CANON / 2
    mean_radius = sum(math.dist(point, centroid) for point in points) / n
    spread = max((math.dist(a, b) for a, b in combinations(points, 2)), default=0.0)

    return Score(
        n=n,
        total=sum(rings),
        avg=sum(rings) / n,
        centroid=centroid,
        bias_mm=math.hypot(dx, dy) * seed.MM_PER_PX,
        direction=_direction(dx, dy),
        mean_radius_mm=mean_radius * seed.MM_PER_PX,
        spread_mm=spread * seed.MM_PER_PX,
    )


def _num(value: float | None, digits: int = 1, unit: str = "") -> str:
    return "—" if value is None else f"{value:.{digits}f}{unit}"


def _conf(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError:
        return CONF_DEFAULT

    return value if value in CONFS else CONF_DEFAULT


def _target_svg(points: list[seed.Point], label: str, group: Score | None = None) -> str:
    c = seed.CANON / 2
    rings = "".join(
        f'<circle class="ring{" bull" if radius <= seed.RING_RADII[3] else ""}" '
        f'cx="{c}" cy="{c}" r="{radius}"/>'
        for radius in reversed(seed.RING_RADII)
    )
    holes = "".join(
        f'<circle class=hole cx="{x:.1f}" cy="{y:.1f}" r="{HOLE_R:.1f}">'
        f"<title>ring {_ring((x, y)) or 'miss'}</title></circle>"
        for x, y in points
    )
    bias = ""

    if group and group.centroid:
        gx, gy = group.centroid
        bias = (
            f'<line class=bias x1="{c}" y1="{c}" x2="{gx:.1f}" y2="{gy:.1f}"/>'
            f'<circle class=centroid cx="{gx:.1f}" cy="{gy:.1f}" r="{HOLE_R:.1f}"/>'
        )

    return (
        f'<svg class=target viewBox="0 0 {seed.CANON} {seed.CANON}" role=img '
        f'aria-label="{_esc(label)}"><rect class=paper width="{seed.CANON}" '
        f'height="{seed.CANON}"/>{rings}{holes}{bias}</svg>'
    )


def _metrics(result: Score) -> str:
    rows = (
        ("holes", str(result.n)),
        ("total", str(result.total)),
        ("average", _num(result.avg, 2)),
        ("mean radius", _num(result.mean_radius_mm, 1, " mm")),
        ("extreme spread", _num(result.spread_mm, 1, " mm")),
        ("bias", f"{_num(result.bias_mm, 1, ' mm')} {result.direction}".strip()),
    )

    cells = "".join(f"<dt>{key}</dt><dd>{_esc(value)}</dd>" for key, value in rows)

    return f"<dl class=metrics>{cells}</dl>"


def _title(session: seed.Session) -> str:
    tag = f" · {session.label}" if session.label else ""

    return f"{session.day:%Y-%m-%d} · shooter {session.shooter} · {session.distance_m} m{tag}"


def _page(title: str, body: str, current: str = "") -> str:
    nav = "".join(
        f'<a{" class=on" if key == current else ""} href="{href}">{label}</a>'
        for key, href, label in (
            ("trend", f"{BASE}/", "trend"),
            ("compare", f"{BASE}/compare/{seed.HELD_OUT}", "compare readings"),
        )
    )

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{_esc(title)} — target-analyzer demo</title>
<link rel=stylesheet href="{BASE}/skin/yoman.css">
<link rel=stylesheet href="{BASE}/static/target.css"></head><body>
<div class=demo>Demo of target-analyzer, photo-based scoring of paper targets, from
<a href="{CASE_STUDY}">this case study</a>. Synthetic targets and readings. On the held-out
target, cv_blob and yolo at 0.10 and 0.25 are matched to the real measured run; every other number
is illustrative.</div>
<header><h1>target-analyzer</h1><span class=sub>demo range</span><nav>{nav}</nav></header>
<main>{body}</main>
</body></html>"""


def _chart(
    sessions: list[seed.Session], values: list[float], name: str, unit: str, top: float
) -> str:
    width, height, left, right, pad = 560, 190, 44, 12, 18
    first, last = sessions[0].day.toordinal(), sessions[-1].day.toordinal()

    def x(session: seed.Session) -> float:
        return left + (session.day.toordinal() - first) / (last - first) * (width - left - right)

    def y(value: float) -> float:
        return pad + (1 - value / top) * (height - 2 * pad)

    grid = "".join(
        f'<line class=grid x1="{left}" x2="{width - right}" y1="{y(top * f):.1f}" '
        f'y2="{y(top * f):.1f}"/><text class=tick x="{left - 6}" y="{y(top * f) + 4:.1f}">'
        f"{top * f:.0f}</text>"
        for f in (0, 0.5, 1)
    )
    line = " ".join(f"{x(s):.1f},{y(v):.1f}" for s, v in zip(sessions, values, strict=True))
    marks = "".join(
        f'<a href="{BASE}/session/{s.id}"><circle class=mark cx="{x(s):.1f}" cy="{y(v):.1f}" r=5>'
        f"<title>{s.day:%Y-%m-%d} · {v:.2f}{unit}</title></circle></a>"
        for s, v in zip(sessions, values, strict=True)
    )
    days = (
        f'<text class="tick start" x="{left}" y="{height - 2}">{sessions[0].day:%Y-%m-%d}</text>'
        f'<text class=tick x="{width - right}" y="{height - 2}">{sessions[-1].day:%Y-%m-%d}</text>'
    )

    return (
        f"<figure class=panel><figcaption>{_esc(name)}</figcaption>"
        f'<svg class=chart viewBox="0 0 {width} {height}" role=img '
        f'aria-label="{_esc(name)} per session">'
        f'{grid}<polyline class=line points="{line}"/>{marks}{days}</svg></figure>'
    )


def render_trend() -> str:
    sessions = sorted(SESSIONS.values(), key=lambda s: s.day)
    scores = [score(s.truth) for s in sessions]
    top_radius = math.ceil(max(r.mean_radius_mm for r in scores) / 5) * 5
    rows = "".join(
        f'<tr><td class=ts><a href="{BASE}/session/{s.id}">{s.day:%Y-%m-%d}</a></td>'
        f"<td>{_esc(s.shooter)}</td><td class=num>{s.distance_m} m</td><td class=num>{r.n}</td>"
        f"<td class=num>{_num(r.avg, 2)}</td><td class=num>{_num(r.mean_radius_mm, 1)}</td>"
        f"<td>{_esc(r.direction)}</td><td class=muted>{_esc(s.label)}</td></tr>"
        for s, r in zip(reversed(sessions), reversed(scores), strict=True)
    )

    return _page(
        "trend",
        f"""<div class=charts>
{_chart(sessions, [r.avg for r in scores], "average ring per hole", "", 10)}
{_chart(sessions, [r.mean_radius_mm for r in scores], "mean radius, mm", " mm", top_radius)}
</div>
<p class=note>Every point is a person-confirmed reading. A detector's reading never reaches the
trend, however confident it is.</p>
<h2>sessions ({len(sessions)})</h2>
<div class=panel><table>
<thead><tr><th>day</th><th>shooter</th><th class=num>distance</th><th class=num>holes</th>
<th class=num>average</th><th class=num>mean radius mm</th><th>bias</th><th></th></tr></thead>
<tbody>{rows}</tbody></table></div>""",
        "trend",
    )


def render_session(session: seed.Session) -> str:
    result = score(session.truth)

    return _page(
        _title(session),
        f"""<h2>{_esc(_title(session))}</h2>
<div class="panel solo">{_target_svg(session.truth, f"{result.n} holes", result)}{_metrics(result)}
<p class=note>Read by hand, then confirmed ·
<a href="{BASE}/compare/{session.id}">compare with the detectors</a></p></div>
<p class=note>The line runs from the point of aim to the group centre: the bias a shooter corrects
by moving the sight, as opposed to the spread, which only practice closes.</p>""",
    )


def _cells(found: list[seed.Point], truth: list[seed.Point]) -> str:
    result = agreement(truth, found, TOL_PX)
    offset = None if result.mean_offset_px is None else result.mean_offset_px * seed.MM_PER_PX

    return (
        f"<td class=num>{len(found)}</td><td class=num>{result.matched}</td>"
        f"<td class=num>{result.missed}</td><td class=num>{result.spurious}</td>"
        f"<td class=num>{_num(offset, 2, ' mm')}</td>"
    )


def render_compare(session: seed.Session, conf: float) -> str:
    yolo = [point for point, confidence in session.yolo if confidence >= conf]
    head = (
        "<thead><tr><th>reading</th><th class=num>proposed</th><th class=num>matched</th>"
        "<th class=num>missed</th><th class=num>spurious</th><th class=num>offset</th></tr></thead>"
    )
    sweep = "".join(
        f"<tr{' class=on' if value == conf else ''}><td>"
        f'<a href="?conf={value:.2f}">yolo ≥ {value:.2f}</a>'
        f"{' <span class=muted>(the default)</span>' if value == CONF_DEFAULT else ''}</td>"
        f"{_cells([p for p, k in session.yolo if k >= value], session.truth)}</tr>"
        for value in CONFS
    )
    panels = "".join(
        f"<div class=panel><h3>{name}</h3>{_target_svg(points, f'{name}: {len(points)} holes')}"
        f"<p class=note>{note}</p></div>"
        for name, points, note in (
            ("confirmed by a person", session.truth, "the truth every reading is scored against"),
            ("cv_blob", session.cv_blob, "classic blob detection, no threshold to move"),
            (f"yolo ≥ {conf:.2f}", yolo, "the trained detector, cut at the threshold chosen above"),
        )
    )

    return _page(
        f"compare · {_title(session)}",
        f"""<h2>{_esc(_title(session))}</h2>
<div class=panel><table>{head}<tbody>
<tr><td>cv_blob</td>{_cells(session.cv_blob, session.truth)}</tr>
<tr class=on><td>yolo ≥ {conf:.2f}</td>{_cells(yolo, session.truth)}</tr>
</tbody></table></div>
<h2>move the threshold</h2>
<div class=panel><table>{head.replace("reading", "yolo cut at")}<tbody>{sweep}</tbody></table></div>
<p class=note>A detector only proposes: a person confirms every target, so a spurious hole costs one
click to remove and a missed one costs a click to place. Lowering the threshold trades misses for
spurious holes. {CONF_DEFAULT:.2f} is the default the real app ships with. Two readings are the
same hole within {MATCH_TOL_MM:.0f} mm.</p>
<div class=compare>{panels}</div>""",
        "compare" if session.id == seed.HELD_OUT else "",
    )


def _session(raw: str) -> seed.Session | None:
    return SESSIONS.get(int(raw)) if raw.isascii() and raw.isdigit() else None


@app.get(f"{BASE}/", response_class=HTMLResponse)
async def trend() -> str:
    return render_trend()


@app.get(f"{BASE}/session/{{sid}}", response_model=None)
async def session_page(sid: str) -> HTMLResponse | RedirectResponse:
    session = _session(sid)

    if not session:
        return RedirectResponse(f"{BASE}/", status_code=303)

    return HTMLResponse(render_session(session))


@app.get(f"{BASE}/compare/{{sid}}", response_model=None)
async def compare_page(sid: str, conf: str = "") -> HTMLResponse | RedirectResponse:
    session = _session(sid)

    if not session:
        return RedirectResponse(f"{BASE}/", status_code=303)

    return HTMLResponse(render_compare(session, _conf(conf)))
