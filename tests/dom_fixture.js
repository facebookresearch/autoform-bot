"use strict";
// Runs one test body against a real page, for tests/dom_fixture.py. The request
// comes on stdin as JSON: the page's HTML and URL, the body (the text of an
// async function) and the arguments it gets. What the body prints goes to
// stdout, followed by what it returns, as JSON.
const fs = require("fs"), vm = require("vm");
const {JSDOM} = require("jsdom");

const request = JSON.parse(fs.readFileSync(0, "utf8"));
const dom = new JSDOM(request.page, {url: request.url, runScripts: "outside-only", pretendToBeVisual: true});
const {window} = dom, errors = [];
// jsdom's console sends the page's errors and its own (an exception in an
// event listener, say, with the exception itself second) to Node's
// console.error, so they all land here, with their stacks.
console.error = (...parts) => errors.push(parts.map(part => part && part.stack || String(part)).join(" "));
// A production file runs whole, as a classic script in the page's own realm.
const load = file => vm.runInContext(fs.readFileSync(file, "utf8"), dom.getInternalVMContext(), {filename: file});
const AsyncFunction = (async () => {}).constructor;
const body = new AsyncFunction("window", "document", "args", "errors", "require", "load", request.body);
let settled = false;
// Called before any await, since a page can still be loading when the body starts.
// Closing the window on every path stops its timers, which would keep Node running.
body(window, window.document, request.args, errors, require, load).finally(() => window.close())
  .then(result => {
    if (errors.length) throw new Error("the page reported errors:\n" + errors.slice(0, 10).join("\n"));
    if (result !== undefined) process.stdout.write(JSON.stringify(result));
  })
  .catch(error => {
    process.stderr.write("the test body failed: " + (error && error.stack || error) + "\n");
    process.exitCode = 1;
  })
  .finally(() => { settled = true; });
// A body that waits on nothing Node can still run would otherwise exit 0 with no result.
process.on("beforeExit", () => {
  if (!settled) {
    process.stderr.write("the test body never finished\n");
    process.exit(1);
  }
});
