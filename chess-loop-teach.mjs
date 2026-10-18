#!/usr/bin/env node
/**
 * Calibrated human-strength picker (default ~1000 Lichess) + FEN-legal teach overlay.
 * Usage: node chess-loop-teach.mjs ROOM [target=1000]
 *   target 400..2800  → Elo mode: calibrated human-like picker at that Lichess
 *                       rating (anchors in chess-sf-worker.py ELO_PROFILES;
 *                       between anchors knobs are interpolated).
 *   target 0..20      → LEGACY Stockfish Skill Level pipeline (pre-2026-10-09;
 *                       NOTE Skill 6 measured ~2100-2300 Lichess, not ~1000).
 *   also accepted: elo:1200 / skill:6
 *   env CHESS_ELO_OVERRIDES='{"T":5}' overrides individual Elo-mode knobs.
 *
 * Elo-mode summary (details in chess-sf-worker.py ELO MODE):
 * - judge MultiPV over all legal moves + shallow depth-2 view; softmax over
 *   perceived win%-loss (temperature T); "blind turns" overlook one refutation;
 *   loosened guard only re-draws queen-for-nothing / mate-in-1 most of the time
 * - won-position rules: no repeating positions / no endless checks when ≥+300cp
 *
 * Legacy notes (unchanged machinery):
 * - Persistent local Stockfish via chess-sf-worker.py
 * - Skill pick = real Skill-N bestmove; full-strength (Skill 20) gate, our POV:
 *   teach must be within ~50cp of primary and not lose material (SEE);
 *   guard replaces piece-hanging skill picks with prob GUARD_REPLACE_P
 * - FEN+Skill cache: stores gate verdict; every hit is re-gated (never bypasses)
 * - Logs every teach/guard override/rejection as "[gate] ..." lines
 * - Ponder on opponent's time (MultiPV → same pipeline → cache)
 * - Threefold avoid when ahead among gated MultiPV alts
 * - Single process per ROOM (pidfile /tmp/chess-loop-ROOM.pid)
 * - Midgame: plain `wait ROOM --timeout 8` (never --any midgame)
 * - Hardened 2026-10-10: restore-safe fsync log (CHESS_LOOP_LOG / argv[4], default
 *   $CHESS_HOME/loop-ROOM.log), async CLI w/ kill timers, 20s per-move budget
 *   (fallback cached → judge top → first legal; worker respawn), clock-jump
 *   detection ("resumed after restore"), [timing] + [relay] lines per move
 * - state first; if yourMove → move before wait; newChat only after move
 * - Post-game: plain wait ~90s; annotate-needed-ROOM + exit 10 on analysis
 */
import { spawn } from "node:child_process";
import {
  writeFileSync, readFileSync, unlinkSync, existsSync, openSync, writeSync, fsyncSync, fstatSync, statSync,
} from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import os from "node:os";
import readline from "node:readline";
import { format } from "node:util";

const room = process.argv[2];
if (!room) {
  console.error("Usage: node chess-loop-teach.mjs ROOM [targetElo=1000 | skill 0-20 | elo:N | skill:N] [logPath]");
  process.exit(1);
}
/** Strength selector → {elo} (default) or {skill} (legacy). */
function parseStrength(arg) {
  const raw = String(arg ?? "").trim().toLowerCase();
  if (!raw) return { elo: 1000 };
  let m = raw.match(/^skill[:=]?(\d+)$/);
  if (m) return { skill: Math.min(20, Math.max(0, Number(m[1]))) };
  m = raw.match(/^elo[:=]?(\d+)$/);
  const n = Number(m ? m[1] : raw);
  if (!Number.isFinite(n)) {
    console.error(`Bad strength '${arg}' — use e.g. 1000, elo:1200, or skill:6`);
    process.exit(1);
  }
  if (!m && n <= 20) return { skill: Math.max(0, Math.round(n)) };
  return { elo: Math.min(2800, Math.max(400, Math.round(n))) };
}
const strength = parseStrength(process.argv[3]);
const skill = strength.skill; // undefined in Elo mode
const strengthLabel = strength.elo ? `elo=${strength.elo}` : `LEGACY skill=${skill}`;
if (!strength.elo) {
  console.error(
    `WARNING: legacy Skill Level ${skill} mode. Skill 6 measured ~2100-2300 Lichess; ` +
      "for a ~1000 practice opponent run with target 1000."
  );
}

