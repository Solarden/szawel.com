"""The socket layer: one WebSocket endpoint, the turn loop, and the wire format.

It imports `rules.py` and adds nothing to it. The rules live there; the clock, the socket and
the decision of who a message is allowed to be belong here.
"""

import asyncio
import hashlib
import json
import time
from enum import Enum, auto
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocketState

from game.rules import (
    Move,
    Player,
    Rejection,
    apply_move,
    greedy_ai,
    legal_moves,
    to_dict,
    winner,
)
from game.server import matches

# The opponent thinking. Never reported as latency — a real local turn resolves in about a
# millisecond, and an instant reply reads as nothing having happened.
AI_PAUSE_SECONDS = 0.26

_HERE = Path(__file__).resolve().parent
DEV_PAGE = _HERE / "dev.html"
CLIENT_DIR = _HERE.parent / "client"


class Transport(Enum):
    """Refusals the engine cannot express, because `rules.py` must not know a socket exists."""

    PROTOCOL = auto()


# No /docs, /redoc or /openapi.json: they describe the one route this app serves and would
# otherwise be reachable wherever this process is served.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/")
async def dev_page() -> FileResponse:
    return FileResponse(DEV_PAGE)


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


def _over_message(match: matches.Match) -> dict:
    champion = winner(match.state)

    return {
        "type": "OVER",
        "winner": None if champion is None else champion.value,
        "scores": to_dict(match.state)["scores"],
    }


def _rejected(reason: Rejection | Transport, *, tile: object = None, seq: object = None) -> dict:
    # Both taxonomies reach the wire as the member name, so nothing has to be kept in step
    # when Transport grows new reasons.
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


async def _handle_move(match: matches.Match, socket: WebSocket, message: dict) -> None:
    tile, seq = message.get("tile"), message.get("seq")

    # Neither is safe raw: the engine raises on a tile that is not an integer, and seq is
    # echoed to every other socket with nothing else bounding what a client may send.
    if type(tile) is not int or type(seq) is not int:  # not isinstance: bool subclasses int
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
            await asyncio.sleep(AI_PAUSE_SECONDS)
            started = time.perf_counter()
            ai_tile = greedy_ai(match.state, Player.SERVER, match.rng)
            settled = apply_move(match.state, Move(Player.SERVER, ai_tile))

            # Unreachable while the opponent only ever picks from legal_moves. A Rejection
            # stored here would poison the match for every socket on it.
            if isinstance(settled, Rejection):
                raise RuntimeError(f"the opponent proposed an illegal move: {settled.name}")

            match.state = settled
            theirs = {"by": Player.SERVER.value, "tile": ai_tile, "seq": None}
            await _broadcast(match, _state_message(match, last=theirs, started=started))

        if match.state.over:
            await _broadcast(match, _over_message(match))


@app.websocket("/ws/play")
async def play(socket: WebSocket) -> None:
    await socket.accept()
    matches.sweep()
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
                if match is None:
                    match = matches.create()

                match.sockets.add(socket)
                matches.touch(match)
                await _send(
                    socket,
                    {
                        "type": "WELCOME",
                        "match_id": match.id,
                        "player_token": match.player_token,
                        **_view(match),
                    },
                )
                # The others learn the client count changed; the joiner just had it in WELCOME.
                await _broadcast(match, _state_message(match), skip=socket)

            elif kind == "MOVE" and match is not None:
                await _handle_move(match, socket, message)

            else:
                await _send(
                    socket,
                    _rejected(Transport.PROTOCOL, tile=message.get("tile"), seq=message.get("seq")),
                )

    except WebSocketDisconnect:
        pass

    finally:
        if match is not None:
            match.sockets.discard(socket)
            # The idle clock starts when the last socket leaves, not at the last message.
            matches.touch(match)
            await _broadcast(match, _state_message(match))
