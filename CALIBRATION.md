# Calibration

The goal is a practice opponent that plays like a real **~1000 Lichess
(Rapid/Classical)** player: not just "weak", but weak in the human way (the
right rate of hung pieces and missed tactics, not random shuffles).

## 1. Human reference data

Players' moves were scored from the first ~19.9k analysed, rated,
normally-terminated games of the Lichess database for September 2025
(`lichess_db_standard_rated_2025-09.pgn.zst`, https://database.lichess.org/),
using the `[%eval]` comments already in the PGN. The code is the same one used for
the bot's games (`tools/calibration/metrics.py`):

* CPL is clamped at ±1000 cp (ACPL computed on clamped evals).
* Win% and per-move accuracy use the Lichess formulas (scalachess `eval.scala`,
  lila `AccuracyPercent.scala`). Game accuracy is the Lichess volatility-weighted
  mean + harmonic mean.
* Inaccuracy, mistake and blunder are drops of ≥0.1, ≥0.2 and ≥0.3 in winning
  chances (lila `Advice.scala`).

`tools/calibration/calib.py URL N OUT.jsonl` streams a month of the database
and writes per-player-game rows. `calib_report.py OUT.jsonl` prints the band table.
The result is `tools/calibration/calib_table.txt`. The raw jsonl (~6 MB) is
not included because it can be regenerated.

| Lichess band | mean ACPL | mean accuracy | inacc/100 | mistakes/100 | blunders/100 |
|---|---|---|---|---|---|
| 1000 | 75.3 | 72.0 | 10.15 | 5.13 | 9.33 |
| 1200 | 67.6 | 74.6 | 9.78 | 4.70 | 8.32 |
| 1400 | 60.2 | 76.9 | 9.58 | 4.68 | 7.18 |
| 1700 | 50.8 | 80.8 | 9.17 | 3.97 | 5.74 |
| 2000 | 43.9 | 83.0 | 8.32 | 3.46 | 4.82 |
| 2300 | 32.4 | 86.5 | 6.32 | 2.31 | 3.59 |

## 2. Why not Stockfish Skill Level / UCI_Elo?

The earlier version played Stockfish "Skill Level 6" moves on the assumption
that this was roughly 1000. Measured with the method above, it scored **ACPL ~33
and ~90% accuracy, which is about 2100-2300 Lichess**. That makes sense:
Stockfish's own calibration puts Skill 6 near 2360 CCRL Blitz, and the
UCI_Elo floor is 1320 *engine* Elo. Skill levels make engine-style errors
(small inaccuracies) and almost never hang material, while real 1000s blunder
about 9 times per 100 moves. The legacy pipeline is still available
(`skill:6`), but it is not human-calibrated.

## 3. Bot measurement

`tools/calibration/batch.sh TAG NGAMES OPP PONDER 'OVERRIDES' [SEEDBASE] [PAR] [ELO]`:

1. `selfplay.py` drives the real worker protocol (`choose`/`ponder`) for
   NGAMES games. OPP is `self` (both sides are the bot), `sf1320` (Stockfish
   UCI_Elo 1320), or `old6` (legacy Skill-6 pipeline).
2. `score.py` re-scores every game with Stockfish at depth 14 (`SCORE_DEPTH`).
3. `report.py` prints bot-side ACPL (bootstrap 90% CI), mean game accuracy,
   I/M/B per 100 moves, and move sources.

`test_threefold.py` is a regression test for the won-position rules (no
repetition when winning, no endless checks). Its expected result is `FAILS: 0`.

### 1000 tuning results (2026-10-09, Stockfish 17)

| tag | T | P_BLIND | TEMPT | moves | ACPL | acc % | I/M/B per 100 |
|---|---|---|---|---|---|---|---|
| t6 | 6.0 | 0 | - | 846 | 48.5 | 86.3 | 13.95/4.14/2.60 |
| t45b12 (min loss 8) | 4.5 | .12 | - | 982 | 55.6 | 82.9 | 13.34/5.50/3.56 |
| A35b35 | 3.5 | .35 | - | 969 | 52.3 | 86.1 | 11.97/4.85/2.48 |
| B30b45 | 3.0 | .45 | - | 767 | 51.4 | 86.3 | 11.34/4.43/2.74 |
| C35b40 (min loss 20) | 3.5 | .40 | 0 | 947 | 53.6 | 82.0 | 7.60/1.69/2.96 |
| D30b55 | 3.0 | .55 | 0 | 871 | 52.1 | 83.4 | 7.00/1.72/5.40 |
| E45b30t12 | 4.5 | .30 | 12 | 518 | 78.1 | 73.3 | 11.58/3.67/9.46 |
| F45b45t8 | 4.5 | .45 | 8 | 773 | 77.6 | 73.4 | 7.50/1.81/7.76 |
| V1self (ponder, won-rules v2) | 4.8 | .28 | 12 | 600 | 66.5 | 78.5 | 8.67/3.17/6.83 |
| **G52b36 (12 games, chosen)** | **5.2** | **.36** | **12** | **1426** | **79.6** (90% CI 73.5-85.8) | **76.0** | **7.99/3.16/9.05** |
| V2self (9 games, ponder) | 5.4 | .34 | 12 | 845 | 74.6 (66.9-82.7) | 77.8 | 12.43/1.89/6.15 |
| V2sf1320 (6 games vs SF UCI_Elo 1320) | 5.4 | .34 | 12 | 316 | 75.6 (66.7-85.2) | 83.5 | 11.08/3.80/3.80 |
| H48b36m12 (MIN_LOSS 12, TEMPT 10) | 4.8 | .36 | 10 | 1276 | 79.7 (74.0-85.7) | 81.2 | 10.82/5.33/6.43 |
| V3self (confirmation, ponder on) | 5.2 | .36 | 12 | 1202 | 67.6 (61.6-73.7) | 78.9 | 10.48/3.41/7.07 |

**Target (Lichess 1000):** ACPL 75.3, accuracy 72.0%, I/M/B 10.15/5.13/9.33.

G52b36 was chosen as the shipped 1000 preset (`presets/elo-profiles.json`)
because it matched best on ACPL, blunder rate, and accuracy together. Pondering
slightly lowers ACPL, because cached replies come from slightly longer judge
searches. Expect ACPL in the 68-80 range from game to game. Tests ran on a
heavily loaded shared machine, and the worker includes engine respawn and retry
for that case.

### 800 / 1200 / 1400 results (2026-10-10, self-play, no ponder, depth 14)

| tag | elo | T | P_BLIND | moves | ACPL (90% CI) | acc % | I/M/B per 100 |
|---|---|---|---|---|---|---|---|
| P44b28 | 1200 | 4.4 | .28 | 910 | 70.7 (63.5-78.4) | 78.4 | 7.58/2.31/5.71 |
| P40b26 | 1200 | 4.0 | .26 | 1211 | 59.0 (53.9-64.5) | 83.5 | 7.51/2.73/4.46 |
| **P46b24 (chosen)** | **1200** | **4.6** | **.24** | **1223** | **65.3 (59.2-72.0)** | **77.1** | **9.48/3.43/6.62** |
| **Q38b20 (chosen)** | **1400** | **3.8** | **.20** | **1094** | **60.4 (55.0-66.2)** | **83.7** | **8.14/2.74/4.02** |
| Q34b16 | 1400 | 3.4 | .16 | 1188 | 55.7 (51.0-60.4) | 82.3 | 9.51/1.09/4.12 |
| **E58b40 (chosen)** | **800** | **5.8** | **.40** | **1106** | **82.1 (74.8-89.7)** | **74.4** | **7.50/4.16/9.40** |
| E64b44 | 800 | 6.4 | .44 | 993 | 101.3 (92.4-110.5) | 65.3 | 10.88/3.73/15.21 |

Targets (Lichess Rapid/Classical): 800 = 82.8 / 69.9 / 10.37-5.54-9.46;
1200 = 67.6 / 74.6 / 9.78-4.70-8.32; 1400 = 60.2 / 76.9 / 9.58-4.68-7.18.

Other knobs for these anchors: TEMPT 12, MIN_LOSS 20, ALPHA 0.5, KAPPA 3, K 6,
TEACH_BONUS 4. TEACH_MAX_LOSS / GUARD_QUEEN_P / GUARD_MATE1_P are
6.0/.85/.60 (800), 5.5/.875/.65 (1200), and 5.0/.90/.70 (1400).

These anchors are shipped as **field-test**. Each one matches its band's ACPL,
but **accuracy reads high in self-play** (by 2.5-7 points), and for **1200 and
1400 the inaccuracy, mistake, and blunder rates are under target**. The bot's
errors cluster into fewer, larger losses than human errors do, so these
presets may play somewhat stronger than their label. Sub-1000 targets now
interpolate between the 800 and 1000 anchors instead of clamping to 1000.

### 1400 retune (2026-10-12, self-play, no ponder, scored at depth 18)

Live 1400 games (5 games, 225 bot moves, all vs one ~1350 human) blundered
6.67 per 100 moves (90% CI on ACPL 43.5-71.5, ACPL 57.0), about 1.7x the old self-play figure,
while ACPL was on target. Rescoring the old batch at depth 18 changes little
(Q38b20: 62.5 / 4.30 blunders), so scoring depth does not explain the gap.
Most self-play blunders come from chosen blind-spot moves (with MIN_LOSS 20
every chosen blind move is a blunder). Lowering P_BLIND alone barely moved the
self-play blunder rate; making blind slips smaller (MIN_LOSS 12, TEMPT 10) and
tightening the queen / mate-in-1 guard did.

| tag | knobs (others as before) | moves | ACPL (90% CI) | acc % | I/M/B per 100 |
|---|---|---|---|---|---|
| Q38b20 (old, rescored d18) | T3.8 Pb.20 | 1094 | 62.5 (57.0-68.2) | 83.2 | 8.41/3.29/4.30 |
| R40b14 | T4.0 Pb.14 | 1267 | 55.4 (50.1-60.8) | 82.3 | 8.37/2.13/4.89 |
| S38b10 | T3.8 Pb.10 | 1161 | 55.2 (50.2-60.2) | 84.4 | 11.71/2.76/3.96 |
| S42b12m12 + repeat | T4.2 Pb.12 m12 t10 | 2693 | 56.2 (53.3-59.2) | 84.3 | 10.40/4.83/4.27 |
| V44b12m12 | T4.4 Pb.12 m12 t10 | 1630 | 55.8 (51.9-59.8) | 83.5 | 10.74/4.60/5.03 |
| U44+W44 (pooled) | T4.4 Pb.10 m12 t10 GQ.97 GM.85 | 2890 | 58.5 (55.5-61.7) | 83.9 | 12.08/4.91/4.22 |
| **W46b10m12g (chosen)** | **T4.6 Pb.10 m12 t10 GQ.97 GM.85** | **1636** | **56.0 (52.4-59.6)** | **86.3** | **10.88/4.03/3.12** |

New 1400 knobs: T 4.6, P_BLIND 0.10, BLIND_TEMPT 10, BLIND_MIN_LOSS 12,
GUARD_QUEEN_P 0.97, GUARD_MATE1_P 0.85 (ALPHA, KAPPA, K, TEACH unchanged).
Still **field-test**. Self-play blunders are aimed below 4 on purpose, because
live games ran about 1.6x the self-play rate. Accuracy still reads high. Error counts are noisy
(about 50 blunders per batch), so validate with more human games. 1200 uses the
same MIN_LOSS 20 blind mechanism, but its one live game (5.1 blunders/100) shows
no excess yet.

## 4. Calibrating a new level (e.g. 1200)

1. Pick the target row from `calib_table.txt`.
2. Start from interpolated knobs. Raise strength mainly by lowering `T` and
   `P_BLIND`, and by raising `GUARD_*_P` slightly.
3. Run `tools/calibration/batch_elo.sh 1200 R1200a 12 self 0 '{"T":4.8,"P_BLIND":0.30}'`
   (or `batch.sh TAG N OPP PONDER OVERRIDES SEEDBASE PAR ELO`). Use at least 10 games
   (more than 1000 moves) per candidate, and confirm the winner with a second
   batch with ponder on and a different seed base.
4. Add the anchor to `presets/elo-profiles.json` with `"calibrated": true`
   and the measured and target metrics. Mirror it in the built-in
   `ELO_PROFILES` fallback in `chess-sf-worker.py`.

Note: the calibration is tied to the engine version (Stockfish 17) and to the
worker's judge depth and time settings. `setup.sh` downloads sf_17 by default for
this reason (`STOCKFISH_TAG` overrides).
