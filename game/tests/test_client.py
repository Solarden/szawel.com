"""Checks on what the domain serves that reading the panel in a browser would not catch."""

import re
from pathlib import Path

from game.rules import Rejection
from game.server.app import Transport

GAME = Path(__file__).resolve().parent.parent
CLIENT = GAME / "client"
# dev.html is not published, but P7 transplants its markup into the apex page, so a CDN
# reference parked there reaches the domain the long way round.
SCANNED = [CLIENT, GAME / "server" / "dev.html", GAME.parent / "index.html"]

# The one host the client is allowed to name: P3 points it at the game's own backend.
OWN_HOSTS = {"play.szawel.com"}

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
