"""The match registry: sockets, tokens and a clock.

Nothing here knows the rules — it stores a `GameState` and never asks it anything.
"""

import asyncio
import random
import secrets
import time
from dataclasses import dataclass, field

from game.rules import GameState, new_game

# Generous on purpose: this timer is the one that can delete a match someone is still using.
IDLE_SECONDS = 30 * 60


@dataclass(slots=True)
class Match:
    id: str
    player_token: str
    state: GameState
    rng: random.Random
    sockets: set = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_seen: float = field(default_factory=time.monotonic)


_matches: dict[str, Match] = {}


def create() -> Match:
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
        if not match.sockets and match.last_seen < cutoff:
            del _matches[match_id]
