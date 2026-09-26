"""A clickable copy of yoman's pages, over synthetic rows.

yoman itself is private and reads a real house's log. This renders the same pages from
`seed.generate()`, in its own process, so nothing here can reach the real one.
"""

from __future__ import annotations

import html
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from demo.yoman import seed

BASE = "/demo/yoman"
CASE_STUDY = "https://www.szawel.com/work/yoman.html"
LIMIT_DEFAULT, LIMIT_MAX = 50, 500
STATUSES = ("", "ok", "error", "push")
SEVERITIES = ("", "open", "alarm", "action", "clear")
SEV_STORED = ("clear", "action", "alarm")
SEV_LABELS = {
    "": "all",
    "open": "DEFCON 1-4",
    "alarm": "DEFCON 2",
    "action": "DEFCON 3-4",
    "clear": "DEFCON 5",
}

NAV = (("", "dashboard"), ("log", "task log"), ("isp", "ISP"), ("net", "traffic"))
NET_WINDOWS = (1, 7, 30)
FLAP_RATIO, FLAP_MIN_BAD = 0.25, 3
VERDICT_RANK = {"alarm": 0, "stale": 1, "action": 2, "flapping": 3}

# The house is regenerated when it gets this old. Its timestamps are relative to the moment it was
# made, and a process running for days would otherwise show every unit stale.
REFRESH = timedelta(minutes=30)
STARTED = datetime.now(UTC)
ROWS = seed.generate(STARTED)
# "alert" only ever appears in a visit's own rows, and its filter must still work there.
TOPICS = sorted({row.topic for row in ROWS} | {"alert"})

# Every handler is async with no await inside, so all of this state is touched from the event loop
# alone and needs no lock.
#
# A visit that stood the alarm down. Held in memory only, keyed by an id in the page's address:
# no cookie, nothing written down, and gone after SESSION_TTL without a request.
SESSION_TTL = timedelta(minutes=30)
SESSION_CAP = 500
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{22}")


def defcon(severity: str | None, status: str) -> str | None:
    """yoman's pill rule: the real five-level scale, with `action` split by status."""
    if severity not in SEV_STORED or status == "push":
        return None

    if severity == "action":
        return "DEFCON 3" if status == "error" else "DEFCON 4"

    return "DEFCON 2" if severity == "alarm" else "DEFCON 5"


def sev_pill(severity: str | None, status: str) -> str:
    """yoman's pill: the stored token as the class, plus `lone` so a 3 does not look like a 4."""
    level = defcon(severity, status)

    if not level:
        return ""

    lone = " lone" if level == "DEFCON 3" else ""

    return f'<span class="pill sev-{severity}{lone}">{level}</span>'


@dataclass(slots=True)
class Visit:
    id: str
    seen: datetime
    stood_down: datetime | None = None
    rows: list[seed.Row] = field(default_factory=list)


_visits: dict[str, Visit] = {}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount(f"{BASE}/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _ts(value: datetime) -> str:
    # Rendered in UTC; the page script rewrites it into the viewer's own zone.
    return f'<span data-ts="{value.isoformat()}">{value:%Y-%m-%d %H:%M:%S}</span>'


def _clamp(raw: str, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(raw)))
    except ValueError:
        return default


def _date(raw: str) -> str:
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _matches(row: seed.Row, topic: str, status: str, sev: str, dfrom: str, dto: str) -> bool:
    day = f"{row.ts:%Y-%m-%d}"

    # A push carries the ntfy priority, not a verdict, so no severity filter ever matches one.
    if sev and row.status == "push":
        return False

    if sev == "open":
        if row.severity not in ("action", "alarm"):
            return False

    elif sev and row.severity != sev:
        return False

    return (
        (not topic or row.topic == topic)
        and (not status or row.status == status)
        and (not dfrom or day >= dfrom)
        and (not dto or day <= dto)
    )


