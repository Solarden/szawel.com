"""The rules of the game. The only place they exist.

Pure: no I/O, no framework, no clock, no module-level mutable state. The server imports
this; nothing here knows it is on a network.
"""

import random
from dataclasses import dataclass, replace
from enum import Enum, auto

COLS, ROWS = 8, 6
BOARD_SIZE = COLS * ROWS


class Terrain(Enum):
    # The value IS the tile's points. Water is unclaimable, so its 0 never reaches a score.
    WATER = 0
    PLAIN = 1
    VALUABLE = 3

    @property
    def points(self) -> int:
        return self.value


class Player(Enum):
    # String values so to_dict needs no second mapping on the way to the wire.
    YOU = "you"
    SERVER = "server"

    @property
    def opponent(self) -> "Player":
        return Player.SERVER if self is Player.YOU else Player.YOU


class Rejection(Enum):
    ADJACENCY = auto()
    WATER = auto()
    OCCUPIED = auto()
    NOT_YOUR_TURN = auto()
    GAME_OVER = auto()


@dataclass(frozen=True, slots=True)
class Board:
    """A map. Tunable in one place, which is why it is a value and not a literal in the engine."""

    water: frozenset[int]
    valuable: frozenset[int]
    you_start: frozenset[int]
    server_start: frozenset[int]

    # The map is the one value meant to be hand-edited, and every one of these mistakes is
    # silent: a start on water expands from inside a wall, a valuable on water loses its points.
    def __post_init__(self) -> None:
        tiles = self.water | self.valuable | self.you_start | self.server_start

        if any(not 0 <= index < BOARD_SIZE for index in tiles):
            raise ValueError("a board index is off the board")
        if self.water & (self.valuable | self.you_start | self.server_start):
            raise ValueError("a water tile cannot also be valuable or a starting tile")
        if self.you_start & self.server_start:
            raise ValueError("the two sides cannot share a starting tile")


# Water sits as obstacles inside a shared middle rather than as a wall across it: a wall is
# fair but turns the game into two solitaires. Rows 2-3 are open the whole way.
DEFAULT_BOARD = Board(
    water=frozenset({2, 5, 10, 13, 34, 37, 42, 45}),
    valuable=frozenset({19, 20, 24, 31}),
    you_start=frozenset({0, 8, 16}),
    server_start=frozenset({7, 15, 23}),
)


@dataclass(frozen=True, slots=True)
class Move:
    player: Player
    index: int


@dataclass(frozen=True, slots=True)
class GameState:
    board: Board
    owners: tuple[Player | None, ...]
    turn: Player
    over: bool


# Orthogonal, not diagonal: a diagonal step walks past a one-tile water gap, and water stops
# meaning anything.
def neighbours(index: int) -> tuple[int, ...]:
    row, col = divmod(index, COLS)

    return tuple(
        (row + delta_row) * COLS + (col + delta_col)
        for delta_row, delta_col in ((-1, 0), (1, 0), (0, -1), (0, 1))
        if 0 <= row + delta_row < ROWS and 0 <= col + delta_col < COLS
    )


def terrain_at(board: Board, index: int) -> Terrain:
    if index in board.water:
        return Terrain.WATER
    if index in board.valuable:
        return Terrain.VALUABLE

    return Terrain.PLAIN


def _tiles_of(state: GameState, player: Player) -> tuple[int, ...]:
    # `owners` marks an unowned tile with None, so a None player would match every free tile
    # and answer "34 legal moves" rather than failing.
    if not isinstance(player, Player):
        raise ValueError(f"not a player: {player!r}")

    return tuple(index for index, owner in enumerate(state.owners) if owner is player)


def legal_moves(state: GameState, player: Player) -> frozenset[int]:
    held = _tiles_of(state, player)

    if state.over:
        return frozenset()

    return frozenset(
        near
        for index in held
        for near in neighbours(index)
        if state.owners[near] is None and near not in state.board.water
    )


def _settle_turn(state: GameState) -> GameState:
    if legal_moves(state, state.turn):
        return state

    opponent = state.turn.opponent

    if legal_moves(state, opponent):
        return replace(state, turn=opponent)

    return replace(state, over=True)


def new_game(board: Board = DEFAULT_BOARD, first: Player = Player.YOU) -> GameState:
    owners: list[Player | None] = [None] * BOARD_SIZE

    for player, start in ((Player.YOU, board.you_start), (Player.SERVER, board.server_start)):
        for index in start:
            owners[index] = player

    return _settle_turn(GameState(board=board, owners=tuple(owners), turn=first, over=False))


def apply_move(state: GameState, move: Move) -> GameState | Rejection:
    if state.over:
        return Rejection.GAME_OVER
    if move.player is not state.turn:
        return Rejection.NOT_YOUR_TURN

    # An off-board index gets ADJACENCY rather than a sixth reason: it is not next to
    # anything the player holds, and a hostile client must not reach an IndexError.
    if not 0 <= move.index < BOARD_SIZE:
        return Rejection.ADJACENCY
    if move.index in state.board.water:
        return Rejection.WATER
    if state.owners[move.index] is not None:
        return Rejection.OCCUPIED
    if move.index not in legal_moves(state, move.player):
        return Rejection.ADJACENCY

    owners = list(state.owners)
    owners[move.index] = move.player

    return _settle_turn(replace(state, owners=tuple(owners), turn=move.player.opponent))


def score(state: GameState, player: Player) -> int:
    return sum(terrain_at(state.board, index).points for index in _tiles_of(state, player))


def is_over(state: GameState) -> bool:
    return state.over


def winner(state: GameState) -> Player | None:
    # Raised, not asserted: python -O strips an assert, and without this check an unfinished
    # game returns None, which the caller reads as a draw.
    if not state.over:
        raise ValueError("winner() is only meaningful once is_over(state)")

    you, server = score(state, Player.YOU), score(state, Player.SERVER)

    if you == server:
        return None

    return Player.YOU if you > server else Player.SERVER


def to_dict(state: GameState) -> dict:
    """The state, and only the state. The message around it belongs to the server."""
    return {
        "cols": COLS,
        "rows": ROWS,
        "terrain": [terrain_at(state.board, index).name.lower() for index in range(BOARD_SIZE)],
        "owners": [None if owner is None else owner.value for owner in state.owners],
        "turn": state.turn.value,
        "over": state.over,
        "scores": {player.value: score(state, player) for player in Player},
    }


def greedy_ai(state: GameState, player: Player, rng: random.Random) -> int:
    moves = legal_moves(state, player)
    assert moves, "greedy_ai was asked for a move with none available"

    best = max(terrain_at(state.board, index).points for index in moves)

    # sorted() so a seeded rng replays the same game: set iteration order is not a contract.
    return rng.choice(sorted(i for i in moves if terrain_at(state.board, i).points == best))
