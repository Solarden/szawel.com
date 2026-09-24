import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from demo.yoman import app as demo
from demo.yoman import seed

LOG = f"{demo.BASE}/"


@pytest.fixture
def client():
    with TestClient(demo.app) as connected:
        yield connected


def rows_on(page: str) -> list[str]:
    return re.findall(r"<tr class=logrow.*?</tr>", page, re.DOTALL)


def test_the_log_shows_the_newest_page_of_rows(client):
    page = client.get(LOG, params={"p": "log"}).text

    assert len(rows_on(page)) == demo.LIMIT_DEFAULT
    assert f"of {len(demo.ROWS)}</span>" in page


def test_a_topic_filter_shows_that_topic_alone(client):
    page = client.get(LOG, params={"p": "log", "topic": "isp", "limit": 500}).text
    topics = {re.findall(r"<td>([^<]*)</td>", row)[0] for row in rows_on(page)}

    assert topics == {"isp"}


def test_the_open_filter_leaves_out_pushes(client):
    page = client.get(LOG, params={"p": "log", "sev": "open", "limit": 500}).text
    shown = rows_on(page)

    assert shown
    assert not any('class="pill push"' in row for row in shown)


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "abc"},
        {"limit": "99999"},
        {"offset": "-5"},
        {"from": "not-a-date"},
        {"topic": "<script>"},
        {"sev": "panic"},
        {"p": "net", "days": "9999"},
        {"p": "isp", "month": "1999-01"},
        {"p": "nowhere"},
    ],
)
def test_a_malformed_filter_renders_the_page_rather_than_an_error(client, params):
    response = client.get(LOG, params=params)

    assert response.status_code == 200
    assert "<script>" not in response.text.replace(f'<script src="{demo.BASE}', "")


@pytest.mark.parametrize("view", ["", "log", "isp", "net"])
def test_no_page_needs_what_the_box_content_security_policy_forbids(client, view):
    page = client.get(LOG, params={"p": view}).text

    # The box serves script-src 'self' and style-src 'self' with no 'unsafe-inline'.
    assert "<style" not in page
    assert re.findall(r"<script(?![^>]*\bsrc=)", page) == []
    assert re.search(r"\son[a-z]+=", page) is None


def test_the_static_files_are_served(client):
    for name in ("yoman.css", "yoman.js"):
        assert client.get(f"{demo.BASE}/static/{name}").status_code == 200


def test_the_seed_is_recent_and_only_uses_its_own_invented_topics():
    now = datetime.now(UTC)
    rows = seed.generate(now)
    invented = {unit[0] for unit in seed.UNITS} | {"note"}

    assert {row.topic for row in rows} <= invented
    assert all(now - timedelta(days=seed.DAYS) <= row.ts <= now for row in rows)


def test_the_dashboard_puts_the_standing_alarm_and_the_quiet_unit_first(client):
    page = client.get(LOG).text
    needs_a_look = page.split("<h2>quiet")[0]

    assert "DEFCON 1" in needs_a_look
    assert f"certs / {seed.STALE_TASK}" in needs_a_look
    assert "stale" in needs_a_look


def test_the_traffic_page_lists_every_device_and_names_the_unleased_one_by_address(client):
    page = client.get(LOG, params={"p": "net", "days": "30"}).text
    unleased = next(host for name, host, _ in seed.DEVICES if name is None)

    assert page.count("<td class=num>") == 3 * len(seed.DEVICES)
    assert f"<td>{seed.LAN}.{unleased}</td>" in page


def test_the_isp_page_counts_nothing_against_the_provider(client):
    page = client.get(LOG, params={"p": "isp"}).text

    assert "ISP downtime" in page
    assert '<div class="tile good"><div class=k>ISP downtime</div><div class=v>0 min</div>' in page


@pytest.fixture(autouse=True)
def no_visits_left_over():
    demo._visits.clear()
    yield
    demo._visits.clear()


