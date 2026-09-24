import hashlib
import hmac
import itertools
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from game.rules import BOARD_SIZE, Board, Rejection, neighbours, new_game
from game.server import app as server
from game.server import leaderboard, matches


@pytest.fixture(autouse=True)
def an_empty_registry():
    # The registry is module-level, so without this every match ever created stays visible to
    # every later test and the first assertion about the registry as a whole goes flaky.
    matches._matches.clear()


@pytest.fixture(autouse=True)
def a_scratch_board(tmp_path, monkeypatch):
    # Every match played out here files a row, so without this the suite writes to the state
    # directory the box has and no other machine does.
    monkeypatch.setattr(leaderboard, "DB_PATH", tmp_path / "leaderboard.db")


@pytest.fixture(autouse=True)
def a_forgetful_window():
    # Every socket in this suite arrives from the same address, so one test's new matches
    # count against the next one's and the limit fires somewhere different on every run.
    server._minted.clear()


@pytest.fixture(autouse=True)
def without_the_thinking_pause(monkeypatch):
    # The pause is a game-design choice, not a delay under test, and a match pays it on every
    # opponent move.
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
        (999, "OFF_BOARD"),
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
            "violations": 1,
            "next": server.UNSHACKLE_AFTER,
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
            "violations": 1,
            "next": server.UNSHACKLE_AFTER,
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


def test_the_board_reaches_a_player_who_has_not_finished_anything(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)

    assert welcome["leaderboard"] == []


def test_finishing_a_match_puts_you_on_the_board(client):
    with client.websocket_connect("/ws/play") as socket:
        over = play_out(socket, hello(socket))

    # The score on the board is the one the server counted, and the client never sent one.
    assert {"handle": over["handle"], "score": over["scores"]["you"]} in over["leaderboard"]


