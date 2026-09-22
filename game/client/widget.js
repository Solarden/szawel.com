// Renders state, sends clicks, and holds no rules: legal moves arrive in every state message,
// so there is nothing here that could disagree with the engine.
(function () {
  'use strict';

  var el = function (id) { return document.getElementById(id); };
  var boardEl = el('board');
  var logEl = el('log');
  var STORE = 'szawel.match';

  var endpoint = new URL('/ws/play', location.href);
  endpoint.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';

  var socket = null;
  var tiles = [];
  var legal = new Set();
  var seq = 0;
  var pending = new Map();
  var live = false;
  var closed = false;

  // Every line below is written by something that happened. Nothing is on a timer.

  function stamp() {
    var now = new Date();

    return now.toTimeString().slice(0, 8) + '.' + String(now.getMilliseconds()).padStart(3, '0');
  }

  function line(kind, arrow, text) {
    var row = document.createElement('div');
    row.className = 'ln ' + kind;
    row.appendChild(span('ts', stamp()));
    row.appendChild(span('ar', arrow));
    row.appendChild(span('', text));
    logEl.appendChild(row);
    logEl.scrollTop = logEl.scrollHeight;

    while (logEl.children.length > 40) {
      logEl.removeChild(logEl.firstChild);
    }
  }

  // textContent, not innerHTML: a reason code is server-supplied text on its way into the DOM.
  function span(cls, text) {
    var node = document.createElement('span');
    node.className = cls;
    node.textContent = text;

    return node;
  }

  function presence(clients) {
    el('connTxt').textContent = 'connected · ' + clients + (clients === 1 ? ' client' : ' clients');
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
      tile.appendChild(span('dot', ''));
      tile.addEventListener('click', function () { move(index); });
      boardEl.appendChild(tile);
      tiles.push(tile);
    });
  }

  function render(state) {
    if (!tiles.length) {
      build(state);
    }

    state.owners.forEach(function (owner, index) {
      tiles[index].className = 'tile ' + state.terrain[index]
        + (owner ? ' ' + owner : '')
        + (legal.has(index) ? ' legal' : '');
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
    line('you', '→', 'MOVE tile=' + index + ' seq=' + seq);
  }

  function receive(event) {
    var message = JSON.parse(event.data);
    metric('mPay', event.data.length, 'B');

    if (message.type === 'WELCOME') {
      remember(message);
      legal = new Set(message.legal_moves);
      presence(message.clients);
      render(message.state);
      line('sync', '⇄', 'WELCOME match=' + message.match_id + ' clients=' + message.clients);

      return;
    }

    if (message.type === 'STATE') {
      legal = new Set(message.legal_moves);
      presence(message.clients);
      render(message.state);

      if (message.last === null) {
        line('sync', '⇄', 'STATE clients=' + message.clients);

        return;
      }

      // Matched on seq, not merely on 'you': a second tab's move broadcasts as 'you' too,
      // and timing that would report someone else's round-trip as this client's.
      if (pending.has(message.last.seq)) {
        metric('mLag', Math.round(performance.now() - pending.get(message.last.seq)), 'ms');
        pending.delete(message.last.seq);
      }

      metric('mSrv', message.server_ms, 'ms');
      line(message.last.by, message.last.by === 'you' ? '✓' : '←',
        'STATE ' + message.last.by + ' tile=' + message.last.tile
        + ' broadcast → ' + message.clients + ' client(s)');

      return;
    }

    if (message.type === 'REJECTED') {
      // A refused move gets no STATE, so nothing else would ever retire its timer.
      pending.delete(message.seq);

      line('bad', '✕',
        'REJECTED ' + message.reason + ' tile=' + message.tile + ' seq=' + message.seq);

      return;
    }

    if (message.type === 'OVER') {
      line('sync', '■', 'OVER winner=' + (message.winner || 'draw')
        + ' ' + message.scores.you + ':' + message.scores.server);
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
    line('bad', '○', 'SOCKET closed — the server is gone, and this client has no rules to carry on with');
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
      line('sync', '⇄', 'HELLO' + (saved.match_id ? ' resume match=' + saved.match_id : ''));
    });

    socket.addEventListener('message', receive);
    socket.addEventListener('close', dead);
    socket.addEventListener('error', dead);
  }

  connect();
})();
