// Drives the real widget.js through a match with the keyboard, against a browser just big enough
// for it: elements, focus, and a socket the test answers. Run by test_client.py under node.
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.classes = new Set();
    this.style = {};
    this.dataset = {};
    this.tabIndex = -1;
    this.parentNode = null;
    this.text = '';
    this.scrollHeight = 0;
    this.scrollTop = 0;
    this.clientHeight = 0;
    const classes = this.classes;
    this.classList = {
      add: (...names) => names.forEach((n) => classes.add(n)),
      remove: (...names) => names.forEach((n) => classes.delete(n)),
      contains: (name) => classes.has(name),
      toggle: (name, on = !classes.has(name)) => (on ? classes.add(name) : classes.delete(name), on),
    };
  }

  set className(value) { this.classes.clear(); value.split(/\s+/).filter(Boolean).forEach((n) => this.classes.add(n)); }
  get className() { return [...this.classes].join(' '); }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map((c) => c.textContent).join(''); }
  get firstChild() { return this.children[0]; }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  removeChild(child) { this.children = this.children.filter((c) => c !== child); child.parentNode = null; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  closest() { return this.parentNode; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  focus() { document.activeElement = this; }
  fire(type, init = {}) {
    const event = Object.assign({ defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } }, init);
    (this.listeners[type] || []).forEach((fn) => fn.call(this, event));

    return event;
  }
}

const byId = {};
const node = (id, parent) => {
  byId[id] = new Element('div');

  if (parent) byId[parent].appendChild(byId[id]);

  return byId[id];
};
['board', 'leaders', 'led', 'connTxt', 'endpoint', 'sYou', 'sServer', 'mLag', 'mSrv', 'mPay', 'logwrap', 'pill']
  .forEach((id) => node(id));
node('start', 'board');
node('log', 'logwrap');
node('turn', 'pill');
byId.board.className = 'board inert';
byId.board.dataset.endpoint = 'wss://play.example/ws/play';

let socket = null;
global.document = {
  activeElement: null,
  getElementById: (id) => byId[id],
  createElement: (tag) => new Element(tag),
};
global.window = {};
global.location = { href: 'https://example.test/' };
global.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
global.WebSocket = class {
  constructor() { socket = this; this.listeners = {}; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener() {}
  send() {}
  close() {}
  emit(type, data) { (this.listeners[type] || []).forEach((fn) => fn(data === undefined ? {} : { data: JSON.stringify(data) })); }
};
console.info = () => {};

eval(fs.readFileSync(path.join(__dirname, '..', 'client', 'widget.js'), 'utf8'));

// An 8x6 board: water in column 3, the player at 0, the server at 7. Legal: 1 and 8.
const terrain = Array.from({ length: 48 }, (_, i) => (i % 8 === 2 ? 'water' : 'plain'));
const owners = Array.from({ length: 48 }, (_, i) => (i === 0 ? 'you' : i === 7 ? 'server' : null));
const welcome = {
  type: 'WELCOME', match_id: 'm1', player_token: 't', session_key: '00'.repeat(32), digest: 'd', clients: 1,
  legal_moves: [8, 1], leaderboard: [],
  state: { cols: 8, rows: 6, terrain, owners, scores: { you: 1, server: 1 }, over: false, turn: 'you', points: {} },
};
const tiles = () => byId.board.children.filter((c) => c.classes.has('tile'));
const stops = () => tiles().filter((t) => t.tabIndex === 0);
const focused = () => tiles().indexOf(document.activeElement);

// Start removes itself while focused: the focus goes to the board, then to the first legal tile.
byId.start.focus();
byId.start.fire('click');
assert.equal(document.activeElement, byId.board, 'start hands the focus to the board');
socket.emit('open');
socket.emit('message', welcome);
assert.equal(focused(), 1, 'the first playable tile takes the focus once the match is seated');
assert.equal(stops().length, 1, 'the board is one tab stop');

// Arrows move inside a row and down a column, and stop at the edges without scrolling the page.
let event = tiles()[1].fire('keydown', { key: 'ArrowLeft' });
assert.equal(focused(), 0);
event = tiles()[0].fire('keydown', { key: 'ArrowLeft' });
assert.equal(focused(), 0, 'left stops at the row start');
assert.ok(event.defaultPrevented, 'an arrow at the edge does not scroll the page');
tiles()[7].focus();
tiles()[7].fire('keydown', { key: 'ArrowRight' });
assert.equal(focused(), 7, 'right does not wrap onto the next row');
tiles()[0].fire('keydown', { key: 'ArrowDown' });
assert.equal(focused(), 8);
tiles()[8].fire('keydown', { key: 'ArrowRight' });
tiles()[9].fire('keydown', { key: 'ArrowRight' });
assert.equal(focused(), 10, 'the cursor may rest on water');
assert.deepEqual(stops().map((t) => tiles().indexOf(t)), [10], 'the tab stop follows the cursor');

// Decided: the board leaves the tab order and says so. Play again lands on a playable tile.
socket.emit('message', { type: 'OVER', winner: 'you', scores: { you: 2, server: 1 }, leaderboard: [] });
socket.emit('message', Object.assign({}, welcome, { type: 'STATE', state: Object.assign({}, welcome.state, { over: true }) }));
assert.equal(stops().length, 0, 'a decided board has no tab stop');
assert.ok(tiles().every((t) => t.getAttribute('aria-disabled') === 'true'));
const again = byId.board.children.find((c) => c.classes.has('result')).children.find((c) => c.tagName === 'button');
again.focus();
again.fire('click');
assert.equal(document.activeElement, byId.board, 'play again hands the focus to the board');
socket.emit('open');
socket.emit('message', welcome);
assert.equal(focused(), 1, 'a new match starts on a playable tile, not where the last cursor rested');
assert.ok(tiles().every((t) => t.getAttribute('aria-disabled') === 'false'));

// A dropped socket makes the board inert the same way.
socket.emit('close');
assert.equal(stops().length, 0);
console.log('widget keyboard: ok');
