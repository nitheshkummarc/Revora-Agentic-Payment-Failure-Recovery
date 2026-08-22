// Copies the committed batch results into public/ so the dashboard can be
// opened with the backend down. Kept as a copy step rather than a second
// checked-in file: one source of truth for the run, and no chance of the two
// drifting apart.
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = resolve(here, "..", "..", "data", "batch_results.json");
const target = resolve(here, "..", "public", "batch_results.json");

if (!existsSync(source)) {
  console.error(`sync-data: ${source} not found. Run: python data/run_batch.py`);
  process.exit(1);
}

mkdirSync(dirname(target), { recursive: true });
copyFileSync(source, target);
console.log(`sync-data: copied batch results to ${target}`);
