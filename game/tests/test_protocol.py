import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from pathlib import Path

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
def kill_switch(tmp_path, monkeypatch):
    """The flag path the server will stat, not yet touched. `touch` and `unlink` it in the test."""
    flag = tmp_path / "disabled"
    monkeypatch.setattr(server, "DISABLED_FLAG", flag)

    return flag


@pytest.fixture
def client():
    # As a context manager, so every socket in a test shares one event loop. Without it each
    # connection gets its own, and a match's lock ends up bound to a loop that is not running it.
    with TestClient(server.app) as connected:
        yield connected


def hello(socket, match_id=None, player_token=None) -> dict:
    socket.send_json({"type": "HELLO", "match_id": match_id, "player_token": player_token})

    return socket.receive_json()


def envelope(welcome, **command) -> dict:
    """What a client sends: the body, signed exactly as it goes on the wire."""
    body = json.dumps(command)
    signature = hmac.new(bytes.fromhex(welcome["session_key"]), body.encode(), hashlib.sha256)

    return {"body": body, "sig": signature.hexdigest()}


def move(socket, welcome, tile, seq=1) -> None:
    socket.send_json(envelope(welcome, type="MOVE", tile=tile, seq=seq))


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


def play_out(socket, welcome) -> dict:
    """Take the first legal tile until the match ends, and return the OVER message."""
    message = welcome
    seq = 0

    while message["type"] != "OVER":
        seq += 1
        # Driven from the server's own hint: it is the only thing the client is told.
        move(socket, welcome, message["legal_moves"][0], seq)
        message = settle(socket)

    return message


def test_a_match_plays_through_to_a_result(client):
    with client.websocket_connect("/ws/play") as socket:
        over = play_out(socket, hello(socket))
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
        welcome = hello(socket)
        move(socket, welcome, tile, seq=7)

        assert socket.receive_json() == {
            "type": "REJECTED",
            "reason": expected,
            "tile": tile,
            "seq": 7,
        }