// CHESS_HOME (default: this script's directory) holds agent-chess.mjs, logs, flags.
const root = process.env.CHESS_HOME || path.dirname(fileURLToPath(import.meta.url));
// Python for the worker: CHESS_PYTHON > $CHESS_HOME/.venv (setup.sh) > python3 on PATH.
const venvPy = path.join(root, ".venv", "bin", "python");
const pythonBin = process.env.CHESS_PYTHON || (existsSync(venvPy) ? venvPy : "python3");
const cli = process.env.AGENT_CHESS_CLI || path.join(root, "agent-chess.mjs");
const workerPy = process.env.CHESS_SF_WORKER || path.join(path.dirname(fileURLToPath(import.meta.url)), "chess-sf-worker.py"); // override for tests
const annotateFlag = path.join(root, `annotate-needed-${room}`);
const pidfile = `/tmp/chess-loop-${room}.pid`;

/* ---------- restore-safe logging (append + fsync every line) ---------- */
const logPath = process.env.CHESS_LOOP_LOG || process.argv[4] || path.join(root, `loop-${room}.log`);
let logFd = null;
let stdoutIsLog = false;
let stderrIsLog = false;
try {
  logFd = openSync(logPath, "a");
  const L = fstatSync(logFd);
  const same = (fd) => {
    try {
      const s = fstatSync(fd);
      return s.ino === L.ino && s.dev === L.dev;
    } catch {
      return false;
    }
  };
  stdoutIsLog = same(1);
  stderrIsLog = same(2);
} catch (e) {
  process.stderr.write(`log open failed ${logPath}: ${e.message}\n`);
}
const origOut = (s) => process.stdout.write(s);
const origErr = (s) => process.stderr.write(s);
function emit(line, isErr) {
  const s = line.endsWith("\n") ? line : line + "\n";
  if (logFd !== null) {
    try {
      writeSync(logFd, s);
      fsyncSync(logFd);
    } catch {
      /* fall through to stdio */
    }
  }
  // Mirror to stdio unless it is the same file (avoid duplicates when launched with > loop-ROOM.log)
  if (isErr ? !stderrIsLog || logFd === null : !stdoutIsLog || logFd === null) {
    try {
      (isErr ? origErr : origOut)(s);
    } catch {
      /* ignore */
    }
  }
}
console.log = (...a) => emit(format(...a), false);
console.error = (...a) => emit(format(...a), true);
const ts = () => new Date().toISOString();

/* ---------- clock-jump detection (box checkpoint/restore) ---------- */
let lastWall = Date.now();
let lastMono = performance.now();
let resumedFlag = false;
const JUMP_MS = 5000;
setInterval(() => {
  const w = Date.now();
  const m = performance.now();
  const drift = (w - lastWall) - (m - lastMono);
  const gap = w - lastWall;
  if (Math.abs(drift) > JUMP_MS || gap > 10000) {
    console.log(
      `[${ts()}] resumed after restore (wall +${gap}ms, mono +${Math.round(m - lastMono)}ms, drift ${Math.round(drift)}ms) — re-reading state`
    );
    resumedFlag = true;
    abortActiveCli("clock-jump");
  }
  lastWall = w;
  lastMono = m;
}, 1000).unref();

function claimPidfile() {
  if (existsSync(pidfile)) {
    let oldPid = 0;
    try {
      oldPid = Number(readFileSync(pidfile, "utf8").trim());
    } catch {
      /* ignore */
    }
    if (oldPid && oldPid !== process.pid) {
      try {
        process.kill(oldPid, 0);
        console.error(
          `Refuse to start: another chess-loop-teach.mjs for ${room} is running (pid ${oldPid}, ${pidfile})`
        );
        process.exit(2);
      } catch {
        // stale pidfile
      }
    }
  }
  writeFileSync(pidfile, String(process.pid) + "\n");
}

