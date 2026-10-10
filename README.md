# grok-bot-chess-bot

The practice engine and coaching tools behind the **Chess Bot** Grok Bot. Chess
Bot plays [Agent Chess](https://splenectomy.github.io/agent-chess) against you
like a real **~1000-rated Lichess player**. It hangs a piece now and then, misses
two-move tactics, and still finds the obvious recapture. When the game is over,
it can annotate the game with engine-verified coaching notes.

> **Get Chess Bot on the Grok Bot marketplace:** (link coming soon)
> <!-- TODO: replace with the marketplace URL -->
> `https://TODO-grok-bot-marketplace.example/chess-bot`

A freshly imported Chess Bot template downloads this repo on its first run and
plays at the shipped, tested 1000 preset. You don't need to recalibrate.

## Quick start

```bash
git clone https://github.com/SplenectomY/grok-bot-chess-bot.git ~/chess-bot
cd ~/chess-bot && bash setup.sh    # idempotent; re-run any time
node chess-loop-teach.mjs ROOM 1000 # ROOM = Agent Chess room code
```

Requirements: Node.js 18+ and Python 3.9+ on Linux or macOS. `setup.sh`:

* checks node and python3, creates `.venv/`, and installs `python-chess`;
* finds Stockfish (`$STOCKFISH_BIN`, then `bin/stockfish`, then `stockfish` on
  PATH). If none is found, it downloads the official **Stockfish 17** release
  for your platform (Linux x86-64 avx2/sse41/plain, macOS Apple Silicon/Intel;
  Linux arm64 uses the sf_19 universal build) into `bin/`;
* fetches the Agent Chess CLI from
  `https://splenectomy.github.io/agent-chess/agent-chess.mjs`. The CLI is not
  vendored, so you always get the current one (`FORCE_CLI=1` refreshes it);
* runs a one-move worker smoke test and prints the launch command.

### Environment

| var | default | meaning |
|---|---|---|
| `CHESS_HOME` | repo dir | where `agent-chess.mjs`, `.venv`, `bin/`, logs, and flag files live |
| `STOCKFISH_BIN` | `$CHESS_HOME/bin/stockfish`, then PATH | engine binary |
| `CHESS_PYTHON` | `$CHESS_HOME/.venv/bin/python`, then `python3` | python for the worker |
| `CHESS_PRESETS` | `presets/elo-profiles.json` | Elo preset file |
| `CHESS_ELO_OVERRIDES` | `{}` | JSON knob overrides for one run (tuning) |
| `CHESS_LOOP_LOG` | `$CHESS_HOME/loop-ROOM.log` | fsync'd play log (or pass it as the 3rd argument) |
| `CHESS_MOVE_BUDGET_MS` | 20000 | per-move budget before fallback |
| `CHESS_WAIT_SEC` | 8 | `wait` timeout midgame |
| `AGENT_CHESS_CLI`, `AGENT_CHESS_RELAY` | | override the CLI path / relay base |

## Launch and exit codes

```
node chess-loop-teach.mjs ROOM [1000 | elo:N | skill:N] [logPath]
```

* `1000` (or any value from 400 to 2800, or `elo:N`): human-calibrated picker
  at that Lichess rating.
* `0`-`20` / `skill:N`: legacy Stockfish Skill Level pipeline (not
  human-calibrated, see below).

| exit | meaning |
|---|---|
| 0 | game over and the post-game listen (~90 s) finished, or SIGINT/SIGTERM |
| 10 | the opponent asked for analysis. `annotate-needed-ROOM` is written in `CHESS_HOME`, so annotate and post notes |
| 2 | another loop for this ROOM is already running (pidfile `/tmp/chess-loop-ROOM.pid`) |
| 1 | usage error or fatal error |

## How the human-calibrated picker works

`chess-sf-worker.py` is a long-lived JSON-lines worker (`choose`, `ponder`,
`cache_get/put`, `ping`, `quit`). For each move in elo mode:

1. **Judge view:** full-strength Stockfish MultiPV over *all* legal moves
   (depth 12 / 0.55 s).
2. **Shallow view:** a second engine at depth 2 with a fresh hash. It sees
   one-move hangs and recaptures and misses 2+-move tactics, which is where
   the human-like missed tactics come from.
3. **Perceived loss** per move is a blend of shallow and deep loss, where loss =
   max(Δwin%, κ·Δcp). Thematic "teach" moves (checks, captures, forks, pins)
   get a small bonus.
4. **Candidates:** top-K of each view, plus all captures and checks, minus junk
   shuffles. These are the moves a human actually looks at.
5. **Blind turns:** with probability `P_BLIND`, one tempting candidate's
   refutation is ignored and scored by material only. This is how the bot
   sometimes hangs a piece at the right rate.
6. **Softmax:** the move is sampled with P(m) ∝ exp(−perceived/T).
7. **Loosened guard:** only queen-for-nothing (re-drawn with p=0.85) and allowing
   mate-in-1 (p=0.6) are usually caught. Hanging minors or rooks happens
   naturally.
8. **Teach overlay:** a FEN-legal thematic candidate can replace the pick if
   it is about as good by full-strength eval and doesn't lose material by SEE.
   This gives the coach instructive moves without changing strength.
9. **Won-position rules** (judge ≥ +300 cp): never repeat a position if a
   non-repeating move holding ≥ 0 exists, and no endless checking after two
   checks in a row. A 1000 can still blunder, but the bot won't throw away a
   won game to a threefold.

Ponder runs on the opponent's time and caches replies. Every cache hit is
re-gated with a fresh full-strength check.

### Clock and robustness

* A 20 s per-move budget with fallback (cached move, then judge top move, then
  first legal move) plus a background worker respawn. Python-chess engine
  timeouts respawn and retry.
* Async CLI calls with kill timers. Midgame uses a plain `wait ROOM --timeout 8`.
* Restore detection: a wall-clock vs monotonic jump means the box was
  checkpointed and resumed, so the loop aborts in-flight calls and re-reads state.
* Every move logs `[timing]` and `[relay]` lines (ply and relay message id).
  Logs are fsync'd so they survive a restore.
* State is read first. If it's our move, the bot moves before waiting. Chat
  replies are sent only after the move, so they never burn our clock.
* A pidfile ensures one loop per room.

## Presets

`presets/elo-profiles.json` has anchors at 800, 1000, 1200, and 1400:

* **1000 is calibrated.** It's the shipped default: ACPL 79.6, accuracy 76%, ~9
  blunders per 100 moves over 1426 moves, vs Lichess-1000 targets of 75.3, 72%,
  and 9.33.
* **800, 1200, and 1400 are field-test presets.** They match their band's ACPL
  in self-play but haven't been validated in human games yet. 1200 and 1400
  blunder less than their bands, so they may play a bit stronger than labelled.

Targets between anchors interpolate. Below 800 clamps to 800, and above 1400
clamps to 1400. See
[presets/README.md](presets/README.md) and [CALIBRATION.md](CALIBRATION.md) for
how to calibrate a new level with `tools/calibration/`.

## Honest note on Stockfish skill levels

Stockfish's Skill Level and UCI_Elo are **not human-calibrated**. Scored
against real Lichess games, **Skill 6 measured about 2100-2300 Lichess** (ACPL
~33, ~90% accuracy), not ~1000. Its errors are small inaccuracies, and it almost
never hangs material. That is why the elo-mode picker exists. The legacy
`skill:N` mode stays available for comparison.

## Analysis verifier

`verify-annotations.py` checks coaching notes before they are posted:

```bash
.venv/bin/python verify-annotations.py --review review.json --notes notes.json [--depth 18] [--out verified.json]
```

It checks each note's "better" move and severity tag against local Stockfish
MultiPV, falls back to Lichess cloud-eval, uses the Syzygy tablebase at 7 pieces
or fewer, and runs an opening-book check (Lichess explorer, or a local ECO index
built from the [lichess-org/chess-openings](https://github.com/lichess-org/chess-openings)
`a.tsv`-`e.tsv` placed in `$CHESS_HOME/opening-data/` or `CHESS_OPENING_DATA`).
It exits 0 if every note passes and 2 if any note was rejected. Kept notes are
still written.

## Layout

```
chess-loop-teach.mjs      play loop (Agent Chess CLI driver)
chess-sf-worker.py        two-engine worker (elo mode + legacy skill mode)
verify-annotations.py     coaching-note verifier
presets/                  Elo presets (+ README)
tools/calibration/        metrics, Lichess calibration, self-play, scoring, tests
setup.sh                  installer
```

## Credits

* Author: **SplenSoft**. MIT License (see LICENSE).
* [Stockfish](https://stockfishchess.org/) (GPLv3) is downloaded by `setup.sh`
  from the official releases and is not vendored or redistributed here.
* [python-chess](https://github.com/niklasf/python-chess) (GPLv3).
* Calibration data: the [Lichess open database](https://database.lichess.org/)
  (CC0) and Lichess's accuracy and win% formulas.
* [Agent Chess](https://splenectomy.github.io/agent-chess).
