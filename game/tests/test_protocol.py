import json
import time

import pytest
from fastapi.testclient import TestClient

from game.rules import Rejection
from game.server import app as server
from game.server import matches


@pytest.fixture(autouse=True)
def an_empty_registry():
    # The registry is module-level, so without this every match ever created stays visible to
    # every later test and the first assertion about the registry as a whole goes flaky.
    matches._matches.clear()


@pytest.fixture(autouse=True)
def without_the_thinking_pause(monkeypatch):
    # The pause is a game-design choice, not a delay under test, and a full match holds 34.
    monkeypatch.setattr(server, "AI_PAUSE_SECONDS", 0)


@pytest.fixture
def client():
    # As a context manager, so every socket in a test shares one event loop. Without it each
    # connection gets its own, and a match's lock ends up bound to a loop that is not running it.
    with TestClient(server.app) as connected:
        yield connected


def hello(socket, match_id=None, player_token=None) -> dict:
    socket.send_json({"type": "HELLO", "match_id": match_id, "player_token": player_token})

    return socket.receive_json()


def drain(socket, wanted) -> dict:
    """Read until `wanted` says this is the message, or fail naming everything that came first."""
    seen = []

    for _ in range(8):
        message = socket.receive_json()
        if wanted(message):
            return message

        seen.append(message["type"])

    raise AssertionError(f"the message never came; saw {seen}")


def settle(socket) -> dict:
    """The broadcasts an accepted move produces, up to the player's next turn or the end."""

    # Total over message types, so a regression that answers REJECTED is reported by drain
    # rather than raising KeyError on a message shape this never expected.
    def playable(message) -> bool:
        if message["type"] != "STATE":
            return message["type"] == "OVER"

        return not message["state"]["over"] and message["state"]["turn"] == "you"

    return drain(socket, playable)


def presence(socket) -> dict:
    """The next STATE that is not about a move — one is sent whenever a client comes or goes."""
    return drain(socket, lambda m: m["type"] == "STATE" and m["last"] is None)


def play_out(socket) -> dict:
    """Take the first legal tile until the match ends, and return the OVER message."""
    message = hello(socket)

    while message["type"] != "OVER":
        # Driven from the server's own hint: it is the only thing the client is told.
        socket.send_json({"type": "MOVE", "tile": message["legal_moves"][0], "seq": 1})
        message = settle(socket)

    return message


def test_a_match_plays_through_to_a_result(client):
    with client.websocket_connect("/ws/play") as socket:
        over = play_out(socket)
        scores = over["scores"]
        expected = None if scores["you"] == scores["server"] else max(scores, key=scores.get)

        assert sum(scores.values()) == 48
        assert over["winner"] == expected


@pytest.mark.parametrize(
    ("tile", "expected"),
    [
        (47, "ADJACENCY"),
        (999, "ADJACENCY"),
        (2, "WATER"),
        (0, "OCCUPIED"),
    ],
)
def test_a_refused_move_comes_back_with_its_reason(client, tile, expected):
    with client.websocket_connect("/ws/play") as socket:
        hello(socket)
        socket.send_json({"type": "MOVE", "tile": tile, "seq": 7})

        assert socket.receive_json() == {
            "type": "REJECTED",
            "reason": expected,
            "tile": tile,
            "seq": 7,
        }


def test_a_move_after_the_result_is_refused_rather_than_ignored(client):
    with client.websocket_connect("/ws/play") as socket:
        play_out(socket)
        socket.send_json({"type": "MOVE", "tile": 0, "seq": 99})

        assert socket.receive_json()["reason"] == "GAME_OVER"


@pytest.mark.parametrize(
    "move",
    [
        {"tile": "3", "seq": 1},
        {"tile": 3.0, "seq": 1},
        {"tile": True, "seq": 1},
        {"tile": None, "seq": 1},
        {"tile": [3], "seq": 1},
        {"tile": 1, "seq": "1"},
        {"tile": 1, "seq": None},
        {"tile": 1, "seq": {"nested": "object"}},
    ],
)
def test_a_move_that_is_not_two_integers_is_the_transport_layers_problem(client, move):
    with client.websocket_connect("/ws/play") as socket:
        hello(socket)
        socket.send_json({"type": "MOVE", **move})

        assert socket.receive_json()["reason"] == "PROTOCOL"


