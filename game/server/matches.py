"""The match registry: sockets, tokens and a clock.

Nothing here knows the rules — it stores a `GameState` and never asks it anything.
"""

import asyncio
import os
import random
import secrets
import time
from dataclasses import dataclass, field

from game.rules import GameState, new_game

# Generous on purpose: this timer is the one that can delete a match someone is still using.
IDLE_SECONDS = 30 * 60

# Every HELLO without a resumable token mints a match, and nothing authenticates a HELLO.
#
# A blank reads as unset, because int("") would take the box down over a value someone commented
# out. A non-blank value that is not a number still raises: "20O" is a typo to be told about.
MAX_MATCHES = int(os.environ.get("PLAY_MAX_MATCHES", "").strip() or "200")

if MAX_MATCHES < 1:
    # Zero reads as "no limit" to anyone setting it, and does the opposite: every visitor is
    # refused while /health still answers ok, which looks nothing like a misconfiguration.
    raise ValueError(f"PLAY_MAX_MATCHES must be at least 1, got {MAX_MATCHES}")


@dataclass(slots=True)
class Match:
    id: str
    player_token: str
    state: GameState
    rng: random.Random
    sockets: set = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_seen: float = field(default_factory=time.monotonic)
    # Handlers currently running against this match. Not derivable from `sockets`: a broadcast
    # drops a socket it cannot reach, so the set empties while that socket's handler is still
    # between awaits holding the match. Deleting it there would orphan a live handler.
    holders: int = 0


_matches: dict[str, Match] = {}


def count() -> int:
    return len(_matches)


def _unheld(match: Match) -> bool:
    """Nobody connected and no handler running — the one predicate both removals may use."""
    return not match.sockets and not match.holders


def _evict_least_recently_seen() -> bool:
    """Drop the idlest unheld match; False when every match is still in use.

    Without it the cap is a weapon: filling the table with open sockets denies everyone else a
    game, and costs an attacker less than the memory exhaustion the cap exists to stop.
    """
    idle = [match for match in _matches.values() if _unheld(match)]

    if not idle:
        return False

    del _matches[min(idle, key=lambda match: match.last_seen).id]

    return True


def create() -> Match | None:
    """A new match, or None when every slot holds a match someone is still connected to."""
    if count() >= MAX_MATCHES and not _evict_least_recently_seen():
        return None

    match = Match(
        id=secrets.token_urlsafe(8),
        player_token=secrets.token_urlsafe(16),
        state=new_game(),
        rng=random.Random(),
    )
    _matches[match.id] = match

    return match


def get(match_id: object, player_token: object) -> Match | None:
    """The match this pair names, or None — an unknown or expired pair is not an error."""
    match = _matches.get(match_id) if isinstance(match_id, str) else None

    # A token this server never minted cannot be right, and compare_digest refuses a string
    # that is not ASCII — which JSON will happily decode, as a lone surrogate.
    if match is None or not isinstance(player_token, str) or not player_token.isascii():
        return None

    # compare_digest, not ==: == leaks its answer one byte at a time, and this is a credential.
    if not secrets.compare_digest(match.player_token, player_token):
        return None

    return match


def touch(match: Match) -> None:
    match.last_seen = time.monotonic()


def sweep() -> None:
    # Idle is no sockets AND no messages. On messages alone this deletes the match of anyone
    # who leaves the tab open and thinks, and their next move fails on a live connection.
    cutoff = time.monotonic() - IDLE_SECONDS

    for match_id, match in list(_matches.items()):
        if _unheld(match) and match.last_seen < cutoff:
            del _matches[match_id]
