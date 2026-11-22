"""Run the book's explorer on real pages in the pinned jsdom (:mod:`tests.dom_fixture`).

:func:`build_site` renders a blueprint and builds it with MkDocs, as a published site is built. :func:`run_book`
opens one of its pages, serves the site's files to the page's fetch and runs the site's own
``javascripts/blueprint-dag.js``, the compiled explorer publication ships, as the page's script. :func:`run_router`
runs the router alone, ``blueprint-navigation.js`` whole, on real pages written from small specs, for transport and
history cases a built site cannot stage. Neither is browser evidence: jsdom draws nothing, so geometry, scrolling and
what a reader can see stay with the WebKit suite.

Both bodies get the helpers of :data:`COMMON`: one clock for every window, which the body advances with ``tick``;
``emit`` and ``click`` for events; ``waitFor``; and what jsdom lacks (``TextDecoder``, ``crypto.subtle``,
``structuredClone``, ``matchMedia``, ``scrollIntoView``, ``history.scrollRestoration``), from Node.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from tests.dom_fixture import run_dom

ROOT = Path(__file__).resolve().parents[1]
#: Where the pages are opened: the site's root.
ORIGIN = "https://blueprint.example.invalid/"
ROUTER = ROOT / "autoform_cli/ui/assets/blueprint-navigation.js"

#: An article with a statement, as an author writes one: format with kind, lean, title, body and uses.
ARTICLE = "---\ndeclaration: {kind}\nstatement: formalized\nlean: {lean}\n---\n\n# {title}\n\n{body}\n\n## Depends on\n\n{uses}\n"

COMMON = r"""
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const util = require('util'), {webcrypto} = require('crypto');
const location = window.location;
// Timers and animation frames of every window wait for tick(), which runs those due in time order; a frame is a 16 ms
// timer. performance.now() reads the same clock, so time moves only when the body says.
const clock = {now: 0, timers: new Map(), seq: 0};
// Media queries the windows match, by query: set before the page reads them.
const media = {};
function install(win) {
  win.setTimeout = (callback, delay, ...rest) => {
    const id = ++clock.seq;
    clock.timers.set(id, {id, at: clock.now + Math.max(0, Number(delay) || 0), callback, rest});
    return id;
  };
  win.clearTimeout = id => { clock.timers.delete(id); };
  win.requestAnimationFrame = callback => win.setTimeout(() => callback(clock.now), 16);
  win.cancelAnimationFrame = id => { clock.timers.delete(id); };
  win.performance.now = () => clock.now;
  if (!win.TextDecoder) Object.assign(win, {TextDecoder: util.TextDecoder, TextEncoder: util.TextEncoder});
  if (!win.crypto || !win.crypto.subtle) Object.defineProperty(win, 'crypto', {value: webcrypto, configurable: true});
  if (!win.structuredClone) win.structuredClone = value => structuredClone(value);
  win.matchMedia = query => ({matches: Boolean(media[query]), media: query, addEventListener() {}, removeEventListener() {}});
  if (!win.Element.prototype.scrollIntoView) win.Element.prototype.scrollIntoView = function () {};
  if (!('scrollRestoration' in win.history))
    Object.defineProperty(win.history, 'scrollRestoration', {value: 'auto', writable: true, configurable: true});
  return win;
}
async function flush() { for (let n = 0; n < 4; n++) await new Promise(resolve => setImmediate(resolve)); }
async function tick(ms = 0) {
  const end = clock.now + ms;
  for (;;) {
    await flush();
    let due = null;
    for (const timer of clock.timers.values()) if (timer.at <= end && (!due || timer.at < due.at)) due = timer;
    if (!due) break;
    clock.timers.delete(due.id); clock.now = due.at;
    try { due.callback(...due.rest); } catch (error) { errors.push(String(error && error.stack || error)); }
  }
  clock.now = end; await flush();
}
async function waitFor(predicate, message, rounds = 400) {
  for (let round = 0; round < rounds; round++) { if (predicate()) return; await tick(5); }
  assert.fail(message || 'waited in vain');
}
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; }
// jsdom has no PointerEvent: a pointer event is a mouse event carrying the pointer's id and type.
function emit(node, type, {pointerId, pointerType, ...init} = {}) {
  const win = (node.ownerDocument || node).defaultView || node;
  const Kind = type === 'wheel' ? win.WheelEvent : type.startsWith('key') ? win.KeyboardEvent :
    /^(focus|blur)/.test(type) ? win.FocusEvent : /^(click|dblclick|mouse|pointer)/.test(type) ? win.MouseEvent : win.Event;
  const event = new Kind(type, {bubbles: !/^(focus|blur)$/.test(type), cancelable: true, ...init});
  if (type.startsWith('pointer')) Object.assign(event, {pointerId, pointerType});
  node.dispatchEvent(event);
  return event;
}
// A click on a link whose default jsdom cannot perform (it loads no other document) is cancelled once the page has had
// it. Returns whether the page took the click: whether it cancelled it itself.
function click(node, init = {}) {
  const win = node.ownerDocument.defaultView;
  let taken = null;
  const shield = event => { taken = event.defaultPrevented; event.preventDefault(); };
  win.addEventListener('click', shield);
  const event = emit(node, 'click', {button: 0, ...init});
  win.removeEventListener('click', shield);
  return taken === null ? event.defaultPrevented : taken;
}
function element(win, tag, attributes = {}, ...children) {
  const node = win.document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  for (const child of children) node.append(child);
  return node;
}
"""

#: The book on a built page: its files served from ``args.site``, the site's explorer loaded, the first commit made.
_BOOK = r"""
const site = args.site, root = new URL('/', location.href).href;
install(window);
const $ = selector => document.querySelector(selector), $$ = (selector, scope = document) => Array.from(scope.querySelectorAll(selector));
// The site's files, as a static server sends them: a folder's index.html for the folder. net.log lists each request,
// relative to the site's root; net.hold(name) holds a file until net.release(name); net.fail(name) fails its next
// request; net.route(href, init) may answer a request itself; args.files maps a path to JSON sent in place of the file.
const net = {log: [], held: new Map(), failing: new Set(), route: null,
  hold(name) { this.held.set(name, []); }, release(name) { (this.held.get(name) || []).forEach(go => go()); this.held.delete(name); },
  fail(name) { this.failing.add(name); }};