def test_a_move_before_hello_is_refused(client):
    with client.websocket_connect("/ws/play") as socket:
        socket.send_json({"type": "MOVE", "tile": 1, "seq": 4})

        # Quoted back, so a client can attribute the refusal to the command that earned it.
        assert socket.receive_json() == {
            "type": "REJECTED",
            "reason": "PROTOCOL",
            "tile": 1,
            "seq": 4,
        }


def test_a_binary_frame_is_refused_and_leaves_the_socket_usable(client):
    # The HELLO afterwards is half the point: a frame nobody can read must not end the session.
    with client.websocket_connect("/ws/play") as socket:
        socket.send_bytes(b'{"type": "HELLO"}')
        refusal = socket.receive_json()
        welcome = hello(socket)

        assert refusal["reason"] == "PROTOCOL"
        assert welcome["type"] == "WELCOME"


@pytest.mark.parametrize(
    "text",
    [
        '{"type": "WHAT"}',
        "not json at all",
        "[]",
        "[" * 100_000,
    ],
)
def test_a_message_the_protocol_does_not_define_is_refused(client, text):
    with client.websocket_connect("/ws/play") as socket:
        socket.send_text(text)

        assert socket.receive_json()["reason"] == "PROTOCOL"


def test_the_two_rejection_taxonomies_cannot_collide_on_the_wire():
    assert not set(server.Transport.__members__) & set(Rejection.__members__)


def test_the_mover_is_taken_from_the_socket_and_not_from_the_message(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        tile = welcome["legal_moves"][0]
        socket.send_json({"type": "MOVE", "tile": tile, "seq": 1, "player": "server"})

        assert socket.receive_json()["last"] == {"by": "you", "tile": tile, "seq": 1}


def test_a_reconnect_resumes_the_same_match(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        socket.send_json({"type": "MOVE", "tile": welcome["legal_moves"][0], "seq": 1})
        settle(socket)

    with client.websocket_connect("/ws/play") as socket:
        resumed = hello(socket, welcome["match_id"], welcome["player_token"])

    assert resumed["match_id"] == welcome["match_id"]
    assert resumed["state"]["scores"]["you"] == 4


def test_a_token_json_accepts_but_utf_8_rejects_gets_a_new_match(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)

    with client.websocket_connect("/ws/play") as socket:
        socket.send_text(
            json.dumps(
                {
                    "type": "HELLO",
                    "match_id": welcome["match_id"],
                    "player_token": "\ud800",
                }
            )
        )
        resumed = socket.receive_json()

    assert resumed["match_id"] != welcome["match_id"]


def test_an_unknown_token_gets_a_new_match_rather_than_an_error(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket, "no-such-match", "no-such-token")

    assert welcome["type"] == "WELCOME"
    assert welcome["state"]["scores"] == {"you": 3, "server": 3}


def test_two_sockets_on_one_match_both_see_the_move(client):
    with client.websocket_connect("/ws/play") as first:
        welcome = hello(first)

        with client.websocket_connect("/ws/play") as second:
            hello(second, welcome["match_id"], welcome["player_token"])
            assert first.receive_json()["clients"] == 2

            second.send_json({"type": "MOVE", "tile": welcome["legal_moves"][0], "seq": 1})

            assert first.receive_json()["last"]["by"] == "you"
            assert second.receive_json()["last"]["by"] == "you"

        assert presence(first)["clients"] == 1


def test_the_players_state_arrives_before_the_opponent_thinks(client, monkeypatch):
    # The pause belongs to the opponent's turn and must stay outside the player's round-trip,
    # which only holds while the two broadcasts are in this order.
    monkeypatch.setattr(server, "AI_PAUSE_SECONDS", 0.2)

    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        started = time.perf_counter()
        socket.send_json({"type": "MOVE", "tile": welcome["legal_moves"][0], "seq": 1})
        mine = socket.receive_json()
        round_trip = time.perf_counter() - started
        theirs = socket.receive_json()

        assert (mine["last"]["by"], theirs["last"]["by"]) == ("you", "server")
        assert round_trip < server.AI_PAUSE_SECONDS


def test_an_idle_match_with_nobody_connected_is_swept(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)

    match = matches.get(welcome["match_id"], welcome["player_token"])
    match.last_seen -= matches.IDLE_SECONDS + 1

    with client.websocket_connect("/ws/play") as socket:
        resumed = hello(socket, welcome["match_id"], welcome["player_token"])

    assert resumed["match_id"] != welcome["match_id"]