function releasePidfile() {
  try {
    if (existsSync(pidfile)) {
      const cur = Number(readFileSync(pidfile, "utf8").trim());
      if (cur === process.pid) unlinkSync(pidfile);
    }
  } catch {
    /* ignore */
  }
}

claimPidfile();

/* ---------- async CLI (spawn + kill timer) ---------- */
const activeCli = new Set();
function abortActiveCli(why) {
  for (const c of activeCli) {
    try {
      c.__aborted = why;
      c.kill("SIGKILL");
    } catch {
      /* ignore */
    }
  }
}
function runCli(args, timeoutMs = 30000) {
  return new Promise((resolve) => {
    let out = "";
    let done = false;
    let child;
    try {
      child = spawn("node", [cli, ...args], { stdio: ["ignore", "pipe", "pipe"] });
    } catch (e) {
      resolve({ code: 1, out: "", error: e });
      return;
    }
    activeCli.add(child);
    const finish = (code, error) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      activeCli.delete(child);
      resolve({ code, out, error, aborted: child.__aborted || null });
    };
    const timer = setTimeout(() => {
      child.__aborted = "timeout";
      try {
        child.kill("SIGKILL");
      } catch {
        /* ignore */
      }
      console.error(`[${ts()}] cli timeout ${args[0]} after ${timeoutMs}ms — killed`);
      finish(1, new Error("timeout"));
    }, timeoutMs);
    child.stdout.on("data", (d) => {
      if (out.length < 10_000_000) out += d;
    });
    child.stderr.on("data", (d) => {
      if (out.length < 10_000_000) out += d;
    });
    child.on("error", (e) => finish(1, e));
    child.on("close", (code, sig) => finish(code === null ? (sig ? 1 : 0) : code));
  });
}

async function state() {
  const { out } = await runCli(["state", room, "--json"], 20000);
  const i = out.indexOf("{");
  if (i < 0) return null;
  try {
    return JSON.parse(out.slice(i));
  } catch {
    return null;
  }
}

/* ---------- relay lookup (for [relay] ply/id lines) ---------- */
let relayBase = "https://ntfy.sh";
try {
  const saved = JSON.parse(readFileSync(path.join(os.homedir(), ".agent-chess.json"), "utf8"));
  if (saved?.[room]?.relay) relayBase = String(saved[room].relay).replace(/\/+$/, "");
} catch {
  /* default */
}
if (process.env.AGENT_CHESS_RELAY) relayBase = process.env.AGENT_CHESS_RELAY.replace(/\/+$/, "");
async function relayRecent(sinceSec = 120) {
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 5000);
    const r = await fetch(`${relayBase}/agentchess-v1-${room}/json?poll=1&since=${sinceSec}s`, { signal: ctl.signal });
    clearTimeout(t);
    const txt = await r.text();
    const msgs = [];
    for (const l of txt.split("\n")) {
      if (!l.trim()) continue;
      try {
        const e = JSON.parse(l);
        if (e.event !== "message") continue;
        let m = {};
        try {
          m = JSON.parse(e.message);
        } catch {
          /* ignore */
        }
        msgs.push({ ...m, player: m.id, relayId: e.id, time: e.time });
      } catch {
        /* ignore */
      }
    }
    return msgs;
  } catch {
    return null;
  }
}

/** Long-lived python-chess + Stockfish worker */
class SfWorker {
  constructor() {
    this.proc = spawn(pythonBin, ["-u", workerPy], {
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
    });
    this.rl = readline.createInterface({ input: this.proc.stdout });
    this.queue = [];
    this.ready = null;
    this.dead = false;
    this.proc.stderr.on("data", (d) => {
      const t = d.toString().trim();
      if (t) console.error(`[${ts()}] [sf-worker]`, t);
    });
    this.proc.on("error", (e) => console.error("[sf-worker] spawn error", e.message));
    this.proc.on("exit", (code) => {
      this.dead = true;
      while (this.queue.length) {
        const { reject } = this.queue.shift();
        reject(new Error(`sf-worker exited ${code}`));
      }
    });
    this.ready = this._readOne();
  }

