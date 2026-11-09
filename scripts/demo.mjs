#!/usr/bin/env node
// One command to reset to a clean data folder and start the whole stack for the demo:
//   node scripts/demo.mjs            wipe demo-data/, start OCR service + backend + frontend (live, no mocks)
//   node scripts/demo.mjs --keep     start without wiping the data
//   PYTHON=py node scripts/demo.mjs  choose the Python executable (default: python3, then python)
// Ctrl+C stops everything. Requires npm install in backend/ and frontend/, and the Python deps of ocr-service.
import { spawn, execFileSync, spawnSync } from "node:child_process";
import { existsSync, rmSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const data = join(root, "demo-data");
const isWin = process.platform === "win32";
const PASSWORD = process.env.DEMO_PASSWORD || "demo-pass-123";
const ports = { ocr: 8000, backend: 4000, frontend: 5173 };
const children = [];

function log(msg) { console.log(`[demo] ${msg}`); }
function die(msg) { console.error(`[demo] ERROR: ${msg}`); stop(); process.exit(1); }
function stop() { for (const c of children) { try { isWin ? spawnSync("taskkill", ["/pid", String(c.pid), "/T", "/F"]) : process.kill(-c.pid); } catch {} } }
process.on("SIGINT", () => { stop(); process.exit(0); });
process.on("SIGTERM", () => { stop(); process.exit(0); });

function python() {
  const candidates = process.env.PYTHON ? [process.env.PYTHON] : isWin ? ["python", "py"] : ["python3", "python"];
  for (const p of candidates) {
    const r = spawnSync(p, ["-c", "import fastapi, uvicorn"], { stdio: "ignore" });
    if (r.status === 0) return p;
  }
  die("no Python with the ocr-service dependencies found (pip install -r ocr-service/requirements.txt, or set PYTHON=...)");
}

function start(name, cmd, args, cwd, env) {
  const child = spawn(cmd, args, { cwd, env: { ...process.env, ...env }, stdio: ["ignore", "pipe", "pipe"], shell: isWin, detached: !isWin });
  children.push(child);
  const tag = (d) => String(d).split(/\r?\n/).filter(Boolean).forEach((l) => console.log(`  [${name}] ${l.slice(0, 160)}`));
  child.stdout.on("data", tag); child.stderr.on("data", tag);
  child.on("exit", (code) => { if (code) log(`${name} exited with code ${code}`); });
}

async function waitFor(url, name, seconds = 90) {
  const end = Date.now() + seconds * 1000;
  while (Date.now() < end) {
    try { const r = await fetch(url); if (r.status < 500) return; } catch {}
    await new Promise((r) => setTimeout(r, 1000));
  }
  die(`${name} did not come up at ${url} within ${seconds}s`);
}

if (!process.argv.includes("--keep")) {
  if (existsSync(data)) rmSync(data, { recursive: true, force: true });
  log("wiped demo-data/ (clean start)");
}
mkdirSync(data, { recursive: true });

let hash;
try {
  hash = execFileSync(isWin ? "npm.cmd" : "npm", ["run", "-s", "hash-password", "--", PASSWORD], { cwd: join(root, "backend"), encoding: "utf8", shell: isWin }).trim().split(/\r?\n/).pop();
} catch (e) { die(`could not hash the password (did you run npm install in backend/?): ${e.message}`); }

start("ocr", python(), ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(ports.ocr)], join(root, "ocr-service"), { DATA_DIR: data });
start("backend", isWin ? "npx.cmd" : "npx", ["tsx", "src/server.ts"], join(root, "backend"), {
  PORT: String(ports.backend), DATA_DIR: data, ANALYSIS_MODE: "http", ANALYSIS_URL: `http://127.0.0.1:${ports.ocr}`, ANALYST_PASSWORD_HASH: hash, NODE_ENV: "development",
});
start("frontend", isWin ? "npx.cmd" : "npx", ["vite", "--port", String(ports.frontend), "--host", "127.0.0.1"], join(root, "frontend"), { VITE_USE_MOCKS: "false" });

await waitFor(`http://127.0.0.1:${ports.ocr}/health`, "OCR service");
await waitFor(`http://127.0.0.1:${ports.backend}/api/v1/auth/session`, "backend");
await waitFor(`http://127.0.0.1:${ports.frontend}/`, "frontend");
const probe = await fetch(`http://127.0.0.1:${ports.frontend}/api/v1/auth/login`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ username: "analyst", password: PASSWORD }) });
if (!probe.ok) die(`frontend can reach the stack, but login answered ${probe.status}`);

console.log(`
[demo] READY (live backend, clean data)
  Open:     http://127.0.0.1:${ports.frontend}
  Login:    analyst / ${PASSWORD}
  Samples:  ${join(root, "samples", "dev")}
            S7a_statement_arithmetic_error  -> debits total mismatch
            S5a_tampered_with_inconsistency -> date-of-birth conflict
  Tip:      create a fresh case in the scanner for each scenario. Ctrl+C stops everything.
`);
