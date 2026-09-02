// Fails loudly if dist/ was built before the last data/batch_results.json
// regeneration, instead of silently serving stale numbers. `vite build` only
// snapshots public/ at build time -- nothing re-runs it when the data changes
// afterward, so `npm run preview` is the one place left that can otherwise
// serve an outdated dashboard with no visible sign anything is wrong.
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const dataPath = resolve(here, "..", "..", "data", "batch_results.json");
const distPath = resolve(here, "..", "dist", "batch_results.json");

if (!existsSync(distPath)) {
  // Nothing built yet -- `vite preview` will fail with its own clear error.
  process.exit(0);
}

if (!existsSync(dataPath)) {
  console.error(`check-dist-fresh: ${dataPath} not found. Run: python data/run_batch.py`);
  process.exit(1);
}

const dataRunId = JSON.parse(readFileSync(dataPath, "utf-8")).batch_run_id;
const distRunId = JSON.parse(readFileSync(distPath, "utf-8")).batch_run_id;

if (dataRunId !== distRunId) {
  console.error(
    "check-dist-fresh: dist/ was built from a different batch run than the " +
      "one currently on disk.\n" +
      `  dist/batch_results.json  -> ${distRunId}\n` +
      `  data/batch_results.json  -> ${dataRunId}\n` +
      "  Run: npm run build   (then re-run preview)"
  );
  process.exit(1);
}

console.log(`check-dist-fresh: dist/ matches the current run (${distRunId})`);
