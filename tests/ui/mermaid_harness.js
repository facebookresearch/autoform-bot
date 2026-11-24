// Runs blueprint-mermaid.js (argv[2]) for tests/ui/test_render.py, against a stub page holding the diagrams in
// argv[3] and a stub Mermaid, and prints what each run asked for, drew and reported, as JSON.
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");

function settle() { return new Promise((resolve) => setImmediate(resolve)); }

async function run(blocks, preloaded, refused, named) {
  const calls = [], injected = [], observers = [], errors = [], selectors = [];
  let scheme = null, inFlight = 0, mostInFlight = 0;
  const elements = blocks.map((block) => ({className: block[0], textContent: block[1], innerHTML: ""}));
  const mermaid = {
    initialize: (config) => calls.push(
      ["initialize", config.securityLevel, config.htmlLabels, config.flowchart.htmlLabels, config.theme, config.secure,
       config.startOnLoad]
    ),
    // A render settles on a later turn, so two drawn at once would overlap.
    render: (id, text) => {
      calls.push(["render", text]);
      inFlight += 1;
      mostInFlight = Math.max(mostInFlight, inFlight);
      return settle().then(() => {
        inFlight -= 1;
        if (text.indexOf("broken") >= 0) throw new Error("parse error");
        return {svg: "<svg>" + text + "</svg>", bindFunctions: () => calls.push(["bind"])};
      });
    },
  };
  const context = {
    console: {error: (message) => errors.push(message)},
    document: {
      // Only the diagrams match, so a broader selector finds none.
      querySelectorAll: (selector) => { selectors.push(selector); return selector === ".mermaid" ? elements : []; },
      body: {getAttribute: () => scheme},
      createElement: () => ({}),
      head: {appendChild: (element) => injected.push(element)},
    },
    MutationObserver: function (callback) { observers.push(callback); this.observe = () => {}; },
  };
  if (preloaded) context.mermaid = mermaid;
  // An element with the id "mermaid" is a global of that name until the library loads.
  if (named) context.mermaid = named;
  vm.createContext(context);
  vm.runInContext(source, context);
  // A library the browser refuses fires its error event instead of its load event.
  if (!preloaded && injected.length && refused) injected[0].onerror();
  else if (!preloaded && injected.length) { context.mermaid = mermaid; injected[0].onload(); }
  // A switch to the dark scheme while the first pass is still drawing redraws
  // every diagram, still locked down, once that pass is done.
  scheme = "slate";
  observers.forEach((callback) => callback([{attributeName: "data-md-color-scheme"}]));
  for (let turn = 0; turn < 100; turn++) await settle();
  return {
    calls, errors, mostInFlight, selectors,
    sources: injected.map((element) => [element.src, element.integrity, element.crossOrigin]),
    drawn: elements.map((element) => element.innerHTML),
  };
}

(async () => {
  console.log(JSON.stringify({
    none: await run([], false),
    loaded: await run(JSON.parse(process.argv[3]), false),
    preloaded: await run([["mermaid", "graph TD\n  c --> d"]], true),
    refused: await run([["mermaid", "graph TD\n  e --> f"]], false, true),
    named: await run([["mermaid", "graph TD\n  g --> h"]], false, false, {id: "mermaid", tagName: "H2"}),
    collection: await run([["mermaid", "graph TD\n  i --> j"]], false, false, {length: 2, 0: {}, 1: {}}),
  }));
})();
