"use strict";
// Typeset the article formulas `autoform site check --mathjax` sends, as the site's articles input does, and
// report the ones that fail.
//
//   node typeset.cjs MATHJAX_PACKAGE JOB_JSON > failed.json
//
// The job is {"settings": the site's TeX settings for its articles, "formulas": [text, ...]}, each text a marked
// formula element's, delimiters and all. MathJax finds the formulas in each text with those settings' delimiters
// and typesets each alone, with a new TeX state. A text in which it finds none, as when a brace is left open,
// shows as written, and the TeX between its delimiters is typeset for the reason. Written out are those that
// fail: {"formula": the text's index, "display": ..., "tex": ..., "error": message} for one MathJax rejects, or
// "undefined": [command, ...] in place of "error" for the commands it does not know, which the site shows in red.
// On a failure, the last line on stderr says why.

const fs = require("fs");
const path = require("path");

// MathJax reports to the console; the results alone go to stdout.
console.log = console.info = console.warn = (...parts) => process.stderr.write(parts.join(" ") + "\n");

const ENTITIES = {amp: "&", lt: "<", gt: ">", quot: '"', apos: "'"};
const unescape = text =>
  text.replace(/&(#x[0-9a-f]+|#[0-9]+|[a-z]+);/gi, (whole, name) =>
    name[0] !== "#"
      ? ENTITIES[name.toLowerCase()] ?? whole
      : String.fromCodePoint(name[1].toLowerCase() === "x" ? parseInt(name.slice(2), 16) : parseInt(name.slice(1), 10))
  );

let job;
Promise.resolve()
  .then(() => {
    job = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
    return require(path.resolve(process.argv[2], "es5", "node-main.js")).init({
      loader: {load: ["input/tex-full", "adaptors/liteDOM"]},
      tex: job.settings,
      startup: {typeset: false},
    });
  })
  .then(MathJax => {
    const input = MathJax.startup.input[0];
    // Display delimiters first, as "$$" starts with "$".
    const delimiters = [
      ...job.settings.displayMath.map(pair => [pair, true]),
      ...job.settings.inlineMath.map(pair => [pair, false]),
    ];
    // What is wrong with one formula: {error}, {undefined}, or null.
    const problem = (tex, display) => {
      MathJax.texReset();
      let mml;
      try {
        mml = MathJax.tex2mml(tex, {display});
      } catch (error) {
        return {error: String((error && error.message) || error)};
      }
      const error = /<merror[^>]*\bdata-mjx-error="([^"]*)"/.exec(mml);
      if (error || mml.includes("<merror")) return {error: error ? unescape(error[1]) : "error"};
      const red = [...mml.matchAll(/<mtext mathcolor="red"[^>]*>([^<]*)<\/mtext>/g)].map(match => unescape(match[1]));
      return red.length ? {undefined: [...new Set(red)]} : null;
    };
    const failed = [];
    job.formulas.forEach((text, formula) => {
      // An escaped delimiter is text, not a formula.
      const found = input.findMath([text]).filter(item => item.display !== null);
      if (!found.length) {
        const shown = text.trim();
        const fits = ([[open, close]]) =>
          shown.length >= open.length + close.length && shown.startsWith(open) && shown.endsWith(close);
        const [[open, close], display] = delimiters.find(fits) || [["", ""], false];
        const tex = shown.slice(open.length, shown.length - close.length);
        const {error} = problem(tex, display) || {};
        failed.push({formula, display, tex, error: error || "MathJax finds no formula in it"});
      }
      for (const {math, display} of found) {
        const result = problem(math, display);
        if (result) failed.push({formula, display, tex: math, ...result});
      }
    });
    process.stdout.write(JSON.stringify(failed));
  })
  .catch(error => {
    process.stderr.write(String((error && error.message) || error).split("\n")[0] + "\n");
    process.exitCode = 1;
  });
