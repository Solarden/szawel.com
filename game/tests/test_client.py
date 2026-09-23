"""Checks on what the domain serves that reading the panel in a browser would not catch."""

import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import pytest

from game.rules import Rejection
from game.server.app import Transport

GAME = Path(__file__).resolve().parent.parent
CLIENT = GAME / "client"
# dev.html is not published, but P7 transplants its markup into the apex page, so a CDN
# reference parked there reaches the domain the long way round.
SCANNED = [CLIENT, GAME / "server" / "dev.html", GAME.parent / "index.html"]

# The hosts the client is allowed to name: P3 points it at the game's own backend, and the
# apex's og: tags must name the page's own origin, because link previews resolve nothing relative.
OWN_HOSTS = {"play.szawel.com", "www.szawel.com"}

# An <a> points wherever the page likes and fetches nothing, so its opening tags come out
# before the scan.
# ponytail: strip anchors rather than name the attributes that fetch — an allow-list of
# src/href/url() misses whatever a later page reaches for, and misses it silently.
ANCHOR = re.compile(r"<a\b[^>]*>", re.IGNORECASE)


def test_the_client_labels_every_rejection_reason():
    source = (CLIENT / "widget.js").read_text()
    table = re.search(r"var REASONS = \{(.*?)\};", source, re.DOTALL)

    assert table, "widget.js has no REASONS table to check"

    labelled = set(re.findall(r"^\s*(\w+):", table.group(1), re.MULTILINE))

    # Equality, not a subset: a missing key is the hole P4's transport-side reasons arrive
    # through, and a stale one is a code the server can no longer send.
    assert labelled == set(Rejection.__members__) | set(Transport.__members__)


def test_the_client_loads_nothing_from_a_third_party():
    hosts = set()
    roots = [p for root in SCANNED for p in (sorted(root.rglob("*")) if root.is_dir() else [root])]
    read = [p for p in roots if p.suffix in {".js", ".css", ".html"}]

    # Anchored on "://" rather than "//", or every JS comment reads as a protocol-relative URL.
    for path in read:
        hosts |= set(re.findall(r"[a-z]+://([^/\s'\")]+)", ANCHOR.sub("", path.read_text())))

    # Named, because `hosts` is empty both when the client is clean and when a rename left this
    # scanning nothing — and a scan that quietly passes is worse than no scan.
    assert {p.name for p in read} >= {"widget.js", "widget.css", "dev.html", "index.html"}
    assert hosts <= OWN_HOSTS


def test_the_vendored_fonts_are_fonts_and_not_a_saved_error_page():
    faces = sorted((CLIENT / "fonts").glob("*.woff2"))

    assert faces

    # A 404 body saved under a .woff2 name fails silently behind font-display: swap.
    assert all(face.read_bytes()[:4] == b"wOF2" for face in faces)


# HTMLParser pushes a self-closed tag and pops it again, so nothing keeps void elements off the
# stack. An `<img>` written without the trailing slash leaks a frame, and the next `</div>` then
# closes that phantom instead — nesting everything after it one level too deep.
VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())


class _Ancestry(HTMLParser):
    """For every element carrying an id, the classes of the elements it is nested inside."""

    def __init__(self) -> None:
        super().__init__()
        self.open: list[list[str]] = []
        self.under: dict[str, set[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)

        if "id" in attributes:
            self.under[attributes["id"]] = {c for frame in self.open for c in frame}

        if tag not in VOID:
            self.open.append((attributes.get("class") or "").split())

    def handle_endtag(self, tag: str) -> None:
        if tag not in VOID and self.open:
            self.open.pop()


def _ancestry(path: Path) -> _Ancestry:
    parsed = _Ancestry()
    parsed.feed(path.read_text())

    return parsed


# el() takes the id directly; metric() takes it as a parameter and passes it on, which is the
# only other way the client reaches for one.
REACHED = re.compile(r"\b(?:el|metric)\('(\w+)'")


@pytest.mark.parametrize("page", ["server/dev.html", "../index.html"])
def test_every_page_carries_the_elements_the_client_reaches_for(page):
    parsed = _ancestry(GAME / page)
    wanted = set(REACHED.findall((CLIENT / "widget.js").read_text()))

    # Named, so a regex that stops matching fails here rather than passing an empty set.
    assert "mLag" in wanted and "start" in wanted

    # Both pages, not the apex against the harness: either one missing an element is a client
    # that throws at parse time and a widget frozen on its idle state with nothing said.
    assert wanted <= set(parsed.under)

    # Reached by class, not id: scrollEl is logEl.closest('.logwrap'), and line() dereferences
    # it on every row — a null throws inside the open handler, socket alive, console empty.
    assert "logwrap" in parsed.under["log"]


def test_the_apex_points_the_socket_at_the_box():
    apex = (GAME.parent / "index.html").read_text()
    configured = re.findall(r'data-endpoint="([^"]+)"', apex)

    assert [urlparse(url).hostname for url in configured] == ["play.szawel.com"]

    # The harness is served by the process it talks to, so it stays on the relative fallback.
    # The apex taking that branch dials Pages: fine in review, dead on the live page.
    assert "data-endpoint" not in (GAME / "server" / "dev.html").read_text()


def test_the_client_is_syntactically_valid():
    node = shutil.which("node")

    if node is None:
        pytest.skip("node is not installed")

    # Nothing else in this repo parses JS: ruff is Python-only and the hooks only fix whitespace,
    # so without this a syntax error reaches the live landing page with no gate firing.
    subprocess.run([node, "--check", str(CLIENT / "widget.js")], check=True)
