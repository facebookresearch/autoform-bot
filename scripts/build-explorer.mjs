// Development-only compiler. Python publications ship the compiled classic script, not Node.
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { transform, version } from "esbuild";
import { minify } from "terser";

function sourceText(bytes) {
  // Match Python's UTF-8 text capture on LF and Windows CRLF checkouts.
  // Preserve a BOM; normalize line endings, not any source token or spelling.
  return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes).replace(/\r\n?/g, "\n");
}

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const destination = resolve(root, "autoform_cli/assets/blueprint-explorer.min.js");
const args = process.argv.slice(2);
if (args.length > 1 || (args.length && args[0] !== "--check")) {
  console.error("Usage: node scripts/build-explorer.mjs [--check]");
  process.exitCode = 2;
} else {
  if (version !== "0.28.2") throw new Error("The explorer compiler must be esbuild 0.28.2");
  const terserPackage = JSON.parse(await readFile(resolve(root, "node_modules/terser/package.json"), "utf8"));
  if (terserPackage.version !== "5.51.2") throw new Error("The explorer compressor must be Terser 5.51.2");
  // Capture both inputs before compiling; fingerprint their canonical LF text.
  const navigation = await readFile(resolve(root, "autoform_cli/assets/blueprint-navigation.js"));
  const viewer = await readFile(resolve(root, "autoform_cli/assets/blueprint-dag.js"));
  const source = [navigation, viewer].map(sourceText).join("\n");
  const digest = createHash("sha256").update(source, "utf8").digest("hex");
  const compiled = await transform(source, {
    loader: "js",
    sourcefile: "blueprint-explorer.js",
    format: "iife",
    target: "es2020",
    charset: "utf8",
    minifyIdentifiers: true,
    minifySyntax: true,
    minifyWhitespace: true,
    // Only explicitly private scene properties use $ names; payload/router fields stay intact.
    mangleProps: /^\$/,
    legalComments: "none",
    sourcemap: false,
  });
  // A safe syntax pass recovers room for real UI/compatibility behavior. Never rename public properties,
  // assume getters are pure, enable unsafe transforms or remove console calls.
  const compressed = await minify({ "blueprint-explorer.js": compiled.code }, {
    ecma: 2020,
    module: false,
    compress: { passes: 2, pure_getters: false, unsafe: false, drop_console: false },
    mangle: { properties: false },
    format: { ascii_only: false, comments: false },
    sourceMap: false,
  });
  if (typeof compressed.code !== "string") throw new Error("The explorer compressor produced no classic script");
  const banner = `/* autoform-explorer-source-sha256:${digest} */\n`;
  const output = Buffer.from(banner + compressed.code + "\n", "utf8");
  if (output.byteLength >= 144_000) {
    console.error(`Explorer bundle is ${output.byteLength} bytes; the integrated JavaScript limit is below 144,000 bytes. Source sha256 ${digest}`);
    process.exitCode = 1;
  } else if (args[0] === "--check") {
    let existing;
    try { existing = await readFile(destination); } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    if (!existing || !Buffer.from(sourceText(existing), "utf8").equals(output)) {
      console.error("Explorer bundle is stale or missing. Run npm run build:explorer.");
      process.exitCode = 1;
    } else {
      console.log(`Explorer bundle matches: ${output.byteLength} bytes; source sha256 ${digest}`);
    }
  } else {
    await writeFile(destination, output);
    console.log(`Built explorer: ${output.byteLength} bytes; source sha256 ${digest}`);
  }
}
