# szawel.com

The landing page at [www.szawel.com](https://www.szawel.com), and the game widget on it: a small
area-control game whose real subject is the server behind it.

The browser holds no rules. Every click is a command sent over a WebSocket, decided on the server,
and answered with either a new state or a typed reason for refusal. The panel beside the board shows
that traffic as it happens: turn latency, server time, payload size, and a log that reads as an audit
trail rather than a game log.

## What it demonstrates

- **Server-side validation.** The rules exist once, in `game/rules.py`: pure Python, no I/O, no
  framework, no clock. A move can be refused for `ADJACENCY`, `WATER`, `OCCUPIED`, `NOT_YOUR_TURN`
  or `GAME_OVER`, and the client learns which moves are legal from a `legal_moves` field rather
  than computing them.
- **Message authentication and replay protection.** After the handshake, every command carries an
  HMAC under a key the server mints for that connection, plus a counter it will not accept twice.
  Envelope checks run before the rules engine, and each layer rejects with its own reason:
  `MISSING_ENVELOPE`, `BAD_SIGNATURE`, `REPLAYED_NONCE`.
- **Session resumption.** A match survives a reload or a second tab; both print the same state
  digest.
- **Operating limits.** A cap on concurrent matches, per-address rate limits, a kill switch, and a
  `/health` endpoint.

And what it does not claim: the signing key is handed to the page, so anyone can read it in
devtools. The envelope protects the *session* against a third party. Anti-cheat comes entirely from
the server refusing illegal moves.

## Layout

```
index.html          the landing page, published by .github/workflows/pages.yml
game/rules.py       the rules engine, the only place rules exist
game/server/        FastAPI + WebSocket server, leaderboard, match store
game/client/        renders state and sends clicks; holds no rules
game/tests/         pytest suite
```

## Running it locally

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13.

```sh
uv run --extra dev pytest
PLAY_DB_FILE=/tmp/leaderboard.db uv run --extra server uvicorn game.server.app:app
```

Then open http://127.0.0.1:8000: the server serves its own dev page, wired to the same socket.
`PLAY_DB_FILE` moves the weekly leaderboard off its production path.

## Privacy

The page loads nothing from a third party: fonts are self-hosted and a test fails the build if any
file names an outside host. The game connection starts only when you press Start. No cookies, no
analytics, and scores reset weekly.