def stand_down(client) -> str:
    response = client.post(f"{demo.BASE}/stand-down", follow_redirects=False)

    assert response.status_code == 303

    return response.headers["location"]


def test_standing_down_moves_the_visit_to_its_own_address(client):
    visit = stand_down(client)

    assert re.fullmatch(rf"{demo.BASE}/s/[A-Za-z0-9_-]{{22}}/", visit)


def test_a_stand_down_clears_the_alarm_for_that_visit_alone(client):
    visit = stand_down(client)
    own = client.get(visit).text
    everyone_else = client.get(LOG).text

    assert "no alarm standing" in own
    assert "DEFCON 1" not in own.split("<h2>quiet")[0]
    assert "stand down</button>" in everyone_else


def test_the_stand_down_is_written_to_the_visits_own_log(client):
    visit = stand_down(client)
    own = rows_on(client.get(visit, params={"p": "log"}).text)
    everyone_else = rows_on(client.get(LOG, params={"p": "log"}).text)

    assert "alarm stood down" in own[0]
    assert not any("alarm stood down" in row for row in everyone_else)


def test_the_pages_of_a_visit_link_within_it(client):
    page = client.get(stand_down(client)).text
    nav = re.search(r"<nav>.*?</nav>", page).group(0)

    assert re.findall(r'href="([^"]+)"', nav) == ["./", "./?p=log", "./?p=isp", "./?p=net"]


@pytest.mark.parametrize("key", ["x" * 22, "dots.are.not.in.an.id!!", "short"])
def test_an_unknown_visit_falls_back_to_the_shared_demo(client, key):
    response = client.get(f"{demo.BASE}/s/{key}/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == LOG


def test_a_visit_is_forgotten_after_its_idle_time(client):
    visit = stand_down(client)
    key = visit.rstrip("/").rsplit("/", 1)[1]
    demo._visits[key].seen -= demo.SESSION_TTL + timedelta(seconds=1)

    response = client.get(visit, follow_redirects=False)

    assert response.headers["location"] == LOG
    assert key not in demo._visits


def test_a_full_house_says_busy_instead_of_growing(client, monkeypatch):
    monkeypatch.setattr(demo, "SESSION_CAP", 2)
    stand_down(client)
    stand_down(client)

    response = client.post(f"{demo.BASE}/stand-down", follow_redirects=False)

    assert response.status_code == 503
    assert len(demo._visits) == 2


@pytest.mark.parametrize("minutes_into_the_day", [0, 1, 9, 14, 15])
def test_the_isp_day_still_under_way_counts_only_minutes_that_have_passed(minutes_into_the_day):
    month_start = datetime(2026, 9, 1, tzinfo=UTC)
    now = datetime(2026, 9, 17, tzinfo=UTC) + timedelta(minutes=minutes_into_the_day)
    today = seed.isp_month(month_start, now)[0]

    assert today["lan_down"] + today["link_down"] <= minutes_into_the_day
    assert today["counted"] >= 0 and today["ok"] >= 0
    assert today["outage_at"] <= now


def test_an_old_house_is_regenerated_on_the_next_request(client, monkeypatch):
    monkeypatch.setattr(demo, "STARTED", demo.STARTED - demo.REFRESH - timedelta(minutes=1))
    stale = demo.ROWS

    client.get(LOG)

    assert demo.ROWS is not stale
    assert datetime.now(UTC) - demo.STARTED < timedelta(minutes=1)


def test_a_visits_log_stays_newest_first_across_a_refresh(client, monkeypatch):
    visit = stand_down(client)
    monkeypatch.setattr(demo, "STARTED", demo.STARTED - demo.REFRESH - timedelta(minutes=1))
    key = visit.rstrip("/").rsplit("/", 1)[1]

    client.get(visit)
    merged = demo._rows(demo._visits[key])

    assert merged == sorted(merged, key=lambda row: row.ts, reverse=True)
