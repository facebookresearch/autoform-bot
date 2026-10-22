// Development-only compiler. Python publications ship the compiled classic script, not Node.
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { transform, version } from "esbuild";
import { minify } from "terser";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const destination = resolve(root, "autoform_cli/assets/blueprint-explorer.min.js");
const vendorDestination = resolve(root, "autoform_cli/assets/blueprint-dagre.vendor.js");
const args = process.argv.slice(2);
if (args.length > 1 || (args.length && args[0] !== "--check")) {
  console.error("Usage: node scripts/build-explorer.mjs [--check]");
  process.exitCode = 2;
} else {
  if (version !== "0.28.2") throw new Error("The explorer compiler must be esbuild 0.28.2");
  const terserPackage = JSON.parse(await readFile(resolve(root, "node_modules/terser/package.json"), "utf8"));
  if (terserPackage.version !== "5.51.2") throw new Error("The explorer compressor must be Terser 5.51.2");
  const dagreRoot = resolve(root, "node_modules/@dagrejs/dagre");
  const dagrePackage = JSON.parse(await readFile(resolve(dagreRoot, "package.json"), "utf8"));
  const graphlibPackage = JSON.parse(await readFile(resolve(root, "node_modules/@dagrejs/graphlib/package.json"), "utf8"));
  if (dagrePackage.version !== "3.1.1" || graphlibPackage.version !== "4.0.5")
    throw new Error("The layout engine must be Dagre 3.1.1 with Graphlib 4.0.5");
  const license = (await readFile(resolve(dagreRoot, "dist/dagre.min.js.LEGAL.txt"), "utf8")).trim();
  const engine = (await readFile(resolve(dagreRoot, "dist/dagre.min.js"), "utf8"))
    .replace(/(?:^|\n)\/\/[#@]\s*sourceMappingURL=[^\r\n]*(?:\r?\n|$)/g, "");
  // Use the pinned distribution unchanged except its source-map comment. The
  // private lexical wrapper prevents global collisions with a consumer's
  // other diagrams; the raw Python fallback uses these same captured bytes.
  const vendor = Buffer.from("/* Dagre 3.1.1 / Graphlib 4.0.5: pinned MIT layout engine. */\n" + license +
    "\nvar AutoformDagre = (function () {\n" +
    engine + "\nreturn dagre.default || dagre;\n})();\n", "utf8");
  // Capture all inputs before compiling, exactly as Python concatenates them.
  const navigation = await readFile(resolve(root, "autoform_cli/assets/blueprint-navigation.js"));
  const viewer = await readFile(resolve(root, "autoform_cli/assets/blueprint-dag.js"));
  const bytes = Buffer.concat([vendor, Buffer.from("\n", "utf8"), navigation, Buffer.from("\n", "utf8"), viewer]);
  new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  const digest = createHash("sha256").update(bytes).digest("hex");
  const compiled = await transform(bytes.toString("utf8"), {
    loader: "js",
    sourcefile: "blueprint-explorer.js",
    format: "iife",
    target: "es2020",
    charset: "utf8",
    minifyIdentifiers: true,
    minifySyntax: true,
    minifyWhitespace: true,
    // Only explicitly private DOM refs use $ names; public payload/router fields stay intact.
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
  const output = Buffer.from(banner + license + "\n" + compressed.code + "\n", "utf8");
  if (output.byteLength >= 132_000) {
    console.error(`Explorer bundle is ${output.byteLength} bytes; the total integrated client limit is below 132,000 bytes. Source sha256 ${digest}`);
    process.exitCode = 1;
  } else if (args[0] === "--check") {
    let existing;
    try { existing = await readFile(destination); } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    let existingVendor;
    try { existingVendor = await readFile(vendorDestination); } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    if (!existing || !existing.equals(output) || !existingVendor || !existingVendor.equals(vendor)) {
      console.error("Explorer bundle is stale or missing. Run npm run build:explorer.");
      process.exitCode = 1;
    } else {
      console.log(`Explorer bundle matches: ${output.byteLength} bytes; source sha256 ${digest}`);
    }
  } else {
    await writeFile(vendorDestination, vendor);
    await writeFile(destination, output);
    console.log(`Built explorer: ${output.byteLength} bytes; source sha256 ${digest}`);
  }
}