  _readOne() {
    return new Promise((resolve, reject) => {
      const onLine = (line) => {
        try {
          resolve(JSON.parse(line));
        } catch (e) {
          reject(e);
        }
      };
      this.rl.once("line", onLine);
      this.proc.once("error", reject);
    });
  }

  async init() {
    const hello = await this.ready;
    if (!hello?.ok) throw new Error("sf-worker failed to start: " + JSON.stringify(hello));
    console.log(
      "SF worker ready", hello.sf || "", "gateCp=", hello.gateCp ?? "?",
      "guardP=", hello.guardP ?? "?", "guardDropCp=", hello.guardDropCp ?? "?",
      "eloAnchors=", JSON.stringify(hello.eloAnchors ?? []),
      "eloCalibrated=", JSON.stringify(hello.eloCalibrated ?? []),
      "eloOverrides=", JSON.stringify(hello.eloOverrides ?? {})
    );
    this.rl.on("line", (line) => {
      const waiter = this.queue.shift();
      if (!waiter) return;
      try {
        waiter.resolve(JSON.parse(line));
      } catch (e) {
        waiter.reject(e);
      }
    });
  }

  request(msg, timeoutMs = 25000) {
    if (this.dead) return Promise.reject(new Error("sf-worker dead"));
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => {
        const idx = this.queue.findIndex((w) => w.resolve === resolve);
        if (idx >= 0) this.queue.splice(idx, 1);
        reject(new Error("sf-worker timeout " + msg.cmd));
      }, timeoutMs);
      this.queue.push({
        resolve: (v) => {
          clearTimeout(t);
          resolve(v);
        },
        reject: (e) => {
          clearTimeout(t);
          reject(e);
        },
      });
      this.proc.stdin.write(JSON.stringify(msg) + "\n");
    });
  }

  async choose(st) {
    const weAreWhite = st.you === "white" || st.color === "white";
    const moves = extractMoves(st);
    const r = await this.request({
      cmd: "choose",
      fen: st.fen,
      ...strength,
      legal: st.legalMoves || [],
      weAreWhite,
      moves,
      startFen: st.startFen || undefined,
      gameId: `${room}:${st.game ?? st.gameNumber ?? 1}`, // per-game blunder budget (worker persists it)
    });
    if (!r?.ok) throw new Error(r?.error || "choose failed");
    return r;
  }

  async ponder(fen) {
    try {
      const r = await this.request({ cmd: "ponder", fen, ...strength, maxLines: 4 }, 20000);
      return r;
    } catch (e) {
      console.error("ponder error", e.message);
      return null;
    }
  }

  kill() {
    this.dead = true;
    try {
      this.proc.kill("SIGKILL");
    } catch {
      /* ignore */
    }
  }

  async quit() {
    try {
      await this.request({ cmd: "quit" }, 3000);
    } catch {
      /* ignore */
    }
    try {
      this.proc.kill();
    } catch {
      /* ignore */
    }
  }
}

function extractMoves(st) {
  if (Array.isArray(st.moves) && st.moves.length) return st.moves;
  if (typeof st.pgn === "string" && st.pgn.trim()) {
    return st.pgn
      .replace(/\d+\.(\.\.)?/g, " ")
      .replace(/1-0|0-1|1\/2-1\/2|\*/g, " ")
      .trim()
      .split(/\s+/)
      .filter(Boolean);
  }
  if (typeof st.moveList === "string") {
    return st.moveList.trim().split(/\s+/).filter(Boolean);
  }
  return [];
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}


const answeredChat = new Set();

