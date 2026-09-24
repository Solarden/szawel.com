import copy
import random

import pytest

from game.rules import (
    BOARD_SIZE,
    COLS,
    DEFAULT_BOARD,
    Board,
    GameState,
    Move,
    Player,
    Rejection,
    apply_move,
    greedy_ai,
    is_over,
    legal_moves,
    new_game,
    score,
    to_dict,
    winner,
)


def hand_board(
    *, water=frozenset(), valuable=frozenset(), you=frozenset({0}), server=frozenset({7})
) -> Board:
    return Board(
        water=frozenset(water),
        valuable=frozenset(valuable),
        you_start=frozenset(you),
        server_start=frozenset(server),
    )


def play_out(state: GameState, seed: int) -> GameState:
    rng = random.Random(seed)

    while not state.over:
        state = apply_move(state, Move(state.turn, greedy_ai(state, state.turn, rng)))
        assert state.over or legal_moves(state, state.turn), "a turn nobody can play"

    return state


def test_new_game_gives_each_side_its_starting_tiles():
    state = new_game()

    assert score(state, Player.YOU) == score(state, Player.SERVER) == 3
    assert state.turn is Player.YOU


@pytest.mark.parametrize(
    ("water", "you", "expected"),
    [
        (frozenset(), frozenset({0}), frozenset({1, 8})),
        (frozenset({1}), frozenset({0}), frozenset({8})),
        (frozenset(), frozenset({0, 1}), frozenset({2, 8, 9})),
    ],
)
def test_legal_moves(water, you, expected):
    state = new_game(hand_board(water=water, you=you))

    assert legal_moves(state, Player.YOU) == expected


@pytest.mark.parametrize(
    ("water", "you", "mover", "index", "expected"),
    [
        (frozenset(), frozenset({0}), Player.YOU, 40, Rejection.ADJACENCY),
        (frozenset(), frozenset({0}), Player.YOU, 999, Rejection.ADJACENCY),
        (frozenset(), frozenset({0}), Player.YOU, -1, Rejection.ADJACENCY),
        (frozenset({1}), frozenset({0}), Player.YOU, 1, Rejection.WATER),
        (frozenset(), frozenset({0, 8}), Player.YOU, 8, Rejection.OCCUPIED),
        (frozenset(), frozenset({0}), Player.SERVER, 6, Rejection.NOT_YOUR_TURN),
    ],
)
def test_each_rejection_fires(water, you, mover, index, expected):
    state = new_game(hand_board(water=water, you=you))

    assert apply_move(state, Move(mover, index)) is expected


@pytest.mark.parametrize(
    ("water", "valuable", "you", "server"),
    [
        (frozenset({0}), frozenset(), frozenset({0}), frozenset({7})),
        (frozenset({19}), frozenset({19}), frozenset({0}), frozenset({7})),
        (frozenset(), frozenset(), frozenset({99}), frozenset({7})),
        (frozenset(), frozenset(), frozenset({0}), frozenset({0})),
    ],
)
def test_a_malformed_board_is_refused(water, valuable, you, server):
    with pytest.raises(ValueError):
        Board(water=water, valuable=valuable, you_start=you, server_start=server)


@pytest.mark.parametrize("ask", [legal_moves, score])
def test_a_non_player_is_refused_rather_than_answered(ask):
    state = new_game()

    with pytest.raises(ValueError):
        ask(state, None)


def test_winner_refuses_an_unfinished_game():
    state = new_game()

    with pytest.raises(ValueError):
        winner(state)


def test_a_move_after_the_game_ends_is_rejected():
    state = new_game(hand_board(water={1, 6, 8, 15}))
    assert is_over(state)

    assert apply_move(state, Move(Player.YOU, 24)) is Rejection.GAME_OVER


def test_legal_moves_and_apply_move_never_disagree():
    # Part-way through a real game: an opening position exercises too few tiles.
    state = new_game()
    rng = random.Random(0)

    for _ in range(8):
        state = apply_move(state, Move(state.turn, greedy_ai(state, state.turn, rng)))

    allowed = legal_moves(state, state.turn)

    for index in range(BOARD_SIZE):
        accepted = isinstance(apply_move(state, Move(state.turn, index)), GameState)
        assert accepted is (index in allowed), f"index {index}"


def test_apply_move_leaves_the_state_it_was_given_alone():
    state = new_game()
    before = copy.deepcopy(state)

    apply_move(state, Move(Player.YOU, 24))

    assert state == before


def test_a_sealed_player_is_passed_over_and_the_game_still_ends():
    # YOU holds tile 0 with water on both its neighbours, so YOU never moves again.
    state = new_game(hand_board(water={1, 8}))
    assert state.turn is Player.SERVER

    end = play_out(state, seed=0)

    assert (score(end, Player.YOU), score(end, Player.SERVER)) == (1, 45)


def test_equal_value_is_a_draw_not_a_win():
    state = new_game(hand_board(water={1, 6, 8, 15}))

    assert is_over(state)
    assert score(state, Player.YOU) == score(state, Player.SERVER)
    assert winner(state) is None


def test_a_full_game_reaches_a_win():
    end = play_out(new_game(), seed=0)

    assert (score(end, Player.YOU), score(end, Player.SERVER)) == (25, 23)
    assert winner(end) is Player.YOU


def test_greedy_ai_replays_the_same_game_from_one_seed():
    def moves(seed):
        state, played = new_game(), []
        rng = random.Random(seed)

        while not state.over:
            move = greedy_ai(state, state.turn, rng)
            played.append(move)
            state = apply_move(state, Move(state.turn, move))

        return played

    assert moves(0) == moves(0)
    assert moves(0)[:5] == [24, 31, 1, 22, 32]


def test_the_default_board_is_mirror_symmetric():
    def mirror(indexes):
        return frozenset(
            row * COLS + (COLS - 1 - col) for row, col in (divmod(i, COLS) for i in indexes)
        )

    # Mirroring is an automorphism of orthogonal adjacency and maps one start onto the other,
    # so these three equalities are the whole of fairness — no distance check needed.
    assert mirror(DEFAULT_BOARD.water) == DEFAULT_BOARD.water
    assert mirror(DEFAULT_BOARD.valuable) == DEFAULT_BOARD.valuable
    assert mirror(DEFAULT_BOARD.you_start) == DEFAULT_BOARD.server_start


def test_to_dict_carries_the_state_and_not_the_envelope():
    payload = to_dict(new_game())

    assert payload["turn"] == "you"
    assert payload["scores"] == {"you": 3, "server": 3}
    assert payload["terrain"][2] == "water" and payload["terrain"][19] == "valuable"
    assert payload["owners"][0] == "you" and payload["owners"][1] is None
    assert "legal_moves" not in payload