def _options(values, selected: str, labels: dict[str, str] | None = None) -> str:
    return "".join(
        f'<option value="{_esc(value)}"{" selected" if value == selected else ""}>'
        f"{_esc((labels or {}).get(value) or value or 'all')}</option>"
        for value in values
    )


def _ago(seconds: float) -> str:
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"

    if seconds < 172800:
        return f"{seconds / 3600:.0f} h"

    return f"{seconds / 86400:.0f} d"


def _bytes(n: int) -> str:
    value = float(n)

    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" or value >= 100 else f"{value:.1f} {unit}"

        value /= 1024

    return f"{value:.1f} TiB"


def _tile(key: str, value: str, verdict: str = "", sub: str = "", href: str = "") -> str:
    label = f'<a href="{_esc(href)}">{_esc(key)}</a>' if href else _esc(key)
    extra = f"<div class=k>{_esc(sub)}</div>" if sub else ""

    return (
        f'<div class="tile {verdict}"><div class=k>{label}</div>'
        f"<div class=v>{_esc(value)}</div>{extra}</div>"
    )


def _sweep(now: datetime) -> None:
    for key in [key for key, visit in _visits.items() if now - visit.seen > SESSION_TTL]:
        del _visits[key]


def _visit(key: str) -> Visit | None:
    now = datetime.now(UTC)
    _sweep(now)
    visit = _visits.get(key) if SESSION_ID.fullmatch(key) else None

    if visit:
        visit.seen = now

    return visit


def _refresh() -> None:
    global STARTED, ROWS

    now = datetime.now(UTC)

    if now - STARTED > REFRESH:
        STARTED, ROWS = now, seed.generate(now)


def _rows(visit: Visit | None) -> list[seed.Row]:
    if not visit:
        return ROWS

    # Merged, not prepended: a refresh can generate house rows newer than the visit's own.
    return sorted(visit.rows + ROWS, key=lambda row: row.ts, reverse=True)


def _alarm(visit: Visit | None) -> seed.Row | None:
    alarm = next((row for row in ROWS if row.severity == "alarm"), None)

    if alarm and visit and visit.stood_down and alarm.ts <= visit.stood_down:
        return None

    return alarm


def _alarm_strip(visit: Visit | None) -> str:
    alarm = _alarm(visit)

    if alarm:
        return (
            # ponytail: always 2, by choice. yoman turns the banner to 1 once an alarm has stood
            # unanswered for a day, and the demo's alarm does get that old - it is seeded 3 h before
            # the process started. A public page reading "ignored" would say the wrong thing about a
            # house nobody is running.
            f"<div class=toast><span class=lvl>DEFCON 2</span>"
            f"<span class=what>{_esc(alarm.topic)} · {_esc(alarm.task)} · "
            f"{_esc(alarm.summary)}</span><span class=when>{_ts(alarm.ts)}</span>"
            f'<form method=post action="stand-down"><button type=submit>stand down</button></form>'
            f"</div>"
        )

    if visit and visit.stood_down:
        return (
            '<div class="toast quiet"><span class=lvl>no alarm standing</span>'
            "<span class=what>stood down on this visit</span>"
            f"<span class=when>{_ts(visit.stood_down)}</span></div>"
        )

    return ""


