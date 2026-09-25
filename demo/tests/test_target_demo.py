import re

import pytest
from fastapi.testclient import TestClient

from demo.target import app as demo
from demo.target import seed
from demo.target.agreement import agreement

HELD_OUT = demo.SESSIONS[seed.HELD_OUT]


@pytest.fixture
def client():
    with TestClient(demo.app) as connected:
        yield connected


def yolo_at(session: seed.Session, conf: float) -> list[seed.Point]:
    return [point for point, confidence in session.yolo if confidence >= conf]


def counts(truth: list[seed.Point], found: list[seed.Point]) -> tuple[int, int, int]:
    result = agreement(truth, found, demo.TOL_PX)

    return result.matched, result.missed, result.spurious


@pytest.mark.parametrize(
    ("found", "expected"),
    [
        (lambda: HELD_OUT.cv_blob, (23, 14, 0)),
        (lambda: yolo_at(HELD_OUT, 0.10), (34, 3, 10)),
        (lambda: yolo_at(HELD_OUT, 0.25), (32, 5, 0)),
    ],
    ids=["cv_blob", "yolo@0.10", "yolo@0.25"],
)
def test_the_held_out_target_reproduces_the_case_study_counts(found, expected):
    assert len(HELD_OUT.truth) == 37
    assert counts(HELD_OUT.truth, found()) == expected


def test_the_compare_page_shows_the_counts_it_computes(client):
    page = client.get(f"{demo.BASE}/compare/{seed.HELD_OUT}", params={"conf": "0.25"}).text
    chosen = re.search(r"<tr class=on><td>yolo ≥ 0.25</td>(.*?)</tr>", page).group(1)

    assert re.findall(r"<td class=num>([^<]*)</td>", chosen)[:4] == ["32", "32", "5", "0"]


@pytest.mark.parametrize("conf", demo.CONFS)
def test_every_hole_is_either_matched_or_missed(conf):
    for session in demo.SESSIONS.values():
        matched, missed, _ = counts(session.truth, yolo_at(session, conf))

        assert matched + missed == len(session.truth)


@pytest.mark.parametrize("raw", ["", "abc", "0.3", "nan", "-1", "1e9"])
def test_a_threshold_off_the_list_falls_back_to_the_default(client, raw):
    page = client.get(f"{demo.BASE}/compare/{seed.HELD_OUT}", params={"conf": raw}).text

    assert f"<tr class=on><td>yolo ≥ {demo.CONF_DEFAULT:.2f}</td>" in page


@pytest.mark.parametrize("path", ["/session/999", "/session/abc", "/session/²", "/compare/0"])
def test_an_unknown_session_goes_back_to_the_trend(client, path):
    response = client.get(f"{demo.BASE}{path}", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"{demo.BASE}/"


def pages() -> list[str]:
    return ["/", *(f"/session/{sid}" for sid in demo.SESSIONS), f"/compare/{seed.HELD_OUT}"]


@pytest.mark.parametrize("path", pages())
def test_no_page_needs_what_the_box_content_security_policy_forbids(client, path):
    response = client.get(f"{demo.BASE}{path}")

    assert response.status_code == 200
    # The box serves script-src 'self' and style-src 'self' with no 'unsafe-inline'.
    assert "<script" not in response.text
    assert "<style" not in response.text
    assert re.search(r"\s(on[a-z]+|style)=", response.text) is None


def test_both_stylesheets_are_served(client):
    page = client.get(f"{demo.BASE}/").text

    hrefs = re.findall(r'<link rel=stylesheet href="([^"]+)"', page)

    assert len(hrefs) == 2
    assert all(client.get(href).status_code == 200 for href in hrefs)
