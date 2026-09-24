"""The socket layer: one WebSocket endpoint, the turn loop, and the wire format.

It imports `rules.py` and adds nothing to it. The rules live there; the clock, the socket and
the decision of who a message is allowed to be belong here.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocketState

from game.rules import (
    GameState,
    Move,
    Player,
    Rejection,
    apply_move,
    greedy_ai,
    legal_moves,
    score,
    to_dict,
    winner,
)
from game.server import leaderboard, matches

# The opponent thinking. Never reported as latency — a real local turn resolves in about a
# millisecond, and an instant reply reads as nothing having happened.
AI_PAUSE_SECONDS = 0.26

_HERE = Path(__file__).resolve().parent
DEV_PAGE = _HERE / "dev.html"
CLIENT_DIR = _HERE.parent / "client"

# Stat'd per HELLO, never cached: the switch is a file precisely so that `touch` and `rm` take
# effect without a restart.
#
# A blank reads as unset because Path("") is Path("."), which exists — so blanking the value in an
# EnvironmentFile would switch the site off permanently and silently.
DISABLED_FLAG = Path(os.environ.get("PLAY_DISABLED_FILE", "").strip() or "/var/lib/play/disabled")

# New matches one address may mint inside the window below. Blank reads as unset, as above.
MINT_LIMIT = int(os.environ.get("PLAY_MINT_LIMIT", "").strip() or "10")
MINT_WINDOW_SECONDS = 60

if MINT_LIMIT < 1:
    # Zero refuses every new visitor while /health still answers ok; see MAX_MATCHES.
    raise ValueError(f"PLAY_MINT_LIMIT must be at least 1, got {MINT_LIMIT}")

# Addresses, and only here. Nothing in this dict is written down, and a minute after an
# address stops minting matches it is gone from the process entirely.
_minted: dict[str, list[float]] = {}


def _may_mint(address: str) -> bool:
    """One rolling window per address, with the whole table swept on the way through.

    It bounds HELLO spam minting matches in 4 GB of RAM, the ceiling `MAX_MATCHES` holds from the
    other side. Not an anti-farming control: the weekly reset answers farming.

    ponytail: the sweep is O(addresses) and runs only on the mint path, which is the path
    being limited. 10 a minute is a guess — tune it once something real has tripped it.
    """
    cutoff = time.monotonic() - MINT_WINDOW_SECONDS

    # The sweep is the TTL. Without it this dict would be a log of every address ever seen.
    for known, times in list(_minted.items()):
        kept = [moment for moment in times if moment > cutoff]

        if kept:
            _minted[known] = kept
        else:
            del _minted[known]

    return len(_minted.get(address, ())) < MINT_LIMIT


def _note_mint(address: str) -> None:
    """Called only once a match exists, so a refusal never counts against the window.

    Charging at the check instead would bill a visitor for matches they did not get: a
    full registry would spend their whole window on AT_CAPACITY refusals and then lock
    them out for a minute after capacity frees.
    """
    _minted.setdefault(address, []).append(time.monotonic())


class Transport(Enum):
    """Refusals the engine cannot express, because `rules.py` must not know a socket exists."""

    PROTOCOL = auto()
    DISABLED = auto()
    AT_CAPACITY = auto()
    MISSING_ENVELOPE = auto()
    BAD_SIGNATURE = auto()
    REPLAYED_NONCE = auto()
    RATE_LIMITED = auto()


# Envelope violations one socket sends before the opponent starts taking an extra turn a round,
# and one more turn for every further ten.
#
# ponytail: 10 is a guess — low enough that a deliberate poke finds it, high enough that an
# honest client hitting a bug does not. Tune once something real has tripped it.
UNSHACKLE_AFTER = 10

# PROTOCOL is deliberately absent. `Session.open` returns it on a frame whose signature
# verified, which is a client holding the key and sending a malformed command — a bug
# rather than a poke, and the taxonomy is what tells the two apart.
ENVELOPE_VIOLATIONS = frozenset(
    {Transport.MISSING_ENVELOPE, Transport.BAD_SIGNATURE, Transport.REPLAYED_NONCE}
)


@dataclass(slots=True)
class Session:
    """The transport session: a key minted for this connection, the nonce it has reached, and
    the forged frames it has sent.

    Not the match session: `player_token` outlives the socket, this dies with it, so a reload
    re-shackles the opponent with nothing to reset.
    """

    key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    last_seq: int = 0
    violations: int = 0

    @property
    def extra_turns(self) -> int:
        return self.violations // UNSHACKLE_AFTER

    def note(self, refusal: Transport) -> dict:
        """The fields a refusal carries when it is the one that widens the opponent's round.

        Empty on every other refusal, so the news is announced once per step up rather than
        repeated under every later violation.
        """
        if refusal not in ENVELOPE_VIOLATIONS:
            return {}

        self.violations += 1

        if self.violations % UNSHACKLE_AFTER:
            return {}

        return {"violations": self.violations, "extra_turns": self.extra_turns}

    def open(self, message: dict) -> dict | Transport:
        """The signed command inside the envelope, or the reason it is not one."""
        body, signature = message.get("body"), message.get("sig")

        if type(body) is not str or type(signature) is not str:
            return Transport.MISSING_ENVELOPE

        # The bytes the client hashed, not a re-serialisation of them: the body crosses as an
        # opaque string, so the outer parse hands back exactly what was signed.
        signed = body.encode(errors="surrogatepass")  # strict raises on a lone surrogate
        expected = hmac.new(self.key, signed, hashlib.sha256).hexdigest()

        # isascii first: compare_digest refuses a string that is not one, and a hex digest is.
        if not signature.isascii() or not hmac.compare_digest(expected, signature):
            return Transport.BAD_SIGNATURE

        # Parsed only now, so a forged signature costs one hash — and the counter below is never
        # read out of a frame that did not verify.
        try:
            command = json.loads(body)
        except (ValueError, RecursionError):
            return Transport.PROTOCOL

        if not isinstance(command, dict) or type(command.get("seq")) is not int:
            return Transport.PROTOCOL

        if command["seq"] <= self.last_seq:
            return Transport.REPLAYED_NONCE

        self.last_seq = command["seq"]

        return command


# No /docs, /redoc or /openapi.json: nothing here is an API for anyone else, and they would be
# reachable wherever this process is served.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/")
async def dev_page() -> FileResponse:
    return FileResponse(DEV_PAGE)


@app.get("/health")
async def health() -> dict:
    # 200 while disabled too. The switch is deliberate, and a watcher that pages on a
    # deliberate switch is a watcher you learn to ignore.
    return {"ok": True, "disabled": DISABLED_FLAG.exists(), "matches": matches.count()}


app.mount("/client", StaticFiles(directory=CLIENT_DIR), name="client")


def _digest(state: dict) -> str:
    """A short fingerprint of the state as it goes out, so two tabs can be compared by eye.

    Not a security control: the server computes it and the client prints it. What it catches
    is a client showing a stale board.
    """
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


def _view(match: matches.Match) -> dict:
    state = to_dict(match.state)

    return {
        "state": state,
        "digest": _digest(state),
        "legal_moves": sorted(legal_moves(match.state, Player.YOU)),
        "clients": len(match.sockets),
    }


def _state_message(
    match: matches.Match, *, last: dict | None = None, started: float | None = None
) -> dict:
    return {
        "type": "STATE",
        "last": last,
        "server_ms": None if started is None else round((time.perf_counter() - started) * 1000, 3),
        **_view(match),
    }


def _board() -> dict:
    """The board as it goes into a message, or nothing at all when the store cannot answer.

    Left out rather than sent empty: an empty board says this week has no scores, and a
    store that just failed has not said that.
    """
    try:
        return {"leaderboard": leaderboard.top()}
    except sqlite3.Error:
        logging.exception("the leaderboard could not be read")

        return {}


def _filed(match: matches.Match) -> dict:
    """The handle this match's score went on the board under, or nothing."""
    try:
        return {"handle": leaderboard.record(score(match.state, Player.YOU))}
    except sqlite3.Error:
        logging.exception("the leaderboard refused a score")

        return {}


