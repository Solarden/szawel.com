// Renders state, sends clicks, and holds no rules: legal moves arrive in every state message,
// so there is nothing here that could disagree with the engine.
(function () {
  'use strict';

  var el = function (id) { return document.getElementById(id); };
  var boardEl = el('board');
  var logEl = el('log');
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
    PROTOCOL: 'not a valid command'
  };

  var endpoint = new URL('/ws/play', location.href);
  endpoint.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';

  var socket = null;
  var tiles = [];
  var legal = new Set();
  var seq = 0;
  var logSeq = 0;
  var pending = new Map();
  var live = false;
  var closed = false;

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
    boardEl.style.gridTemplateColumns = 'repeat(' + state.cols + ', 1fr)';
    boardEl.style.aspectRatio = state.cols + '/' + state.rows;

    state.terrain.forEach(function (_, index) {
      var tile = document.createElement('div');
      // A div with a click handler is unreachable without a mouse, and the board is the whole
      // game. Literal values only: nothing server-supplied reaches an attribute here.
      tile.setAttribute('role', 'button');
      tile.tabIndex = 0;
      tile.appendChild(span('dot', ''));
      tile.addEventListener('click', function () { move(index); });
      tile.addEventListener('keydown', function (event) {
        // What the reader pressed, not where it sits on the board — a tile is activated the
        // way any button is, and that is the layout-aware field.
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          move(index);
        }
      });
      boardEl.appendChild(tile);
      tiles.push(tile);
    });
  }

  // Ownership and terrain are painted, and the two sides sit 1.89:1 apart in luminance —
  // nothing to a red-green colour blind reader. This is that same state in words.
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
      tiles[index].setAttribute('aria-label', describe(state, index, owner));
    });

    el('sYou').textContent = state.scores.you;
    el('sServer').textContent = state.scores.server;
    el('turn').textContent = state.over ? 'finished' : state.turn;
    boardEl.classList.toggle('inert', state.over || !live);
  }

  // Every click is sent. The browser decides nothing — highlighting is a hint, not a gate,
  // and an illegal click earns a reason code from the server, which is the thing on show.
  function move(index) {
    if (!live) {
      return;
    }

    seq += 1;
    pending.set(seq, performance.now());
    socket.send(JSON.stringify({ type: 'MOVE', tile: index, seq: seq }));
    line('client', 'MOVE', 'tile=' + index, '→ sent');
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
        'ACCEPTED' + took);
    }

    line('server', 'STATE', 'state=' + message.digest, 'broadcast → ' + plural(message.clients));
  }

  function receive(event) {
    var message = JSON.parse(event.data);
    // Bytes, which is what the tile claims — String.length counts UTF-16 units.
    metric('mPay', BYTES.encode(event.data).length, 'B');

    if (message.type === 'WELCOME') {
      remember(message);
      legal = new Set(message.legal_moves);
      presence(message.clients);
      render(message.state);
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

      // A malformed frame is refused without ever being a move, so the event column
      // must not claim it was one.
      var aMove = message.tile != null;
      var gloss = REASONS[message.reason];
      line('client', aMove ? 'MOVE' : 'COMMAND', aMove ? 'tile=' + message.tile : '—',
        'REJECTED ' + message.reason + (gloss ? ' — ' + gloss : ''));

      return;
    }

    if (message.type === 'OVER') {
      line('server', 'OVER', 'winner=' + (message.winner || 'draw'),
        message.scores.you + ':' + message.scores.server);
    }
  }

  function dead() {
    if (closed) {
      return;
    }

    closed = true;
    live = false;
    // A highlight is a claim about what the server would accept, and there is no server now.
    legal = new Set();
    tiles.forEach(function (tile) { tile.classList.remove('legal'); });
    el('led').classList.add('off');
    el('connTxt').textContent = 'disconnected';
    boardEl.classList.add('inert');
    line('client', 'SOCKET', '—',
      'CLOSED — the server is gone, and this client has no rules to carry on with');
  }

  function connect() {
    var saved = recall();
    el('endpoint').textContent = endpoint.href;
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

  connect();
})();