def test_a_resumed_finished_match_still_sees_the_board(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        over = play_out(socket, welcome)

    with client.websocket_connect("/ws/play") as socket:
        resumed = hello(socket, welcome["match_id"], welcome["player_token"])
        # The next frame answers this move rather than replaying OVER, so WELCOME is the only
        # place a returning player sees the finished board.
        move(socket, resumed, 0)
        answered = socket.receive_json()

    assert resumed["state"]["over"] is True
    assert resumed["leaderboard"] == over["leaderboard"]
    assert (answered["type"], answered["reason"]) == ("REJECTED", "GAME_OVER")


def test_a_state_message_does_not_carry_the_board(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        move(socket, welcome, welcome["legal_moves"][0])
        broadcast = socket.receive_json()

    assert broadcast["type"] == "STATE"
    assert "leaderboard" not in broadcast


def test_a_match_with_two_sockets_on_it_is_recorded_once(client):
    with client.websocket_connect("/ws/play") as first:
        welcome = hello(first)

        with client.websocket_connect("/ws/play") as second:
            hello(second, welcome["match_id"], welcome["player_token"])
            presence(first)
            play_out(first, welcome)

    assert len(leaderboard.top()) == 1


def test_a_new_match_is_refused_once_the_window_is_full(client, monkeypatch):
    monkeypatch.setattr(server, "MINT_LIMIT", 1)

    with client.websocket_connect("/ws/play") as socket:
        hello(socket)

    with client.websocket_connect("/ws/play") as turned_away:
        refusal = hello(turned_away)

    assert refusal["reason"] == "RATE_LIMITED"


def test_a_resume_is_never_rate_limited(client, monkeypatch):
    monkeypatch.setattr(server, "MINT_LIMIT", 1)

    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)

    with client.websocket_connect("/ws/play") as socket:
        resumed = hello(socket, welcome["match_id"], welcome["player_token"])

    assert resumed["type"] == "WELCOME"
    assert resumed["match_id"] == welcome["match_id"]


def test_the_window_forgets(client, monkeypatch):
    monkeypatch.setattr(server, "MINT_LIMIT", 1)

    with client.websocket_connect("/ws/play") as socket:
        hello(socket)

    aged = time.monotonic() - server.MINT_WINDOW_SECONDS - 1

    for address in server._minted:
        server._minted[address] = [aged]

    with client.websocket_connect("/ws/play") as socket:
        fresh = hello(socket)

    assert fresh["type"] == "WELCOME"


def test_a_different_address_gets_its_own_window(client, monkeypatch):
    monkeypatch.setattr(server, "MINT_LIMIT", 1)

    with client.websocket_connect("/ws/play") as socket:
        hello(socket)

    # The address is the whole key, and starlette lets a test be a second visitor.
    with TestClient(server.app, client=("10.0.0.2", 1234)) as elsewhere:
        with elsewhere.websocket_connect("/ws/play") as socket:
            welcome = hello(socket)

    assert welcome["type"] == "WELCOME"


@pytest.fixture
def a_broken_store(monkeypatch):
    """A store that raises on every call, which is a full disk or an unwritable state dir."""

    def refuse(*args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(leaderboard, "record", refuse)
    monkeypatch.setattr(leaderboard, "top", refuse)


def test_a_broken_store_does_not_cost_a_visitor_their_game(client, a_broken_store):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)

    assert welcome["type"] == "WELCOME"
    assert "leaderboard" not in welcome


def test_a_broken_store_does_not_cost_a_finished_match_its_result(client, a_broken_store):
    with client.websocket_connect("/ws/play") as socket:
        over = play_out(socket, hello(socket))

    assert sum(over["scores"].values()) == 48
    assert "handle" not in over
    assert "leaderboard" not in over


def test_a_refused_match_is_not_counted_against_the_window(client, monkeypatch):
    monkeypatch.setattr(matches, "MAX_MATCHES", 1)
    monkeypatch.setattr(server, "MINT_LIMIT", 2)

    with client.websocket_connect("/ws/play") as held:
        hello(held)

        # Turned away because the registry is full, not because of anything this visitor did.
        with client.websocket_connect("/ws/play") as turned_away:
            assert hello(turned_away)["reason"] == "AT_CAPACITY"

    with client.websocket_connect("/ws/play") as socket:
        after = hello(socket)

    # One match was handed out, so one of the two slots is spent — not both.
    assert after["type"] == "WELCOME"


def provoke(socket, count) -> list[dict]:
    """Unsigned frames, refused one at a time, and the refusals they came back as."""
    refusals = []

    for _ in range(count):
        socket.send_json({"type": "MOVE", "tile": 0, "seq": 1})
        refusals.append(socket.receive_json())

    return refusals


def probe(socket, welcome) -> None:
    """A command the server refuses on shape, marking where a round's broadcasts end.

    The handler is sequential, so the refusal cannot overtake the round in front of it — and
    the turn flipping back to the player cannot mark it, because a seized turn comes after.
    """
    socket.send_json(envelope(welcome, type="MOVE", tile=0))


def until_refused(socket) -> list[dict]:
    """Everything broadcast before the probe's refusal comes back."""
    seen = []

    for _ in range(BOARD_SIZE * 2):
        message = socket.receive_json()

        if message["type"] == "REJECTED":
            return seen

        seen.append(message)

    raise AssertionError(f"the probe was never refused; saw {len(seen)} messages")


def opponent_round(socket, welcome) -> list[dict]:
    """The `last` of every opponent move in one round, seized turns included."""
    probe(socket, welcome)

    return [
        message["last"]
        for message in until_refused(socket)
        if message["type"] == "STATE" and message["last"] and message["last"]["by"] == "server"
    ]


def watch_out(socket, welcome) -> tuple[list[dict], dict]:
    """Play the match out reading every frame, and return the states with the closing OVER.

    The states in between are where a seized turn shows up if the opponent ever took a tile
    the engine would have refused.
    """
    states = [welcome]
    legal = welcome["legal_moves"]

    for seq in range(1, BOARD_SIZE + 1):
        move(socket, welcome, legal[0], seq)
        probe(socket, welcome)
        broadcast = until_refused(socket)
        states.extend(message for message in broadcast if message["type"] == "STATE")
        over = [message for message in broadcast if message["type"] == "OVER"]

        if over:
            return states, over[0]

        # Read off the last state of the round, not the one that handed the turn back: a
        # seized turn moves the board again after that.
        legal = states[-1]["legal_moves"]

    raise AssertionError("the match never ended")


def can_move(board, player) -> bool:
    owners, terrain = board["owners"], board["terrain"]

    return any(
        owners[near] is None and terrain[near] != "water"
        for index, owner in enumerate(owners)
        if owner == player
        for near in neighbours(index)
    )


def test_envelope_violations_unshackle_the_opponent_a_turn_at_a_time(client):
    step = server.UNSHACKLE_AFTER

    with client.websocket_connect("/ws/play") as socket:
        hello(socket)
        refusals = provoke(socket, step * 2)

    announced = [(r["violations"], r["extra_turns"]) for r in refusals if "extra_turns" in r]

    assert [r["reason"] for r in refusals] == ["MISSING_ENVELOPE"] * (step * 2)

    # Once per step up and nowhere in between: the console says it, it does not repeat it.
    assert announced == [(step, 1), (step * 2, 2)]


def test_a_verified_command_the_protocol_refuses_does_not_count(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)

        for _ in range(server.UNSHACKLE_AFTER * 2):
            # Signed with this session's own key, so it clears the envelope and fails on shape.
            socket.send_json(envelope(welcome, type="MOVE", tile=0))
            refusal = socket.receive_json()

            assert refusal["reason"] == "PROTOCOL"
            assert "extra_turns" not in refusal


def test_an_unshackled_opponent_takes_a_burst_of_seized_turns(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        provoke(socket, server.UNSHACKLE_AFTER * 2)
        move(socket, welcome, welcome["legal_moves"][0], 1)
        theirs = opponent_round(socket, welcome)

    # The first is the turn the engine handed over; the two after it were seized.
    assert [taken["seized"] for taken in theirs] == [False, True, True]


def test_a_second_socket_meets_a_shackled_opponent(client):
    with client.websocket_connect("/ws/play") as poked:
        hello(poked)
        provoke(poked, server.UNSHACKLE_AFTER)

        with client.websocket_connect("/ws/play") as clean:
            welcome = hello(clean)
            move(clean, welcome, welcome["legal_moves"][0], 1)
            theirs = opponent_round(clean, welcome)

    assert [taken["seized"] for taken in theirs] == [False]


def test_every_move_an_unshackled_opponent_makes_is_legal(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        provoke(socket, server.UNSHACKLE_AFTER * 3)
        states, over = watch_out(socket, welcome)

    for before, after in itertools.pairwise(states):
        owners, terrain = before["state"]["owners"], before["state"]["terrain"]
        claimed = [i for i, owner in enumerate(after["state"]["owners"]) if owner != owners[i]]
        index = after["last"]["tile"]
        taken = after["state"]["owners"][index]

        assert len(claimed) == 1 + after["last"].get("settled", 0), "a tile nobody accounted for"
        assert all(owners[i] is None for i in claimed), "a tile changed hands"
        assert all(terrain[i] != "water" for i in claimed)
        assert any(owners[near] == taken for near in neighbours(index))

    assert sum(over["scores"].values()) == 48


def test_a_seized_turn_leaves_a_state_the_engine_could_have_produced(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        provoke(socket, server.UNSHACKLE_AFTER * 3)
        states, _ = watch_out(socket, welcome)

    # The invariant `_settle_turn` maintains, and the one thing seizing a turn could break:
    # after any move, either the game is over or the player to move has a move.
    for message in states:
        board = message["state"]

        assert board["over"] or can_move(board, board["turn"])


def test_a_seizure_that_fails_mid_turn_leaves_the_match_playable(client, monkeypatch):
    real = server.greedy_ai
    picks, fell = [], []

    def fall_over_on_the_seized_turn(state, player, rng):
        picks.append(state)

        # The second pick of the round is the seized one, and the patch outlives the socket:
        # the resume below picks again, and has to be allowed to.
        if len(picks) == 2:
            fell.append(True)

            raise RuntimeError("the opponent fell over mid-seizure")

        return real(state, player, rng)

    monkeypatch.setattr(server, "greedy_ai", fall_over_on_the_seized_turn)

    with pytest.raises(RuntimeError), client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        provoke(socket, server.UNSHACKLE_AFTER)
        move(socket, welcome, welcome["legal_moves"][0], 1)
        socket.receive_json()

    # A seized turn stored before the pause would still be in the registry, and this move
    # would answer NOT_YOUR_TURN.
    with client.websocket_connect("/ws/play") as resumed:
        again = hello(resumed, welcome["match_id"], welcome["player_token"])
        move(resumed, again, again["legal_moves"][0], 1)
        answer = settle(resumed)

    assert fell
    assert answer["type"] == "STATE"


def test_a_sealed_player_is_settled_rather_than_played_out_by_seizures(client):
    # A pocket with exactly one move in it: tile 1, and then the player is sealed for good.
    pocket = Board(
        water=frozenset({2, 8, 9}),
        valuable=frozenset(),
        you_start=frozenset({0}),
        server_start=frozenset({7}),
    )

    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        [match] = matches._matches.values()
        match.state = new_game(pocket)
        refusals = provoke(socket, server.UNSHACKLE_AFTER)
        move(socket, welcome, 1, 1)
        mine = socket.receive_json()
        over = socket.receive_json()

    # Without this the test would pass on an opponent that was never unshackled.
    assert refusals[-1]["extra_turns"] == 1

    assert mine["last"] == {"by": "you", "tile": 1, "seq": 1, "settled": 42}
    assert mine["winner"] == "server"
    assert over["type"] == "OVER"


def test_an_unshackled_match_still_ends_and_still_files_its_score(client):
    with client.websocket_connect("/ws/play") as socket:
        welcome = hello(socket)
        provoke(socket, server.UNSHACKLE_AFTER * 2)
        _, over = watch_out(socket, welcome)

    assert over["handle"]
    assert over["leaderboard"][0]["score"] == over["scores"]["you"]


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
            "from game.server import app, leaderboard, matches;"
            "print(matches.MAX_MATCHES, app.DISABLED_FLAG, app.MINT_LIMIT, leaderboard.DB_PATH)",
        ],
        env={
            **os.environ,
            "PLAY_MAX_MATCHES": blanked,
            "PLAY_DISABLED_FILE": blanked,
            "PLAY_MINT_LIMIT": blanked,
            "PLAY_DB_FILE": blanked,
        },
        cwd=Path(__file__).resolve().parent.parent.parent,
        capture_output=True,
        text=True,
    )

    assert proof.returncode == 0, proof.stderr
    assert proof.stdout.split() == [
        "200",
        "/var/lib/play/disabled",
        "10",
        "/var/lib/play/leaderboard.db",
    ]


@pytest.mark.parametrize("refused", ["0", "-1"])
@pytest.mark.parametrize(
    ("variable", "module"), [("PLAY_MAX_MATCHES", "matches"), ("PLAY_MINT_LIMIT", "app")]
)
def test_a_limit_below_one_is_refused_at_import_rather_than_silently_applied(
    variable, module, refused
):
    proof = subprocess.run(
        [sys.executable, "-c", f"from game.server import {module}"],
        env={**os.environ, variable: refused},
        cwd=Path(__file__).resolve().parent.parent.parent,
        capture_output=True,
        text=True,
    )

    assert proof.returncode != 0
    assert f"{variable} must be at least 1" in proof.stderr


@pytest.mark.parametrize(
    "origin",
    [None, "https://www.szawel.com", "https://play.szawel.com", "http://localhost:8811"],
)
def test_the_site_its_box_and_a_local_preview_may_open_a_match(client, origin):
    headers = {"origin": origin} if origin else {}

    with client.websocket_connect("/ws/play", headers=headers) as socket:
        welcome = hello(socket)

    assert welcome["type"] == "WELCOME"


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "https://www.szawel.com.evil.example",
        "http://www.szawel.com",
        "null",
    ],
)
def test_another_sites_page_is_refused_at_the_handshake(client, origin):
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/ws/play", headers={"origin": origin}):
            pass

    assert refused.value.code == 1008