def _over_message(match: matches.Match) -> dict:
    champion = winner(match.state)

    return {
        "type": "OVER",
        "winner": None if champion is None else champion.value,
        "scores": to_dict(match.state)["scores"],
        **_board(),
    }


def _rejected(reason: Rejection | Transport, *, tile: object = None, seq: object = None) -> dict:
    # Both taxonomies reach the wire as the member name: no mapping to keep in step.
    return {"type": "REJECTED", "reason": reason.name, "tile": tile, "seq": seq}


async def _receive(socket: WebSocket) -> dict | None:
    """The next frame as a JSON object, or None for anything that is not one."""
    frame = await socket.receive()

    if frame["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(frame.get("code", 1000), frame.get("reason"))

    # A binary frame carries no "text", and starlette's receive_text would raise KeyError on it.
    # An unhandled exception is the one answer a page promising reason codes must never give.
    text = frame.get("text")

    if text is None:
        return None

    # RecursionError as well as ValueError: deeply nested input exhausts the decoder rather
    # than failing it, and both mean the same thing here — this is not a message.
    try:
        message = json.loads(text)
    except (ValueError, RecursionError):
        return None

    return message if isinstance(message, dict) else None


async def _send(socket: WebSocket, message: dict) -> bool:
    """False once the socket has closed under us, which a direct reply can ignore because
    `play`'s finally removes it anyway. A send failing any other way is a bug, and raises.
    """
    try:
        await socket.send_json(message)
    except (WebSocketDisconnect, RuntimeError):
        if socket.application_state is WebSocketState.CONNECTED:
            raise

        return False

    return True


async def _broadcast(match: matches.Match, message: dict, skip: WebSocket | None = None) -> None:
    for socket in list(match.sockets):
        if socket is skip:
            continue

        # A peer that cannot be reached leaves the match. It must not take the handler of
        # whoever caused the broadcast down with it — that socket has done nothing wrong.
        try:
            reached = await _send(socket, message)
        except (WebSocketDisconnect, RuntimeError):
            reached = False

        if not reached:
            match.sockets.discard(socket)


async def _opponent_turn(match: matches.Match, state: GameState, *, seized: bool = False) -> None:
    """One opponent move, played from the state handed in.

    `match.state` is only ever assigned what `apply_move` returns. A hand-built state stored
    before the awaits below would survive one of them raising, and a match whose turn says
    SERVER outside the loop in `_handle_move` answers every later move NOT_YOUR_TURN — through
    reconnects, until the idle sweep reaches it.
    """
    await asyncio.sleep(AI_PAUSE_SECONDS)
    # Started after the pause: see AI_PAUSE_SECONDS.
    started = time.perf_counter()
    tile = greedy_ai(state, Player.SERVER, match.rng)
    settled = apply_move(state, Move(Player.SERVER, tile))

    # Unreachable while the opponent only ever picks from legal_moves, a seized turn included:
    # what is seized is the turn, never the move. A Rejection stored here would poison the
    # match for every socket on it.
    if isinstance(settled, Rejection):
        raise RuntimeError(f"the opponent proposed an illegal move: {settled.name}")

    match.state = settled
    theirs = {"by": Player.SERVER.value, "tile": tile, "seq": None, "seized": seized}
    await _broadcast(match, _state_message(match, last=theirs, started=started))


async def _handle_move(
    match: matches.Match, socket: WebSocket, session: Session, message: dict
) -> None:
    # `Session.open` has already required seq to be an integer, because the replay check needed it.
    tile, seq = message.get("tile"), message["seq"]

    # The engine raises on a tile that is not an integer.
    if type(tile) is not int:  # not isinstance: bool subclasses int
        await _send(socket, _rejected(Transport.PROTOCOL, tile=tile, seq=seq))

        return

    # Held across the pause below: during the opponent's turn there is nothing a second tab
    # can validly do, so blocking it costs a rejection it was going to get anyway.
    # ponytail: per-match lock; nothing here needs more.
    async with match.lock:
        matches.touch(match)
        started = time.perf_counter()

        # The engine enforces turn order but cannot know who sent a packet, so binding a socket
        # to its side is this layer's job alone.
        outcome = apply_move(match.state, Move(Player.YOU, tile))

        if isinstance(outcome, Rejection):
            await _send(socket, _rejected(outcome, tile=tile, seq=seq))

            return

        match.state = outcome
        mine = {"by": Player.YOU.value, "tile": tile, "seq": seq}
        await _broadcast(match, _state_message(match, last=mine, started=started))

        # A loop, not an if: the engine's auto-pass hands the server consecutive turns whenever
        # the player is sealed off.
        while not match.state.over and match.state.turn is Player.SERVER:
            await _opponent_turn(match, match.state)

            # Seizes turns, never moves: each tile still goes through `apply_move`. Keyed to the
            # mover's socket, so nobody else's opponent changes.

            # ponytail: no cap on the count — the legal-moves guard is the ceiling, and it
            # is the board's, so the worst case is a match ending inside one round.
            for _ in range(session.extra_turns):
                # Nothing to seize: the engine's auto-pass has already left the turn here, so
                # the loop above takes it and the console does not call it a seizure.
                if match.state.turn is Player.SERVER:
                    break

                # A hard condition, not greedy_ai's assert, which python -O strips: the
                # opponent can legitimately be sealed off while the player still has moves.
                if not legal_moves(match.state, Player.SERVER):
                    break

                await _opponent_turn(match, replace(match.state, turn=Player.SERVER), seized=True)

        if match.state.over:
            # The one place a match ends, so the one place a score is filed. Its own statement
            # because the row has to be written before `_over_message` reads the board back,
            # or the player's own score is missing from the message announcing it.
            filed = _filed(match)
            await _broadcast(match, _over_message(match) | filed)


@app.websocket("/ws/play")
async def play(socket: WebSocket) -> None:
    await socket.accept()
    matches.sweep()
    session = Session()
    match: matches.Match | None = None

    try:
        while True:
            message = await _receive(socket)

            if message is None:
                # Nothing parsed, so there is no seq to quote back.
                await _send(socket, _rejected(Transport.PROTOCOL))

                continue

            kind = message.get("type")

            if kind == "HELLO" and match is None:
                match = matches.get(message.get("match_id"), message.get("player_token"))

                # All three refusals below reach only a new match. The resume above has
                # already run, and ending a game in progress is what none of them is for —
                # so a reconnect is never counted against the window either.
                if match is None:
                    if DISABLED_FLAG.exists():
                        await _send(socket, _rejected(Transport.DISABLED))

                        break

                    address = socket.client.host if socket.client else "unknown"

                    if not _may_mint(address):
                        await _send(socket, _rejected(Transport.RATE_LIMITED))

                        break

                    match = matches.create()

                    if match is None:
                        await _send(socket, _rejected(Transport.AT_CAPACITY))

                        break

                    _note_mint(address)

                match.sockets.add(socket)
                # Paired with the decrement in `finally`; `Match.holders` says why it exists.
                match.holders += 1
                matches.touch(match)
                await _send(
                    socket,
                    {
                        "type": "WELCOME",
                        "match_id": match.id,
                        "player_token": match.player_token,
                        "session_key": session.key.hex(),
                        # WELCOME only: `_view` also feeds every STATE, and the board changes
                        # once per match.
                        **_board(),
                        **_view(match),
                    },
                )
                # The others learn the client count changed; the joiner just had it in WELCOME.
                await _broadcast(match, _state_message(match), skip=socket)

                continue

            # Everything after the handshake is signed. HELLO above is the one command that
            # cannot be: the key it answers with did not exist when it was sent.
            opened = session.open(message)

            if isinstance(opened, Transport):
                # No tile or seq echoed: one reply shape beats a field that comes and goes.
                await _send(socket, _rejected(opened) | session.note(opened))

                continue

            if opened.get("type") == "MOVE" and match is not None:
                await _handle_move(match, socket, session, opened)

            else:
                await _send(
                    socket,
                    _rejected(Transport.PROTOCOL, tile=opened.get("tile"), seq=opened["seq"]),
                )

    except WebSocketDisconnect:
        pass

    finally:
        if match is not None:
            match.holders -= 1
            match.sockets.discard(socket)
            # The idle clock starts when the last socket leaves, not at the last message.
            matches.touch(match)
            await _broadcast(match, _state_message(match))