def _page(title: str, body: str, visit: Visit | None, current: str = "") -> str:
    kept = (
        " This visit's stand-down lives in the page address and is forgotten after 30 idle minutes."
        if visit
        else ""
    )

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{_esc(title)} — yoman demo</title>
<link rel=stylesheet href="{BASE}/static/yoman.css"></head><body>
<div class=demo>Demo of yoman, the audit-trail dashboard from
<a href="{CASE_STUDY}">this case study</a>.
Synthetic data, regenerated on every restart.{kept}</div>
<header><h1>yoman</h1><span class=sub>task_log · demo house</span>
{_nav(current)}</header>
<main>{_alarm_strip(visit)}{body}</main>
<div id=modal class=modal hidden><div class=modal-backdrop></div>
<div class=modal-card><div id=modal-head></div><div id=modal-body></div></div></div>
<script src="{BASE}/static/yoman.js"></script>
</body></html>"""


def _nav(current: str) -> str:
    links = "".join(
        f'<a{" class=on" if key == current else ""} href="./{"?p=" + key if key else ""}">'
        f"{_esc(label)}</a>"
        for key, label in NAV
    )

    return f"<nav>{links}</nav>"


def _verdict(row: seed.Row | None, budget: int | None, now: datetime, visit: Visit | None) -> str:
    if row and row.severity == "alarm":
        if _alarm(visit):
            return "alarm"

        # Stood down: the row stays in the log, but only its age can still make it a finding.
        row = seed.Row(row.ts, row.topic, row.task, row.status, None, row.summary, row.detail)

    if budget is not None and (row is None or (now - row.ts).total_seconds() > budget * 3600):
        return "stale"

    return "action" if row and row.severity == "action" else ""


def render_dashboard(visit: Visit | None) -> str:
    now = datetime.now(UTC)
    week = now.timestamp() - 7 * 86400
    exceptions, quiet = [], 0

    for (topic, task), budget in seed.CADENCE.items():
        runs = [
            row
            for row in _rows(visit)
            if (row.topic, row.task) == (topic, task) and row.status != "push"
        ]
        latest = runs[0] if runs else None

        if budget is None and latest is None:
            continue

        verdict = _verdict(latest, budget, now, visit)
        recent = [row for row in runs if row.ts.timestamp() > week]
        bad = sum(row.severity in ("action", "alarm") or row.status == "error" for row in recent)

        if not verdict and bad >= FLAP_MIN_BAD and bad >= len(recent) * FLAP_RATIO:
            verdict = "flapping"

        if not verdict:
            quiet += 1

            continue

        pill = (
            sev_pill(latest.severity, latest.status)
            if verdict in ("alarm", "action")
            else f'<span class="pill stale">{verdict}</span>'
        )
        said = latest.summary if verdict != "flapping" else f"{bad} of {len(recent)} runs not clean"
        expect = f", expected every {budget} h" if budget else ""
        age = _ago((now - latest.ts).total_seconds())
        when = f"{_ts(latest.ts)} <span class=muted>{age} ago{expect}</span>"
        exceptions.append(
            (
                VERDICT_RANK[verdict],
                f'<tr><td><a href="?p=log&amp;topic={_esc(topic)}">'
                f"{_esc(topic)} / {_esc(task)}</a></td>"
                f"<td>{pill}</td><td class=ts>{when}</td><td>{_esc(said)}</td></tr>",
            )
        )

    exceptions.sort(key=lambda item: item[0])
    rows = "".join(html_row for _, html_row in exceptions) or (
        f"<tr><td colspan=4 class=muted>nothing — {quiet} units, all reporting on time</td></tr>"
    )
    hosts = "".join(
        _tile(host, headline, verdict, sub) for host, verdict, headline, sub in seed.HOSTS
    )
    total = quiet + len(exceptions)
    tiles = (
        _tile("ISP", "within contract", "good", "0 ISP-outage minutes", "?p=isp")
        + _tile("devices", str(len(seed.DEVICES)), "good", "traffic per device", "?p=net")
        + _tile("reporting on time", f"{quiet}/{total}", "" if exceptions else "good")
    )
    pushes = [row for row in _rows(visit) if row.status == "push"][:9]
    lately = "".join(
        f"<tr><td class=ts>{_ts(row.ts)}</td>"
        f'<td><a href="?p=log&amp;topic={_esc(row.topic)}">{_esc(row.topic)}</a></td>'
        f"<td>{_esc(row.summary)}</td></tr>"
        for row in pushes
    )

    return _page(
        "dashboard",
        f"""<div class=tiles>{hosts}</div>
