// A small DOM stand-in for running the whole-project map client in Node (tests only; not browser evidence).
// Usage: node project_map_dom.js CLIENT MAP_DIR SCRIPT
// SCRIPT is evaluated after the client mounts, with `app`, `wait`, `$`, `$$`, `emit` and `assert` in scope.
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm"), assert = require("assert");
const [clientPath, mapDir, scriptPath] = process.argv.slice(2);

class Node_ {
  constructor(tag, ns) {
    this.tagName = String(tag).toUpperCase(); this.localName = String(tag); this.namespaceURI = ns || null;
    this.children = []; this.parentNode = null; this.attributes = new Map(); this.listeners = new Map();
    this.dataset = {}; this.style = {}; this._text = ""; this.hidden = false; this.value = ""; this.checked = false; this.type = "";
    const self = this;
    this.classList = {
      add: (...names) => names.forEach(n => self._classes().add(n) && self._syncClass()),
      remove: (...names) => { names.forEach(n => self._classes().delete(n)); self._syncClass(); },
      contains: n => self._classes().has(n),
      toggle: (n, on) => { const has = self._classes().has(n); const want = on === undefined ? !has : !!on; if (want) self._classes().add(n); else self._classes().delete(n); self._syncClass(); return want; },
    };
  }
  _classes() { if (!this.__classes) this.__classes = new Set(String(this.attributes.get("class") || "").split(/\s+/).filter(Boolean)); return this.__classes; }
  _syncClass() { this.attributes.set("class", Array.from(this._classes()).join(" ")); }
  get className() { return this.attributes.get("class") || ""; }
  set className(v) { this.attributes.set("class", String(v)); this.__classes = null; }
  get id() { return this.attributes.get("id") || ""; }
  set id(v) { this.attributes.set("id", String(v)); }
  get href() { return this.attributes.get("href") || ""; }
  set href(v) { this.attributes.set("href", String(v)); }
  get placeholder() { return this.attributes.get("placeholder") || ""; }
  set placeholder(v) { this.attributes.set("placeholder", String(v)); }
  setAttribute(name, value) {
    value = String(value);
    if (name === "class") this.className = value; else this.attributes.set(name, value);
    if (name.startsWith("data-")) this.dataset[name.slice(5).replace(/-([a-z])/g, (_m, c) => c.toUpperCase())] = value;
  }
  getAttribute(name) { if (name === "class") return this.className || null; return this.attributes.has(name) ? this.attributes.get(name) : null; }
  hasAttribute(name) { return this.attributes.has(name); }
  removeAttribute(name) { this.attributes.delete(name); }
  appendChild(child) { if (child.parentNode) child.parentNode.removeChild(child); child.parentNode = this; this.children.push(child); return child; }
  append(...nodes) { nodes.forEach(n => this.appendChild(typeof n === "string" ? document.createTextNode(n) : n)); }
  removeChild(child) { const i = this.children.indexOf(child); if (i >= 0) this.children.splice(i, 1); child.parentNode = null; return child; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  replaceChildren(...nodes) { this.children.slice().forEach(c => this.removeChild(c)); this.append(...nodes); }
  get firstChild() { return this.children[0] || null; }
  get textContent() { return this.tagName === "#TEXT" ? this._text : this._text + this.children.map(c => c.textContent).join(""); }
  set textContent(v) { this.children.slice().forEach(c => this.removeChild(c)); this._text = String(v); }
  addEventListener(type, fn) { if (!this.listeners.has(type)) this.listeners.set(type, []); this.listeners.get(type).push(fn); }
  removeEventListener(type, fn) { const list = this.listeners.get(type) || []; const i = list.indexOf(fn); if (i >= 0) list.splice(i, 1); }
  dispatch(type, props) {
    const event = Object.assign({type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}}, props || {});
    for (let at = this; at; at = at.parentNode) (at.listeners.get(type) || []).forEach(fn => fn.call(at, event));
    (document.listeners.get(type) || []).forEach(fn => fn(event));
    return event;
  }
  contains(other) { for (let at = other; at; at = at.parentNode) if (at === this) return true; return false; }
  closest(selector) { for (let at = this; at && at !== document; at = at.parentNode) if (matches(at, selector)) return at; return null; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) { const out = []; const walk = n => n.children.forEach(c => { if (matches(c, selector)) out.push(c); walk(c); }); walk(this); return out; }
  getBoundingClientRect() { return {x: 0, y: 0, left: 0, top: 0, width: 1200, height: 800, right: 1200, bottom: 800}; }
  focus() { document.activeElement = this; }
  select() {}
  setPointerCapture() {}
}
function matches(node, selector) {
  return selector.split(",").some(part => {
    part = part.trim();
    const attr = part.match(/^\[([a-z-]+)\]$/); if (attr) return node.attributes.has(attr[1]);
    if (part.startsWith(".")) return part.slice(1).split(".").every(c => node.classList.contains(c));
    if (part.startsWith("#")) return node.id === part.slice(1);
    return node.localName === part;
  });
}
const document = new Node_("#document");
document.activeElement = null;
document.createElement = tag => new Node_(tag);
document.createElementNS = (ns, tag) => new Node_(tag, ns);
document.createTextNode = text => { const n = new Node_("#text"); n._text = String(text); return n; };
document.documentElement = new Node_("html");
document.body = new Node_("body");
document.documentElement.appendChild(document.body);
const host = new Node_("div"); host.setAttribute("data-project-map", ""); host.setAttribute("data-map", "file:///map/");
host.setAttribute("data-routes", "file:///dependencies/index.json"); host.setAttribute("data-home", "../");
document.body.appendChild(host);
document.querySelector = s => document.documentElement.querySelector(s);
document.querySelectorAll = s => document.documentElement.querySelectorAll(s);
document.addEventListener = (type, fn) => { if (!document.listeners.has(type)) document.listeners.set(type, []); document.listeners.get(type).push(fn); };

