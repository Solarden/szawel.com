// Renders state, sends clicks, and holds no rules: legal moves arrive in every state message,
// so there is nothing here that could disagree with the engine.
(function () {
  'use strict';

  var el = function (id) { return document.getElementById(id); };
  var boardEl = el('board');
  var logEl = el('log');
  var leadersEl = el('leaders');
  // The header and the rows scroll together, so the scroller is the wrapper, not the rows.
  var scrollEl = logEl.closest('.logwrap');
  var BYTES = new TextEncoder();
  var STORE = 'szawel.match';

  // The code is the record; the gloss is decoration. A reason with no entry prints the code
  // alone rather than a blank — and a test asserts these keys against both server enums.
  var REASONS = {
    ADJACENCY: 'not next to yours',
    WATER: 'water is unclaimable',
    OCCUPIED: 'already owned',
    NOT_YOUR_TURN: "opponent's turn",
    GAME_OVER: 'match is decided',
    PROTOCOL: 'not a valid command',
    DISABLED: 'not accepting new matches',
    AT_CAPACITY: 'too many matches in progress',
    MISSING_ENVELOPE: 'command was not signed',
    BAD_SIGNATURE: 'signature did not verify',
    REPLAYED_NONCE: 'command already seen',
    RATE_LIMITED: 'too many new matches from here'
  };

  // One per step up, and then silence. Reaching the third means sixty forged frames on one
  // connection, which is nobody who got here by accident.
  var SMITH = [
    'I hate this place.',
    'This zoo. This prison.',
    'I can taste your stink.'
  ];

  // The host page names the socket. A page that does not is talking to its own origin, which
  // is what the dev harness does — and what the apex must not do, since it is served by Pages.
  var endpoint = new URL(boardEl.dataset.endpoint || '/ws/play', location.href);

  // Only the relative branch needs mapping: a configured wss:// URL already is one.
  if (endpoint.protocol === 'http:' || endpoint.protocol === 'https:') {
    endpoint.protocol = endpoint.protocol === 'https:' ? 'wss:' : 'ws:';
  }

  var socket = null;
  var signer = null;
  // Signing is async, so the sends queue on this: two quick clicks must reach the socket in
  // click order, or the lower seq arrives second and the server reads it as a replay.
  var sending = Promise.resolve();
  var tiles = [];
  var legal = new Set();
  var seq = 0;
  var logSeq = 0;
  var pending = new Map();
  var live = false;
  var closed = false;
  var seated = false;
  var declined = null;

  // Every line below is written by something that happened. Nothing is on a timer.

  function stamp() {
    var now = new Date();

    return now.toTimeString().slice(0, 8) + '.' + String(now.getMilliseconds()).padStart(3, '0');
  }

  function plural(clients) {
    return clients + (clients === 1 ? ' client' : ' clients');
  }

  // Counts lines written, not frames received: a message that never arrived leaves no gap here.
  function line(actor, event, subject, outcome) {
    var row = document.createElement('div');
    // Follow the tail only for a reader already at it. Checking seq for gaps means scrolling
    // up, and an opponent turn writes two lines — enough to snatch it back every few seconds.
    var following = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight < 4;
    logSeq += 1;
    row.className = 'ln ' + actor;
    row.appendChild(span('sq', String(logSeq).padStart(3, '0')));
    row.appendChild(span('ts', stamp()));
    row.appendChild(span('ac', actor));
    row.appendChild(span('ev', event));
    row.appendChild(span('su', subject));
    // The outcome's first word carries its own colour, so an actor class never has to know
    // whether the thing it did was refused.
    row.appendChild(span('ou ' + (outcome.match(/[A-Za-z]+/) || [''])[0].toLowerCase(), outcome));
    logEl.appendChild(row);

    while (logEl.children.length > 40) {
      logEl.removeChild(logEl.firstChild);
    }

    // After the trim, or it scrolls to a height that is about to shrink.
    if (following) {
      scrollEl.scrollTop = scrollEl.scrollHeight;
    }

    // ponytail: no else — the browser's scroll anchoring already holds a parked reader's rows
    // still, and compensating for the trimmed height on top of that scrolls them backwards.
  }

  // textContent, not innerHTML: a reason code is server-supplied text on its way into the DOM.
  function span(cls, text) {
    var node = document.createElement('span');
    node.className = cls;
    node.textContent = text;

    return node;
  }

  // A decided match replays no OVER, so a returning player only ever meets the board on
  // WELCOME — which is why both messages carry it.
  function leaders(rows) {
    // A server that sent no board has not said the week is empty, so nothing here may
    // say it either: the strip keeps its placeholder. Reachable the moment the client
    // and the server stop deploying together, which is exactly what P7 does to them.
    if (!rows) {
      return;
    }

    leadersEl.textContent = '';

    if (!rows.length) {
      leadersEl.appendChild(span('empty', 'no scores yet this week'));

      return;
    }

    rows.forEach(function (row, index) {
      var entry = document.createElement('div');
      entry.className = 'lead';
      entry.appendChild(span('rk', String(index + 1).padStart(2, '0')));
      entry.appendChild(span('hd', row.handle));
      entry.appendChild(span('pt', String(row.score)));
      leadersEl.appendChild(entry);
    });
  }

  function presence(clients) {
    el('connTxt').textContent = 'connected · ' + plural(clients);
  }

  function metric(id, value, unit) {
    var node = el(id);
    var suffix = document.createElement('small');
    suffix.textContent = ' ' + unit;
    node.textContent = value;
    node.appendChild(suffix);
  }

  // A private window or blocked storage costs the resume, not the game.

  function recall() {
    try {
      return JSON.parse(localStorage.getItem(STORE)) || {};
    } catch (err) {
      return {};
    }
  }

  function remember(welcome) {
    try {
      localStorage.setItem(STORE, JSON.stringify({
        match_id: welcome.match_id,
        player_token: welcome.player_token
      }));
    } catch (err) {
      return;
    }
  }

  function build(state) {
    // The placeholder board and the start control are children here and are not in `tiles`.
    // Without this the real tiles append under them and the board silently doubles in height.
    boardEl.textContent = '';
    boardEl.style.gridTemplateColumns = 'repeat(' + state.cols + ', 1fr)';
    boardEl.style.aspectRatio = state.cols + '/' + state.rows;

    state.terrain.forEach(function (_, index) {
      var tile = document.createElement('div');
      // A div with a click handler is unreachable without a mouse, and the board is the game.
      tile.setAttribute('role', 'button');
      tile.tabIndex = 0;
      tile.appendChild(span('dot', ''));
      tile.addEventListener('click', function () { move(index); });
      tile.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          move(index);
        }
      });
      boardEl.appendChild(tile);
      tiles.push(tile);
    });
  }

  // The board says whose a tile is in colour; this says it in words.
  function describe(state, index, owner) {
    var place = 'row ' + (Math.floor(index / state.cols) + 1)
      + ' column ' + (index % state.cols + 1) + ', ';

    if (owner) {
      return place + (owner === 'you' ? 'yours' : 'opponent');
    }

    if (state.terrain[index] === 'water') {
      return place + 'water, cannot be claimed';
    }

    return place + (legal.has(index) ? 'free, playable' : 'free');
  }

  function render(state) {
    if (!tiles.length) {
      build(state);
    }

    state.owners.forEach(function (owner, index) {
      tiles[index].className = 'tile ' + state.terrain[index]
        + (owner ? ' ' + owner : '')
        + (legal.has(index) ? ' legal' : '');
      // describe() composes literals and integers, so no server string reaches an attribute.
      tiles[index].setAttribute('aria-label', describe(state, index, owner));
    });

    el('sYou').textContent = state.scores.you;
    el('sServer').textContent = state.scores.server;
    el('turn').textContent = state.over ? 'finished' : state.turn;
    boardEl.classList.toggle('inert', state.over || !live);
  }

  function hex(buffer) {
    return Array.from(new Uint8Array(buffer), function (byte) {
      return byte.toString(16).padStart(2, '0');
    }).join('');
  }

  // The key is readable right here, which is the limit the note under the widget states: this
  // protects the session, not against the player.
  function arm(key) {
    if (!crypto.subtle) {
      line('client', 'KEY', 'session', 'UNAVAILABLE · signing needs https or localhost');

      return;
    }

    if (typeof key !== 'string') {
      line('client', 'KEY', 'session', 'MISSING · the server sent nothing to sign with');

      return;
    }

    var raw = Uint8Array.from(key.match(/../g), function (pair) { return parseInt(pair, 16); });

    crypto.subtle.importKey('raw', raw, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
      .then(function (imported) {
        signer = imported;
        line('client', 'KEY', 'session', 'ARMED · HMAC-SHA-256, this connection only');
      });
  }

  // Synthesised, not fetched: the client loads nothing from anywhere, and a half-second tone
  // is under the three seconds that would owe the page a mute control.
  function blip() {
    try {
      var Sound = window.AudioContext || window.webkitAudioContext;

      if (!Sound) {
        return;
      }

      var ctx = new Sound();
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();
      var now = ctx.currentTime;
      osc.type = 'sawtooth';
      osc.frequency.setValueAtTime(220, now);
      osc.frequency.exponentialRampToValueAtTime(55, now + 0.5);
      // Ramped rather than switched, or the tone ends on a click.
      gain.gain.setValueAtTime(0.06, now);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.5);
      osc.connect(gain).connect(ctx.destination);
      osc.start(now);
      osc.stop(now + 0.5);
      // Closed rather than left running: one context per blip would hit the browser's cap.
      osc.onended = function () { ctx.close(); };
    } catch (err) {
      // Never out of here and into receive(): a raise there leaves the widget at "connecting"
      // with nothing said. An autoplay policy or a missing AudioContext costs the tone alone.
      return;
    }
  }

  // Nothing here judges a click: highlighting is a hint, not a gate, and an illegal click earns
  // a reason code from the server, which is the thing on show.
  function move(index) {
    if (!live) {
      return;
    }

    if (!signer) {
      line('client', 'MOVE', 'tile=' + index, 'NOT SENT · no session key to sign with');

      return;
    }

    seq += 1;
    var mine = seq;
    // Built once and sent as it stands: the signature is over these exact bytes, and
    // re-serialising them anywhere would change them.
    var body = JSON.stringify({ type: 'MOVE', tile: index, seq: mine });

    sending = sending.then(function () {
      return crypto.subtle.sign('HMAC', signer, BYTES.encode(body));
    }).then(function (signature) {
      // Checked here, not at the click: signing is async, and send() on a socket that closed
      // in the meantime buffers in silence rather than throwing.
      if (socket.readyState !== WebSocket.OPEN) {
        throw new Error('the socket closed while the command was being signed');
      }

      // Started here rather than at the click, so the tile keeps measuring a round-trip.
      pending.set(mine, performance.now());
      socket.send(JSON.stringify({ body: body, sig: hex(signature) }));
      line('client', 'MOVE', 'tile=' + index, '→ signed, sent');
    }).catch(function () {
      // A chain left rejected drops every later click in silence.
      pending.delete(mine);
      line('client', 'MOVE', 'tile=' + index, 'DROPPED · nothing reached the server');
    });
  }

  // Two lines out of one STATE frame, each carrying a measurement of its own — how long the
  // server spent deciding, and how far the broadcast reached.
  function transition(message) {
    var moved = message.last;

    // != null, not !==: a field the server omits arrives as undefined, which a strict guard
    // waves through — into a TypeError here and an "(undefined ms)" below.
    if (moved != null) {
      var mine = moved.by === 'you';
      // No server_ms, no parenthetical: the panel prints times it was sent, not times it assumed.
      var took = message.server_ms == null ? '' : ' (' + message.server_ms + ' ms)';
      line(mine ? 'server' : 'ai', mine ? 'VALIDATE' : 'MOVE', 'tile=' + moved.tile,
        'ACCEPTED' + took + (moved.seized ? ' \u00b7 seized turn' : ''));
    }

    line('server', 'STATE', 'state=' + message.digest, 'broadcast → ' + plural(message.clients));
  }

  function receive(event) {
    var message = JSON.parse(event.data);
    // Bytes, which is what the tile claims — String.length counts UTF-16 units.
    metric('mPay', BYTES.encode(event.data).length, 'B');

    if (message.type === 'WELCOME') {
      seated = true;
      remember(message);
      arm(message.session_key);
      legal = new Set(message.legal_moves);
      presence(message.clients);
      render(message.state);
      leaders(message.leaderboard);
      // Digest in the subject column, as on every STATE — comparing two tabs means reading
      // one column down, and a session's first digest must not sit in a different one.
      line('server', 'WELCOME', 'state=' + message.digest,
        'ACCEPTED · match=' + message.match_id);

      return;
    }

    if (message.type === 'STATE') {
      legal = new Set(message.legal_moves);
      presence(message.clients);
      render(message.state);

      // Matched on seq, not merely on 'you': a second tab's move broadcasts as 'you' too,
      // and timing that would report someone else's round-trip as this client's.
      if (message.last != null && pending.has(message.last.seq)) {
        metric('mLag', Math.round(performance.now() - pending.get(message.last.seq)), 'ms');
        pending.delete(message.last.seq);
      }

      if (message.server_ms != null) {
        metric('mSrv', message.server_ms, 'ms');
      }

      transition(message);

      return;
    }

    if (message.type === 'REJECTED') {
      // A refused move gets no STATE, so nothing else would ever retire its timer.
      pending.delete(message.seq);

      // Refused before ever being seated is the server declining the session rather than a
      // move, and the close that follows is its doing — not the server dying under us.
      if (!seated) {
        declined = message.reason;
      }

      // A malformed frame is refused without ever being a move, so the event column
      // must not claim it was one.
      var aMove = message.tile != null;
      var gloss = REASONS[message.reason];
      line('client', aMove ? 'MOVE' : 'COMMAND', aMove ? 'tile=' + message.tile : '·',
        'REJECTED ' + message.reason + (gloss ? ' · ' + gloss : ''));

      // Sent only on the refusal that widens the opponent's round, and composed here from two
      // numbers rather than shipped as a sentence — no server prose reaches the DOM.
      if (message.extra_turns) {
        line('server', 'OPPONENT', 'violations=' + message.violations,
          'UNSHACKLED · ' + message.extra_turns
          + (message.extra_turns === 1 ? ' extra turn' : ' extra turns')
          + ' a round, every move still validated');

        // The one line on this panel that is a wink rather than a record. The event under it
        // is real; only the wording is not.
        var quip = SMITH[message.extra_turns - 1];

        if (quip) {
          line('ai', 'SMITH', '·', '"' + quip + '"');
          blip();
        }
      }

      return;
    }

    if (message.type === 'OVER') {
      line('server', 'OVER', 'winner=' + (message.winner || 'draw'),
        message.scores.you + ':' + message.scores.server);
      leaders(message.leaderboard);

      // No handle means nothing was filed, and a FILED line would say otherwise.
      if (message.handle) {
        line('server', 'SCORE', message.handle, 'FILED · week resets, board is top ten');
      }
    }
  }

  function dead() {
    if (closed) {
      return;
    }

    closed = true;
    live = false;
    // A highlight is a claim about what the server would accept, and nothing is asking it now.
    legal = new Set();
    tiles.forEach(function (tile) { tile.classList.remove('legal'); });
    el('led').classList.add('off');
    el('connTxt').textContent = 'disconnected';
    boardEl.classList.add('inert');
    line('client', 'SOCKET', '·', declined
      ? 'CLOSED · the server declined the session: ' + declined
      : 'CLOSED · the server is gone, and this client has no rules to carry on with. Reload to retry');
  }

  function connect() {
    var saved = recall();
    el('connTxt').textContent = 'connecting';
    socket = new WebSocket(endpoint.href);

    socket.addEventListener('open', function () {
      live = true;
      el('led').classList.remove('off');
      socket.send(JSON.stringify({
        type: 'HELLO',
        match_id: saved.match_id || null,
        player_token: saved.player_token || null
      }));
      line('client', 'HELLO', 'match=' + (saved.match_id || 'new'), '→ sent');
    });

    socket.addEventListener('message', receive);
    socket.addEventListener('close', dead);
    socket.addEventListener('error', dead);
  }

  // Markup and nothing more: no dot, no role, no tabindex, no listener. It exists so the pane
  // reads as a board rather than as a hole before anyone has asked the server for one.
  function placeholder() {
    var index = 48;

    while (index--) {
      var tile = document.createElement('div');
      tile.className = 'tile';
      boardEl.appendChild(tile);
    }
  }

  // Not on load: a socket for every visitor costs a connection and shows the server an address
  // nobody offered, on a page whose note says nothing leaves your machine until you press this.
  el('start').addEventListener('click', function () {
    this.remove();
    connect();
  });

  // A fact about how this page is configured, not a claim that anything has happened.
  el('endpoint').textContent = endpoint.href;
  placeholder();
})();