<h2>needs a look ({len(exceptions)})</h2>
<div class=panel><table>
<thead><tr><th>what</th><th>state</th><th>last run</th><th>said</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<h2>quiet ({quiet})</h2>
<div class=tiles>{tiles}</div>
<h2>lately</h2>
<div class=panel><table>
<thead><tr><th>when</th><th>topic</th><th>said</th></tr></thead>
<tbody>{lately}</tbody></table></div>""",
        visit,
    )


def render_net(days: int, visit: Visit | None) -> str:
    options = "".join(
        f'<option value="{d}"{" selected" if d == days else ""}>last {d}d</option>'
        for d in NET_WINDOWS
    )
    body = "".join(
        f"<tr><td>{_esc(name or address)}</td><td class=muted>{_esc(address) if name else ''}</td>"
        f"<td class=num>{_bytes(in_b)}</td><td class=num>{_bytes(out_b)}</td>"
        f"<td class=num>{_bytes(in_b + out_b)}</td><td class=ts>{_ts(seen)}</td></tr>"
        for address, name, out_b, in_b, seen in seed.traffic(days, STARTED)
    )

    return _page(
        "traffic",
        f"""<form class=filters method=get action="">
<input type=hidden name=p value=net>
<div class=field><label for=days>window</label><select id=days name=days>{options}</select></div>
<div class=field><button type=submit>apply</button></div>
</form>
<div class=panel><table>
<thead><tr><th>device</th><th>address</th><th class=num>in</th><th class=num>out</th>
<th class=num>total</th><th>last seen</th></tr></thead>
<tbody>{body}</tbody></table></div>
<p class=note>In the real house this is built from the router's flow records, joined to its
DHCP leases. An address with no lease still counts, and shows as the address.</p>""",
        visit,
        "net",
    )


def _median(values: list[float]) -> float | None:
    if not values:
        return None

    middle = len(values) // 2

    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def render_isp(month: str, visit: Visit | None) -> str:
    now = datetime.now(UTC)
    this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month = (this_month - timedelta(days=1)).replace(day=1)
    months = {f"{m:%Y-%m}": m for m in (this_month, last_month)}
    start = months.get(month, this_month)
    end = now if start == this_month else this_month - timedelta(seconds=1)
    days = seed.isp_month(start, end)
    counted = sum(d["counted"] for d in days)
    ok = sum(d["ok"] for d in days)
    downs = sorted(v for d in days for v in d["down"])
    ups = sorted(v for d in days for v in d["up"])
    uptime = 100.0 * ok / counted if counted else 0.0
    min_down, min_up = seed.CONTRACT
    med_down, med_up = _median(downs), _median(ups)
    tiles = "".join(
        (
            _tile(
                f"uptime ({counted} min sampled)",
                f"{uptime:.3f}%",
                "good" if uptime >= 99.9 else "bad",
            ),
            _tile("ISP downtime", "0 min", "good"),
            _tile("link downtime", f"{sum(d['link_down'] for d in days)} min"),
            _tile("ours (excluded)", f"{sum(d['lan_down'] for d in days)} min"),
            _tile(
                f"median down / min {min_down}",
                f"{med_down:.0f} Mb/s",
                "good" if med_down >= min_down else "bad",
            ),
            _tile(
                f"median up / min {min_up}",
                f"{med_up:.0f} Mb/s",
                "good" if med_up >= min_up else "bad",
            ),
        )
    )
    outages = (
        "".join(
            f"<tr><td class=ts>{_ts(d['outage_at'])}</td><td class=num>{minutes}</td>"
            f'<td><span class="pill {"skipped" if cause == "lan_down" else "error"}">'
            f"{cause}</span></td></tr>"
            for d in days
            for cause, minutes in (("lan_down", d["lan_down"]), ("link_down", d["link_down"]))
            if minutes >= 2
        )
        or "<tr><td colspan=3 class=muted>no outage ≥2 min</td></tr>"
    )

    def day_row(d: dict) -> str:
        uptime = f"{100.0 * d['ok'] / d['counted']:.2f}%" if d["counted"] else "—"

        return (
            f"<tr><td class=ts>{d['day']:%Y-%m-%d}</td><td class=num>{len(d['down'])}</td>"
            f'<td class="num{" bad" if d["failed"] else ""}">{d["failed"]}</td>'
            f"<td class=num>{_median(d['down']):.0f}</td><td class=num>{min(d['down']):.0f}</td>"
            f"<td class=num>{_median(d['up']):.0f}</td><td class=num>{uptime}</td>"
            f"<td class=num>{d['link_down'] + d['isp_down']}</td></tr>"
        )

    per_day = "".join(day_row(d) for d in days)
    options = "".join(
        f'<option value="{key}"{" selected" if months[key] == start else ""}>{key}</option>'
        for key in months
    )

    return _page(
        "ISP",
        f"""<form class=filters method=get action="">