function respond(href, bytes, status = bytes ? 200 : 404) {
  return {ok: status >= 200 && status < 300, status, url: href, headers: {get: () => null},
    text: () => window.Promise.resolve(bytes ? Buffer.from(bytes).toString('utf8') : ''),
    arrayBuffer: () => window.Promise.resolve(bytes ? Uint8Array.from(bytes).buffer : new ArrayBuffer(0))};
}
function siteFile(href) {
  if (!href.startsWith(root)) return null;
  let file = path.join(site, decodeURIComponent(new URL(href).pathname));
  if (fs.existsSync(file) && fs.statSync(file).isDirectory()) file = path.join(file, 'index.html');
  return fs.existsSync(file) ? fs.readFileSync(file) : null;
}
window.fetch = (input, init = {}) => {
  const href = new URL(String(input), location.href).href, name = href.startsWith(root) ? href.slice(root.length) : href;
  net.log.push(name);
  if (net.route) { const answer = net.route(href, init); if (answer !== undefined) return window.Promise.resolve(answer); }
  if (net.failing.delete(name)) return window.Promise.reject(new window.TypeError('the network dropped ' + name));
  const served = args.files && Object.hasOwn(args.files, name) ? Buffer.from(JSON.stringify(args.files[name])) : null;
  const answer = () => respond(href, served || siteFile(href));
  if (!net.held.has(name)) return window.Promise.resolve(answer());
  return new window.Promise((resolve, reject) => {
    net.held.get(name).push(() => resolve(answer()));
    if (init.signal) init.signal.addEventListener('abort', () => reject(new window.DOMException('Aborted', 'AbortError')));
  });
};
// The book appends the whole map's script, which jsdom neither loads nor reports: args.map 'load' runs the site's map
// script and reports its load, 'fail' reports an error, and anything else leaves it pending, as on a slow network.
const append = document.head.appendChild.bind(document.head);
document.head.appendChild = node => {
  const added = append(node);
  if (node.tagName === 'SCRIPT' && node.src && args.map) window.setTimeout(() => {
    if (args.map === 'load') load(path.join(site, new URL(node.src).pathname));
    (args.map === 'load' ? node.onload : node.onerror).call(node, new window.Event(args.map === 'load' ? 'load' : 'error'));
  }, 0);
  return added;
};
Object.assign(media, args.media || {});
const host = () => document.querySelector('.bp-dag-viewer');
eval(args.before || '');
load(path.join(site, 'javascripts/blueprint-dag.js'));
if (args.ready) {
  await waitFor(() => document.readyState !== 'loading');
  await waitFor(() => host() && host().getAttribute('data-dag-ready') === 'true' && document.body.classList.contains('bp-dag-app-page') &&
    !host().hasAttribute('aria-busy') && document.querySelector('.bp-dag-search'), 'the book never committed its first page');
  await tick(200);
}
// Back and Forward, and the page's response to them.
async function back() { window.history.back(); await tick(0); await tick(200); }
async function forward() { window.history.forward(); await tick(0); await tick(200); }
"""


def build_site(project: Path) -> Path:
    """Render *project*'s blueprint and build its site with MkDocs, strictly; return the built site."""
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    from autoform_cli.ui.render import render_site

    render_site(project / "blueprint", project / "site-src", lean_root=project)
    build(load_config(config_file=str(project / "mkdocs.yml"), strict=True))
    return project / "site"