def test_a_move_after_the_result_is_refused_rather_than_ignored(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        play_out(socket, welcome)
        # Above every seq the play-out spent, so the envelope passes it to the rules.
        move(socket, welcome, 0, seq=99)

        assert socket.receive_json()["reason"] == "GAME_OVER"


@pytest.mark.parametrize(
    "command",
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
def test_a_move_that_is_not_two_integers_is_the_transport_layers_problem(client, command):
    # Signed, so the shape is the only thing left to refuse it on.
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        socket.send_json(envelope(welcome, type="MOVE", **command))

        assert socket.receive_json()["reason"] == "PROTOCOL"


def test_a_move_before_hello_is_refused(client):
    with client.websocket_connect("/ws/play") as socket:
        socket.send_json({"type": "MOVE", "tile": 1, "seq": 4})

        # Refused for being unsigned before anything asks whether there is a match to move in.
        assert socket.receive_json() == {
            "type": "REJECTED",
            "reason": "MISSING_ENVELOPE",
            "tile": None,
            "seq": None,
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
    ("text", "expected"),
    [
        # A frame that parses is asked for its envelope before anyone asks what it claims to be.
        ('{"type": "WHAT"}', "MISSING_ENVELOPE"),
        ("not json at all", "PROTOCOL"),
        ("[]", "PROTOCOL"),
        ("[" * 100_000, "PROTOCOL"),
    ],
)
def test_a_message_the_protocol_does_not_define_is_refused(client, text, expected):
    with client.websocket_connect("/ws/play") as socket:
        socket.send_text(text)

        assert socket.receive_json()["reason"] == expected


def test_a_replayed_envelope_is_refused_the_second_time(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        replayed = envelope(welcome, type="MOVE", tile=welcome["legal_moves"][0], seq=1)
        socket.send_json(replayed)
        settle(socket)
        socket.send_json(replayed)

        assert socket.receive_json()["reason"] == "REPLAYED_NONCE"


def test_a_sequence_number_that_does_not_advance_is_refused(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        move(socket, welcome, welcome["legal_moves"][0], seq=5)
        board = settle(socket)
        move(socket, welcome, board["legal_moves"][0], seq=5)

        assert socket.receive_json()["reason"] == "REPLAYED_NONCE"


def test_a_tampered_body_does_not_verify(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        forged = envelope(welcome, type="MOVE", tile=welcome["legal_moves"][0], seq=1)
        forged["body"] = forged["body"].replace('"seq": 1', '"seq": 2')
        socket.send_json(forged)

        assert socket.receive_json() == {
            "type": "REJECTED",
            "reason": "BAD_SIGNATURE",
            "tile": None,
            "seq": None,
        }


def test_the_signature_is_checked_before_the_sequence(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        move(socket, welcome, welcome["legal_moves"][0], seq=1)
        settle(socket)
        stale = envelope(welcome, type="MOVE", tile=0, seq=1)
        socket.send_json({**stale, "sig": "0" * 64})

        assert socket.receive_json()["reason"] == "BAD_SIGNATURE"


@pytest.mark.parametrize(
    ("body", "signature"),
    [
        ('{"type": "MOVE", "tile": 1, "seq": 1}', "not hex"),
        ('{"type": "MOVE", "tile": 1, "seq": 1}', ""),
        # Both halves survive JSON and neither survives compare_digest: a signature that is not
        # ASCII raises there, and a body that is not encodable raises before it.
        ('{"type": "MOVE", "tile": 1, "seq": 1}', "\ud800" * 64),
        ("\ud800", "0" * 64),
    ],
)
def test_a_frame_that_cannot_be_verified_is_refused_rather_than_raising(client, body, signature):
    with client.websocket_connect("/ws/play") as socket:
        hello(socket)
        socket.send_text(json.dumps({"body": body, "sig": signature}))

        assert socket.receive_json()["reason"] == "BAD_SIGNATURE"


def test_the_key_belongs_to_the_connection_and_not_to_the_match(client):
    with client.websocket_connect("/ws/play") as first:
        welcome = hello(first)

        with client.websocket_connect("/ws/play") as second:
            joined = hello(second, welcome["match_id"], welcome["player_token"])
            presence(first)
            # The other socket's key on this one: same match, and it still does not verify.
            move(second, welcome, welcome["legal_moves"][0])

            assert joined["session_key"] != welcome["session_key"]
            assert second.receive_json()["reason"] == "BAD_SIGNATURE"


def test_a_resume_mints_a_new_key_rather_than_reusing_the_old_one(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)

    with client.websocket_connect("/ws/play") as socket:
        resumed = hello(socket, welcome["match_id"], welcome["player_token"])

    # The match survives the socket, the key does not — and the two answer different questions,
    # so they are never the same string either.
    assert resumed["match_id"] == welcome["match_id"]
    assert resumed["session_key"] != welcome["session_key"]
    assert resumed["session_key"] != resumed["player_token"]


def test_the_two_rejection_taxonomies_cannot_collide_on_the_wire():
    assert not set(server.Transport.__members__) & set(Rejection.__members__)


def test_the_mover_is_taken_from_the_socket_and_not_from_the_message(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        tile = welcome["legal_moves"][0]
        socket.send_json(envelope(welcome, type="MOVE", tile=tile, seq=1, player="server"))

        assert socket.receive_json()["last"] == {"by": "you", "tile": tile, "seq": 1}


def test_a_reconnect_resumes_the_same_match(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        move(socket, welcome, welcome["legal_moves"][0])
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
            joined = hello(second, welcome["match_id"], welcome["player_token"])
            assert first.receive_json()["clients"] == 2

            # Signed with the joiner's own key: the match is shared, the session key is not.
            move(second, joined, welcome["legal_moves"][0])

            assert first.receive_json()["last"]["by"] == "you"
            assert second.receive_json()["last"]["by"] == "you"

        assert presence(first)["clients"] == 1


def test_two_sockets_on_one_match_print_the_same_digest(client):
    # A join changes the client count, which must not move a fingerprint of the board.
    with client.websocket_connect("/ws/play") as first:
        welcome = hello(first)

        with client.websocket_connect("/ws/play") as second:
            joined = hello(second, welcome["match_id"], welcome["player_token"])

            assert joined["digest"] == welcome["digest"]


def test_the_digest_moves_when_the_state_does(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        move(socket, welcome, welcome["legal_moves"][0])

        assert socket.receive_json()["digest"] != welcome["digest"]


def test_the_players_state_arrives_before_the_opponent_thinks(client, monkeypatch):
    # The pause belongs to the opponent's turn and must stay outside the player's round-trip,
    # which only holds while the two broadcasts are in this order.
    monkeypatch.setattr(server, "AI_PAUSE_SECONDS", 0.2)

    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        started = time.perf_counter()
        move(socket, welcome, welcome["legal_moves"][0])
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


def test_the_kill_switch_refuses_a_new_match(client, kill_switch):
    kill_switch.touch()

    with client.websocket_connect("/ws/play") as socket:
        refusal = hello(socket)

    assert refusal["reason"] == "DISABLED"


def test_the_kill_switch_lets_a_match_already_running_resume(client, kill_switch):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)

    kill_switch.touch()

    with client.websocket_connect("/ws/play") as socket:
        resumed = hello(socket, welcome["match_id"], welcome["player_token"])

    assert resumed["match_id"] == welcome["match_id"]


def test_removing_the_flag_re_enables_without_a_restart(client, kill_switch):
    kill_switch.touch()

    with client.websocket_connect("/ws/play") as socket:
        refused = hello(socket)

    kill_switch.unlink()

    with client.websocket_connect("/ws/play") as socket:
        allowed = hello(socket)

    assert (refused["type"], allowed["type"]) == ("REJECTED", "WELCOME")


def test_the_cap_refuses_once_every_match_has_a_socket(client, monkeypatch):
    monkeypatch.setattr(matches, "MAX_MATCHES", 1)

    with client.websocket_connect("/ws/play") as held:
        hello(held)

        with client.websocket_connect("/ws/play") as turned_away:
            refusal = hello(turned_away)

    assert refusal["reason"] == "AT_CAPACITY"


def test_a_full_registry_evicts_a_match_nobody_is_connected_to(client, monkeypatch):
    monkeypatch.setattr(matches, "MAX_MATCHES", 1)

    with client.websocket_connect("/ws/play") as socket:
        abandoned = hello(socket)

    with client.websocket_connect("/ws/play") as socket:
        fresh = hello(socket)

    assert fresh["type"] == "WELCOME"
    assert fresh["match_id"] != abandoned["match_id"]


@pytest.mark.parametrize("switched_off", [False, True])
def test_health_reports_the_kill_switch_rather_than_failing_on_it(
    client, kill_switch, switched_off
):
    if switched_off:
        kill_switch.touch()

    answer = client.get("/health")

    assert answer.status_code == 200
    assert answer.json()["disabled"] is switched_off


@pytest.mark.parametrize("blanked", ["", "   "])
def test_a_blanked_env_value_reads_as_unset_rather_than_breaking_the_box(blanked):
    # Import-time config, so proving it needs a real import in a real process.
    proof = subprocess.run(
        [
            sys.executable,
            "-c",
            "from game.server import app, matches;print(matches.MAX_MATCHES, app.DISABLED_FLAG)",
        ],
        env={**os.environ, "PLAY_MAX_MATCHES": blanked, "PLAY_DISABLED_FILE": blanked},
        cwd=Path(__file__).resolve().parent.parent.parent,
        capture_output=True,
        text=True,
    )

    assert proof.returncode == 0, proof.stderr
    assert proof.stdout.split() == ["200", "/var/lib/play/disabled"]


@pytest.mark.parametrize("refused", ["0", "-1"])
def test_a_cap_below_one_is_refused_at_import_rather_than_silently_applied(refused):
    proof = subprocess.run(
        [sys.executable, "-c", "from game.server import matches"],
        env={**os.environ, "PLAY_MAX_MATCHES": refused},
        cwd=Path(__file__).resolve().parent.parent.parent,
        capture_output=True,
        text=True,
    )

    assert proof.returncode != 0
    assert "PLAY_MAX_MATCHES must be at least 1" in proof.stderr