<input type=hidden name=p value=isp>
<div class=field><label for=month>month</label><select id=month name=month>{options}</select></div>
<div class=field><button type=submit>show</button></div>
</form>
<div class=tiles>{tiles}</div>
<h2>outages (≥2 min, newest first)</h2>
<div class=panel><table>
<thead><tr><th>start</th><th class=num>minutes</th><th>cause</th></tr></thead>
<tbody>{outages}</tbody></table></div>
<h2>per day</h2>
<div class=panel><table>
<thead><tr><th>day</th><th class=num>tests</th><th class=num>failed</th><th class=num>med down</th>
<th class=num>worst</th><th class=num>med up</th><th class=num>uptime</th>
<th class=num>down min</th></tr></thead>
<tbody>{per_day}</tbody></table></div>
<p class=note>{_esc(seed.ISP_NAME)} is invented, and so are its numbers. A minute where the house's
own network was down is counted as ours and left out of the provider's uptime.</p>""",
        visit,
        "isp",
    )


def render_log(
    topic: str,
    status: str,
    sev: str,
    dfrom: str,
    dto: str,
    limit: int,
    offset: int,
    visit: Visit | None,
) -> str:
    matched = [row for row in _rows(visit) if _matches(row, topic, status, sev, dfrom, dto)]
    shown = matched[offset : offset + limit]

    def href(page: int) -> str:
        params = (
            ("p", "log"),
            ("topic", topic),
            ("status", status),
            ("sev", sev),
            ("from", dfrom),
            ("to", dto),
            ("limit", limit),
            ("offset", (page - 1) * limit),
        )

        return "?" + _esc(urlencode([(k, v) for k, v in params if v not in ("", 0)]))

    pages = max(1, -(-len(matched) // limit))
    asked = min(pages, offset // limit + 1)
    links, previous = [], 0

    for page in sorted({1, pages} | {p for p in range(asked - 2, asked + 3) if 1 <= p <= pages}):
        if page - previous > 1:
            links.append("<span class=gap>…</span>")

        links.append(
            f"<span class=cur>{page}</span>"
            if page == asked and shown
            else f'<a href="{href(page)}">{page}</a>'
        )
        previous = page

    first, last = (offset + 1, offset + len(shown)) if shown else (0, 0)
    pager = (
        f"<div class=pager><span class=range>rows {first}–{last} of {len(matched)}</span>"
        f"{''.join(links) if pages > 1 else ''}</div>"
    )

    body_rows = []

    for row in shown:
        sev_cell = sev_pill(row.severity, row.status)
        detail = _esc(json.dumps(row.detail))
        body_rows.append(
            f'<tr class=logrow data-detail="{detail}"><td class=ts>{_ts(row.ts)}</td>'
            f"<td>{_esc(row.topic)}</td><td>{_esc(row.task)}</td>"
            f'<td><span class="pill {_esc(row.status)}">{_esc(row.status)}</span></td>'
            f"<td>{sev_cell}</td><td>{_esc(row.summary)}</td>"
            f"<td class=exp>{'▸' if row.detail else ''}</td></tr>"
        )

    tbody = "".join(body_rows) or "<tr><td colspan=7 class=muted>no rows</td></tr>"

    return _page(
        "task log",
        f"""<form class=filters method=get action="">
<input type=hidden name=p value=log>
<div class=field><label for=topic>topic</label>
<select id=topic name=topic>{_options([""] + TOPICS, topic)}</select></div>
<div class=field><label for=status>status</label>
<select id=status name=status>{_options(STATUSES, status)}</select></div>
<div class=field><label for=sev>severity</label>
<select id=sev name=sev>{_options(SEVERITIES, sev, SEV_LABELS)}</select></div>
<div class=field><label for=from>from (UTC)</label>
<input id=from name=from type=date value="{_esc(dfrom)}"></div>
<div class=field><label for=to>to (UTC)</label>
<input id=to name=to type=date value="{_esc(dto)}"></div>
<div class=field><label for=limit>per page</label>
<input id=limit name=limit type=number min=1 max={LIMIT_MAX} value={limit}></div>
<div class=field><button type=submit>filter</button></div>
</form>
<div class=panel><table>
<thead><tr><th>ts</th><th>topic</th><th>task</th><th>status</th><th>severity</th>
<th>summary</th><th>detail</th></tr></thead>
<tbody>{tbody}</tbody></table>{pager}</div>""",
        visit,
        "log",
    )


def _render(query, visit: Visit | None) -> str:
    view = query.get("p", "")

    _refresh()

    if view == "net":
        days = _clamp(query.get("days", ""), 1, 30, 7)

        return render_net(days if days in NET_WINDOWS else 7, visit)

    if view == "isp":
        return render_isp(query.get("month", ""), visit)

    if view != "log":
        return render_dashboard(visit)

    topic, status, sev = query.get("topic", ""), query.get("status", ""), query.get("sev", "")

    return render_log(
        topic if topic in TOPICS else "",
        status if status in STATUSES else "",
        sev if sev in SEVERITIES else "",
        _date(query.get("from", "")),
        _date(query.get("to", "")),
        _clamp(query.get("limit", ""), 1, LIMIT_MAX, LIMIT_DEFAULT),
        # A visit adds one row at most, its stand-down.
        _clamp(query.get("offset", ""), 0, len(ROWS) + 1, 0),
        visit,
    )


def _stand_down(visit: Visit) -> RedirectResponse:
    alarm = _alarm(visit)
    now = datetime.now(UTC)

    if alarm:
        visit.stood_down = now
        visit.rows.insert(
            0,
            seed.Row(
                now,
                "alert",
                "yoman",
                "push",
                None,
                "DEFCON 5 · alarm stood down",
                {
                    "stood_down": f"{alarm.topic} · {alarm.task} · {alarm.summary}",
                    "by": "this visit",
                },
            ),
        )

    return RedirectResponse(f"{BASE}/s/{visit.id}/", status_code=303)


@app.get(f"{BASE}/", response_class=HTMLResponse)
async def page(request: Request) -> str:
    return _render(request.query_params, None)


@app.get(f"{BASE}/s/{{key}}/", response_model=None)
async def visit_page(key: str, request: Request) -> HTMLResponse | RedirectResponse:
    visit = _visit(key)

    if not visit:
        return RedirectResponse(f"{BASE}/", status_code=303)

    return HTMLResponse(_render(request.query_params, visit))


@app.post(f"{BASE}/stand-down", response_model=None)
async def stand_down_new() -> HTMLResponse | RedirectResponse:
    now = datetime.now(UTC)
    _sweep(now)

    if len(_visits) >= SESSION_CAP:
        busy = (
            '<div class="flash error">The demo is busy. Try the stand-down again in a minute.</div>'
        )

        return HTMLResponse(_page("busy", busy, None), status_code=503)

    visit = Visit(secrets.token_urlsafe(16), now)
    _visits[visit.id] = visit

    return _stand_down(visit)


@app.post(f"{BASE}/s/{{key}}/stand-down")
async def stand_down(key: str) -> RedirectResponse:
    visit = _visit(key)

    if not visit:
        return RedirectResponse(f"{BASE}/", status_code=303)

    return _stand_down(visit)