async function handleNewChat(st) {
  // Only after a move (or when not burning our clock) — caller gates timing
  const chatLines = [...(st.newChat || []), ...(st.chat || [])];
  for (const line of chatLines) {
    if (!line.startsWith("Chess Bot:") && !answeredChat.has(line)) {
      answeredChat.add(line);
      const text = String(line).replace(/^[^:]+:\s*/, "");
      await runCli(["chat", room, `Ha — noted. Board first, banter after.`], 15000);
      console.log("Replied to chat:", text);
    }
  }
}

/* ---------- worker management: budget + background respawn ---------- */
const MOVE_BUDGET_MS = Number(process.env.CHESS_MOVE_BUDGET_MS || 20000);
const WAIT_SEC = Number(process.env.CHESS_WAIT_SEC || 8);
let worker = null;
let respawning = null;
function respawnWorker(why) {
  if (respawning) return respawning;
  console.error(`[${ts()}] [worker] respawning in background (${why})`);
  try {
    worker?.kill();
  } catch {
    /* ignore */
  }
  worker = null;
  respawning = (async () => {
    for (let i = 1; i <= 3; i++) {
      const w = new SfWorker();
      try {
        await Promise.race([
          w.init(),
          new Promise((_, rej) => setTimeout(() => rej(new Error("init timeout")), 20000)),
        ]);
        worker = w;
        console.error(`[${ts()}] [worker] respawn ok (attempt ${i})`);
        return w;
      } catch (e) {
        console.error(`[${ts()}] [worker] respawn attempt ${i} failed: ${e.message}`);
        w.kill();
      }
    }
    return null;
  })().finally(() => {
    respawning = null;
  });
  return respawning;
}

// Loop-local copy of ponder results: "<fen before opp move>|<opp SAN>" -> {ourSan, top}
const ponderMap = new Map();
let lastPonderFen = null;
function recordPonder(fen, pr) {
  for (const ln of pr?.lines || []) {
    let top = null;
    for (const ev of ln.events || []) {
      const m = /\bvs ([^\s)]+)\)/.exec(ev);
      if (m) top = m[1];
    }
    ponderMap.set(`${fen}|${ln.opp}`, { ourSan: ln.ourSan, top });
  }
  if (ponderMap.size > 400) ponderMap.delete(ponderMap.keys().next().value);
}

function fallbackMove(st) {
  const legal = st.legalMoves || [];
  const moves = extractMoves(st);
  const opp = moves[moves.length - 1];
  const hit = lastPonderFen && opp ? ponderMap.get(`${lastPonderFen}|${opp}`) : null;
  if (hit?.ourSan && legal.includes(hit.ourSan)) return { san: hit.ourSan, how: "cached" };
  if (hit?.top && legal.includes(hit.top)) return { san: hit.top, how: "judge-top" };
  if (legal.length) return { san: legal[0], how: "first-legal" };
  return { san: null, how: "none" };
}

async function chooseWithBudget(st, budgetMs) {
  const w = worker;
  if (!w || w.dead) {
    if (!respawning) respawnWorker("worker missing/dead");
    return { fb: true, why: "worker unavailable" };
  }
  let timer;
  const budget = new Promise((res) => {
    timer = setTimeout(() => res({ fb: true, why: `budget ${budgetMs}ms exceeded` }), Math.max(1000, budgetMs));
  });
  const work = w.choose(st).then(
    (r) => ({ r }),
    (e) => ({ fb: true, why: `choose failed: ${e.message}` })
  );
  const out = await Promise.race([work, budget]);
  clearTimeout(timer);
  if (out.fb) respawnWorker(out.why); // stalled/broken: don't trust its reply queue any more
  return out;
}

