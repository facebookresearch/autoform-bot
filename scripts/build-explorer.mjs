// Development-only compiler. Python publications ship the compiled classic script, not Node.
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { transform, version } from "esbuild";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const destination = resolve(root, "autoform_cli/assets/blueprint-explorer.min.js");
const args = process.argv.slice(2);
if (args.length > 1 || (args.length && args[0] !== "--check")) {
  console.error("Usage: node scripts/build-explorer.mjs [--check]");
  process.exitCode = 2;
} else {
  if (version !== "0.28.2") throw new Error("The explorer compiler must be esbuild 0.28.2");
  // Capture both inputs before compiling. This is exactly the Python fallback's UTF-8 concatenation.
  const navigation = await readFile(resolve(root, "autoform_cli/assets/blueprint-navigation.js"));
  const viewer = await readFile(resolve(root, "autoform_cli/assets/blueprint-dag.js"));
  const bytes = Buffer.concat([navigation, Buffer.from("\n", "utf8"), viewer]);
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
  const banner = `/* autoform-explorer-source-sha256:${digest} */\n`;
  const output = Buffer.from(banner + compiled.code, "utf8");
  if (output.byteLength >= 66_000) {
    console.error(`Explorer bundle is ${output.byteLength} bytes; the unchanged limit is below 66,000 bytes. Source sha256 ${digest}`);
    process.exitCode = 1;
  } else if (args[0] === "--check") {
    let existing;
    try { existing = await readFile(destination); } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    if (!existing || !existing.equals(output)) {
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