const frames = [];
// MAP_HASH opens the page at a hash, as a deep link would.
const historyStack = [{state: null, url: process.env.MAP_HASH || "/map/"}];
let historyIndex = 0;
const location = {href: "file:///map/", pathname: "/map/", search: "", hash: process.env.MAP_HASH || ""};
function setUrl(url) { if (url) { const hash = url.startsWith("#") ? url : ""; location.hash = hash; } }
const history = {
  get state() { return historyStack[historyIndex].state; },
  pushState(state, _t, url) { historyStack.splice(historyIndex + 1); historyStack.push({state, url}); historyIndex++; setUrl(url); },
  replaceState(state, _t, url) { historyStack[historyIndex] = {state, url: url || historyStack[historyIndex].url}; setUrl(url); },
  get length() { return historyStack.length; },
  back() { if (historyIndex > 0) { historyIndex--; setUrl(historyStack[historyIndex].url); (window.listeners.get("popstate") || []).forEach(fn => fn({state: historyStack[historyIndex].state})); } },
  forward() { if (historyIndex < historyStack.length - 1) { historyIndex++; setUrl(historyStack[historyIndex].url); (window.listeners.get("popstate") || []).forEach(fn => fn({state: historyStack[historyIndex].state})); } },
};
const window = {listeners: new Map(), addEventListener(type, fn) { if (!this.listeners.has(type)) this.listeners.set(type, []); this.listeners.get(type).push(fn); }};
const errors = [];
// The network, for scripts: MAP_HOLD and MAP_FAIL (comma-separated files) hold or fail a file from the start;
// net.release lets a held file through, and net.log lists every file asked for, in order.
const held = new Map(), failing = new Map();
(process.env.MAP_HOLD || "").split(",").filter(Boolean).forEach(name => held.set(name, []));
(process.env.MAP_FAIL || "").split(",").filter(Boolean).forEach(name => failing.set(name, (failing.get(name) || 0) + 1));
const net = {
  log: [],
  hold(name) { if (!held.has(name)) held.set(name, []); },
  release(name) { const waiting = held.get(name) || []; held.delete(name); waiting.forEach(go => go()); },
  fail(name, times) { failing.set(name, times || 1); },
};
function respond(relative) {
  try { const text = fs.readFileSync(path.join(mapDir, relative), "utf8"); return {ok: true, status: 200, json: () => Promise.resolve(JSON.parse(text))}; }
  catch (e) { return {ok: false, status: 404, json: () => Promise.reject(e)}; }
}
const context = {
  document, window, location, history, URL, URLSearchParams, Map, Set, Promise, Math, JSON, Object, Array, String, Number, Infinity, isNaN, encodeURIComponent, decodeURIComponent,
  console: {log: (...a) => console.log(...a), error: (...a) => errors.push(a.map(String).join(" ")), warn: () => {}},
  setTimeout: (fn) => { frames.push(fn); return frames.length; }, clearTimeout: () => {},
  requestAnimationFrame: fn => { frames.push(() => fn(Date.now() + 10000)); return frames.length; }, cancelAnimationFrame: () => {},
  performance: {now: () => 0},
  matchMedia: () => ({matches: true}),
  getComputedStyle: () => ({getPropertyValue: () => "#888888"}),
  MutationObserver: class { observe() {} },
  ResizeObserver: class { observe() {} },
  navigator: {},
  fetch: url => {
    const relative = String(url).replace("file:///map/", "");
    net.log.push(relative);
    if (failing.get(relative) > 0) { failing.set(relative, failing.get(relative) - 1); return Promise.reject(new TypeError("the network dropped " + relative)); }
    if (held.has(relative)) return new Promise(resolve => held.get(relative).push(() => resolve(respond(relative))));
    return Promise.resolve(respond(relative));
  },
};
Object.assign(window, context);
context.window = window;
vm.createContext(context);
vm.runInContext(fs.readFileSync(clientPath, "utf8"), context, {filename: "project-map.js"});

async function wait(rounds) {
  for (let r = 0; r < (rounds || 12); r++) {
    await new Promise(resolve => setImmediate(resolve));
    const batch = frames.splice(0);
    batch.forEach(fn => { try { fn(); } catch (e) { errors.push(String(e && e.stack || e)); } });
  }
}
const $ = s => host.querySelector(s), $$ = s => host.querySelectorAll(s);
function emit(node, type, props) { return node.dispatch(type, props); }
(async () => {
  await wait(20);
  const body = fs.readFileSync(scriptPath, "utf8");
  const run = new Function("app", "wait", "$", "$$", "emit", "assert", "history", "location", "errors", "net", "return (async () => {" + body + "})();");
  try {
    await run(host, wait, $, $$, emit, assert, history, location, errors, net);
  } catch (e) {
    console.error("SCRIPT FAILED:", e && e.stack || e);
    process.exitCode = 1;
  }
  if (errors.length) { console.error("CLIENT ERRORS:\n" + errors.slice(0, 10).join("\n")); process.exitCode = 1; }
})();