async function main() {
  worker = new SfWorker();
  await worker.init();
  if (strength.elo && strength.elo !== 1000) {
    console.error(`NOTE: target ${strength.elo} uses interpolated/provisional knobs (only 1000 is calibrated so far).`);
  }
  console.log(
    `[${ts()}] Teach+SF loop ${room} ${strengthLabel} ` +
      (strength.elo
        ? "(calibrated human picker+teach bonus+loose guard+won-position rules+ponder+cache)"
        : "(legacy full-strength-gate+guard+ponder+cache+threefold)") +
      ` log=${logPath} budget=${MOVE_BUDGET_MS}ms wait=${WAIT_SEC}s pid=${process.pid}`
  );

  let shuttingDown = false;
  const shutdown = async (code = 0) => {
    if (shuttingDown) return;
    shuttingDown = true;
    abortActiveCli("shutdown");
    if (worker) await worker.quit();
    releasePidfile();
    console.log(`[${ts()}] exit ${code}`);
    process.exit(code);
  };
  process.on("SIGINT", () => shutdown(0));
  process.on("SIGTERM", () => shutdown(0));

  while (true) {
    if (resumedFlag) {
      resumedFlag = false;
      console.log(`[${ts()}] post-resume: state re-read`);
    }
    // Always state first
    const tState = performance.now();
    const st = await state();
    const stateMs = Math.round(performance.now() - tState);
    if (!st?.ok) {
      console.error(`[${ts()}] state failed (${stateMs}ms) — retry`);
      await sleep(1500);
      continue;
    }

    if (st.result || st.gameOver) {
      console.log("Game over:", JSON.stringify(st.result || { gameOver: true }));
      const reason = st.result?.reason || "";
      if (reason) console.log("Result reason:", reason);

      // Agent Chess 0.18+: plain wait already listens like --any post-game
      const deadline = Date.now() + 90_000;
      while (Date.now() < deadline) {
        await runCli(["wait", room, "--timeout", "20"], 35000);
        const st2 = await state();
        if (!st2) break;

        if (
          st2.analysisRequestedBy?.length ||
          (st2.pending || []).includes("analysis")
        ) {
          try {
            writeFileSync(
              annotateFlag,
              JSON.stringify({
                room,
                at: new Date().toISOString(),
                analysisRequestedBy: st2.analysisRequestedBy || [],
                pending: st2.pending || [],
              }) + "\n"
            );
            console.log("Analysis requested — wrote", annotateFlag, "— exit 10");
          } catch (e) {
            console.error("flag write failed", e.message);
          }
          await shutdown(10);
        }

        if (st2.rematchRequested) {
          const rr = st2.rematchRequested;
          const they = st2.you === "white" ? rr.black : rr.white;
          const me = st2.you === "white" ? rr.white : rr.black;
          if (they && !me) {
            console.log("Accepting rematch...");
            await runCli(["rematch", room], 30000);
            break;
          }
        }

        for (const line of st2.chat || []) {
          if (!line.startsWith("Chess Bot:") && !answeredChat.has(line)) {
            answeredChat.add(line);
            console.log("Post-game chat:", line);
          }
        }
      }
      if (st.result || (await state())?.gameOver) {
        await shutdown(0);
      }
      continue;
    }

    // If yourMove → move before wait (and before chat)
    if (st.yourMove) {
      const t0 = performance.now();
      const ply = st.ply ?? extractMoves(st).length;
      let move;
      let meta = {};
      let fbHow = null;
      const out = await chooseWithBudget(st, MOVE_BUDGET_MS - stateMs);
      if (out.r) {
        move = out.r.san;
        const r = out.r;
        meta = {
          cached: !!r.cached,
          score: r.score,
          deviated: r.deviated,
          skillBestSan: r.skillBestSan,
          fullBestSan: r.fullBestSan,
          source: r.source,
          events: Array.isArray(r.events) ? r.events : [],
        };
        if (move && st.legalMoves?.length && !st.legalMoves.includes(move)) {
          console.error(`[${ts()}] worker move ${move} not in legal list — fallback`);
          move = null;
        }
      }
      if (!move) {
        const fb = fallbackMove(st);
        move = fb.san;
        fbHow = fb.how;
        console.error(`[${ts()}] [fallback] ${out.why || "invalid move"} → ${fb.how} ${move}`);
      }
      const chooseMs = Math.round(performance.now() - t0);
      if (!move) {
        console.error("No move available");
        await sleep(1000);
        continue;
      }
      console.log(
        `[${ts()}] move ${move}` +
          (fbHow ? ` (fallback:${fbHow})` : meta.cached ? " (cache hit)" : " (computed)") +
          (meta.deviated ? " [threefold-avoid]" : "") +
          (meta.skillBestSan && meta.skillBestSan !== move
            ? ` (skillBest=${meta.skillBestSan})`
            : "") +
          (meta.source && meta.source !== "skill" ? ` [${meta.source}]` : "") +
          (strength.elo && meta.fullBestSan && meta.fullBestSan !== move
            ? ` (best=${meta.fullBestSan})`
            : "")
      );
      // Gate reasons: teach/guard overrides, rejections, cache re-gate verdicts
      for (const ev of meta.events || []) console.log(`    [gate] ${ev}`);
      const pre = await relayRecent(300);
      const preLast = pre?.length ? pre[pre.length - 1] : null;
      console.log(
        `[${ts()}] [relay] before move ${move}: ply=${ply} lastRelayId=${preLast?.relayId ?? "?"}` +
          (preLast?.type ? ` (${preLast.type}${preLast.san ? " " + preLast.san : ""})` : "")
      );
      const tMove = performance.now();
      const mr = await runCli(["move", room, move], 30000);
      const moveMs = Math.round(performance.now() - tMove);
      const firstLine = (mr.out || "").split("\n").find((l) => /Played|not accepted|not legal|isn't your move|over/i.test(l)) || "";
      const post = await relayRecent(300);
      const mine = (post || []).filter((m) => m.type === "move" && m.ply === ply && m.san === move).pop();
      console.log(
        `[${ts()}] [relay] after move ${move}: code=${mr.code}${mr.aborted ? " aborted=" + mr.aborted : ""} ` +
          `ply=${ply} relayId=${mine?.relayId ?? "NOT-FOUND"} ${firstLine.trim()}`
      );
      // newChat only after move
      const tRe = performance.now();
      const stAfter = await state();
      const recheckMs = Math.round(performance.now() - tRe);
      console.log(
        `[${ts()}] [timing] ply=${ply} move=${move} state=${stateMs}ms choose=${chooseMs}ms move=${moveMs}ms recheck=${recheckMs}ms total=${Math.round(performance.now() - t0 + stateMs)}ms` +
          (fbHow ? ` fallback=${fbHow}` : "")
      );
      if (stAfter?.ok) await handleNewChat(stAfter);
      continue;
    }

    // Not our move: ponder, then plain wait (never --any midgame)
    if (worker && !worker.dead) {
      try {
        const w = worker;
        const pr = await w.ponder(st.fen);
        if (pr?.ok) {
          lastPonderFen = st.fen;
          recordPonder(st.fen, pr);
          console.log(
            `[ponder] cached ${pr.pondered || 0} replies (cache=${pr.cacheSize || "?"})`
          );
          for (const ln of pr.lines || []) {
            for (const ev of ln.events || [])
              console.log(`    [ponder-gate] if ${ln.opp} → ${ln.ourSan}: ${ev}`);
          }
        } else if (pr === null && w === worker) {
          respawnWorker("ponder failed/timeout"); // reply queue may be misaligned
        }
      } catch (e) {
        console.error("ponder error", e?.message || e);
      }
    }
    // Re-check before waiting — opponent may have moved during ponder
    const st3 = await state();
    if (st3?.yourMove) continue;
    if (st3?.gameOver || st3?.result) continue;
    if (resumedFlag) continue;
    console.log(`[${ts()}] wait ${WAIT_SEC}s ...`);
    // Midgame: plain wait only — returns on your move / game end; short cap so a missed wake costs ≤ WAIT_SEC
    await runCli(["wait", room, "--timeout", String(WAIT_SEC)], (WAIT_SEC + 10) * 1000);
  }
}

main().catch(async (e) => {
  console.error(e);
  releasePidfile();
  process.exit(1);
});