def run_book(
    site: Path, page: str, body: str, *, hash: str = "", before: str = "", media: Mapping[str, bool] | None = None,
    map: str | None = None, ready: bool = True, args: Mapping[str, object] | None = None,
    files: Mapping[str, object] | None = None, timeout: float = 60,
) -> str:
    """Open *page* of the built *site* (a path from its root, a folder for its index.html) at :data:`ORIGIN`, with
    *hash*, run the site's explorer on it and then *body*. *before* runs ahead of the explorer, and *media* names the
    media queries the page matches. *map* is how the book's whole-map script loads: ``"load"``, ``"fail"``, or not at
    all. *files* maps a path from the site's root to JSON sent in place of that file, such as a search index with a shape
    the site cannot make. With *ready*, the body starts once the book has made its first commit."""
    file = site / page
    if page.endswith("/") or file.is_dir():
        file = file / "index.html"
    job = {"site": str(site), "before": before, "media": dict(media or {}), "map": map, "ready": ready, "files": dict(files or {}),
           **(args or {})}
    return run_dom(COMMON + _BOOK + body, page=file.read_text(encoding="utf-8"), url=ORIGIN + page + hash, args=job,
                   timeout=timeout)


#: The router alone, on pages written from specs: setup() opens a new window per call. A spec gives a page's title,
#: route (its canonical address), data (its graph's address), catalog (the search index's) and scope, and optionally
#: prose. contents maps each address the router may fetch to a spec, written as that page's HTML, or to a payload,
#: sent as JSON.
_ROUTER = r"""
const {JSDOM} = require('jsdom');
const routerSource = fs.readFileSync(args.router, 'utf8');
const base = 'https://example.test/project/', absolute = value => new URL(value, base).href;
const escape = value => String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
// A reading as render publishes one, with what the router must remove or rebase: handlers, a script, a javascript:
// link, relative links and images, and an outside link it leaves alone.
function pageHTML(spec) {
  const viewer = spec.moved ? `<div class="bp-dag-viewer" data-page-href="${escape(spec.moved)}" hidden></div><p><a href="${escape(spec.moved)}">Continue to the mathematical page</a></p>`
    : `<div class="bp-dag-viewer" data-page-href="${escape(spec.route)}" data-graph-src="${escape(spec.data)}" data-catalog-src="${escape(spec.catalog || '../dependencies/index.json')}" data-scope-id="${escape(spec.scope)}"></div>`;
  const reading = spec.moved ? '<h1>This page moved</h1>' : `<div class="bp-reading-source" data-autoform-math-source="\\(L \\neq 0\\)" onclick="bad()">` +
    `<h1>${escape(spec.title)}</h1><p>${escape(spec.prose || 'Complete source statement and proof.')}</p>${spec.extra || ''}` +
    `<a href="b.html#theorem-b" onclick="bad()">B theorem</a><img src="figures/chart.svg" srcset="figures/chart.png 1x, figures/chart@2x.png 2x">` +
    `<a href="https://outside.test/paper.pdf">Source paper</a><a href="javascript:bad()">Bad link</a><script>bad()</script></div>`;
  return `<!doctype html><html><head><title>${escape(spec.title || 'This page moved')}</title></head><body>` +
    `<main class="bp-explorer-page" data-page-title="${escape(spec.title || '')}">${reading}${viewer}</main></body></html>`;
}
const graph = {schema: 'autoform-dag-view/v2', palette: {planned: {}}, title: 'Graph',
  nodes: [{id: 'a', title: 'A theorem', url: '../roadmap/a.html#theorem-a'}, {id: 'b', title: 'B theorem'}],
  edges: [{source: 'a', target: 'b', statement: 1, proof: 0}], view: {book: '../roadmap/index.html'}};
function index() {
  return {schema: 'autoform-dag-search/v1', nodes: [
    {id: 'roadmap', title: 'Root', kind: 'scope', result: false, url: '../dependencies.html', route: '../roadmap/index.html', data_url: '../dependencies.json', scope: 'roadmap'},
    {id: 'a', title: 'A theorem', kind: 'node', result: true, url: 'scopes/a.html#node=a', route: '../roadmap/a.html#theorem-a', data_url: 'a.json', scope: 'a', summary: 'Not the complete article'},
    {id: 'b', title: 'B theorem', kind: 'node', result: true, url: 'scopes/b.html#node=b', route: '../roadmap/b.html#theorem-b', data_url: 'b.json', scope: 'b'}
  ], relations: [['a', 'b', 1, 0]]};
}
const rootSpec = {title: 'Root', route: absolute('roadmap/index.html'), data: '../dependencies.json', scope: 'roadmap'};
const aSpec = {title: 'A topic', route: absolute('roadmap/a.html'), data: '../dependencies/a.json', scope: 'a'};
const bSpec = {title: 'B topic', route: absolute('roadmap/b.html'), data: '../dependencies/b.json', scope: 'b'};
// The router's index normalizer reads the clock every 128 rows and ends a batch once 25 ms have passed; this clock
// passes them every 4 reads, so a batch ends every 512 rows.
function steppedClock(app) { let read = 0; app.win.performance.now = () => read++ * 25 / 4; return app; }
// A new window on extra.initial (or the root page) at extra.url, with the router created over it. Its state is what the
// shell's captureState gives: app.state(value) sets it, and restoreState records what it was given.
function setup(extra = {}) {
  const initialSpec = extra.initial || rootSpec;
  const dom = new JSDOM(pageHTML(initialSpec), {url: extra.url || initialSpec.route || rootSpec.route, runScripts: 'outside-only', pretendToBeVisual: true});
  const win = install(dom.window), doc = win.document, commits = [], errors = [], restored = [], fetches = [];
  if (extra.scrollRestoration) win.history.scrollRestoration = extra.scrollRestoration;
  win.history.replaceState({material: 'preserved'}, '', win.location.href);
  let state = {mode: 'map', readingScroll: 0};
  const contents = new Map([
    [absolute('dependencies/index.json'), index()], [absolute('dependencies.json'), graph],
    [absolute('dependencies/a.json'), graph], [absolute('dependencies/b.json'), graph],
    [rootSpec.route, rootSpec], [aSpec.route, aSpec], [bSpec.route, bSpec]
  ]);
  function reply(value, url, status = 200) {
    const text = value && typeof value === 'object' && ('route' in value || 'moved' in value) && !('schema' in value) ? pageHTML(value) :
      typeof value === 'string' ? value : JSON.stringify(value);
    return {ok: status === 200, status, url, headers: {get() { return null; }}, text: async () => text};
  }
  const fetch = async (url, settings) => {
    fetches.push(url);
    if (extra.fetch) { const overridden = extra.fetch(url, settings, reply); if (overridden !== undefined) return overridden; }
    return reply(contents.get(url), url, contents.has(url) ? 200 : 404);
  };
  vm.runInContext(routerSource, dom.getInternalVMContext(), {filename: args.router});
  const initial = {doc, host: doc.querySelector('.bp-dag-viewer'), reading: doc.querySelector('.bp-reading-source'),
    root: doc.querySelector('.bp-explorer-page')};
  const nav = win.AutoformNavigation.create({host: initial.host, window: win, document: doc, fetch,
    onCommit(bundle) { commits.push(bundle); if (extra.commit) return extra.commit(bundle); },
    onError(error) { errors.push(error); },
    captureState() { return structuredClone(state); },
    restoreState(saved, bundle) { state = structuredClone(saved); restored.push(saved); if (extra.restore) return extra.restore(saved, bundle); },
    onIndex: extra.onIndex, onWikiLinks: extra.onWikiLinks,
    htmlEntries: extra.htmlEntries || 8, jsonEntries: 16, cacheBytes: extra.cacheBytes, timeout: extra.timeout, jsonLimit: extra.jsonLimit});
  return {nav, win, doc, dom, commits, errors, restored, fetches, contents, reply, initial,
    state(value) { state = value; }, getState() { return state; },
    // Back and Forward land once the window's traversal task runs.
    async back() { win.history.back(); await tick(0); }, async forward() { win.history.forward(); await tick(0); }};
}
async function until(predicate, message) { await waitFor(predicate, message || 'the router did not settle'); }
"""


def run_router(body: str, *, args: Mapping[str, object] | None = None, timeout: float = 60) -> str:
    """Run *body* with :data:`COMMON` and ``setup()``, which opens a window over a page made from a spec and creates
    the router, ``blueprint-navigation.js``, on it."""
    return run_dom(COMMON + _ROUTER + body, args={"router": str(ROUTER), **(args or {})}, timeout=timeout)
