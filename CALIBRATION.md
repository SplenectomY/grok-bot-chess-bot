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

### Hanging-piece guard (2026-10-15)

Live games with the retuned 1400 preset showed about 9 practical blunders per 100 moves:
attacked pieces left where they were, rooks left hanging, walking into forks. Practical
blunders are metric blunders plus clamp-hidden material losses, counted by
`tools/calibration/practical.py`: a move counts if it loses at least 3 pawns of material within 5 plies
while the raw eval drops at least 100cp. The win-chance metric misses many of these
once a game is already lost.

After the picker draws a move, the guard classifies it:
* **obvious**: it loses at least `GUARD_HANG_DROP` cp against the top move, and SEE
  (`material_net`) is at least 2 pawns worse than the top move's (a piece en prise,
  or an attacked piece ignored);
* **subtle**: same drop, and the material swing along the deep PV (5 plies) is at least 3 pawns
  worse than along the top move's PV (forks, two-move tactics).

With probability `GUARD_HANG_OBV_P` / `GUARD_HANG_SUB_P` the move is discarded and
the picker re-draws from the remaining candidates, up to 6 times. Otherwise the blunder is kept.
The guard never blocks a move outright.

| anchor | OBV_P | SUB_P | DROP | status |
|---|---|---|---|---|
| 800 | 0 | 0 | 250 | off |
| 1000 | 0 | 0 | 250 | off (keeps the locked calibration) |
| 1200 | .55 | .30 | 200 | set, not re-calibrated |
| 1400 | .85 | .55 | 150 | calibrated (T raised 4.6 -> 5.7) |

1400 self-play, depth 18 (practical = practical blunders per 100):

| tag | knobs | moves | ACPL (90% CI) | acc | I/M/B per 100 | practical (90% CI) |
|---|---|---|---|---|---|---|
| W46b10m12g (before) | no hang guard | 1636 | 56.0 (52.4-59.6) | 86.3 | 10.88/4.03/3.12 | 5.20 (4.34-6.11) |
| H2 | .90/.60 drop 250 | 1636 | 50.9 (47.4-54.3) | 86.2 | 11.37/3.91/3.06 | 4.22 (3.36-5.01) |
| H6 | .80/.50 drop 150 T5.6 | 1515 | 58.2 (54.5-61.7) | 84.7 | 13.33/4.42/4.49 | 5.28 (4.36-6.20) |
| H7 | .90/.60 drop 150 T5.8 | 1347 | 53.9 (50.4-57.5) | 86.3 | 13.07/4.38/3.41 | 4.75 (3.79-5.72) |
| C1+C2 | .85/.55 drop 150 T5.7 | 1311 | 47.1 (43.6-50.6) | 84.3 | 13.20/5.26/3.59 | 5.42 (4.35-6.48) |
| D1+D2 | same + P_BLIND .14 | 1283 | 56.4 (52.4-60.5) | 83.7 | 12.86/5.69/5.46 | 7.25 (6.08-8.42) |
| **H6+H7+C1+C2 (shipped family)** | **T5.6-5.8, ~.85/.55** | **4173** | **53.3 (51.3-55.5)** | **85.1** | **13.20/4.67/3.86** | **5.15 (4.60-5.73)** |

Self-play live-equivalent: the old live pools ran 8.9 (old 1400) and 9.9 (retuned 1400)
practical blunders per 100 against about 5.2 in self-play. 1000 check, guard off: 6 games, 765 moves,
ACPL 74.1 (66.1-82.7), practical 10.46, against the calibrated G52b36 79.6 / 11.29. No
material change. In self-play, ACPL and blunder rate are tightly coupled: no setting gave
ACPL of 55 or more with fewer than 5 practical blunders per 100.

### Per-game blunder budget + material / mate-in-2 / win-chance guard (2026-10-18)

Live 1400 games still showed hangs the cp-drop guard could not see: EX7VDH 27.Nd4 (a
rook, raw drop only 94cp because the game was already lost at -849) and 31.Nxa5 (allowed a
mating net; depth 12 does not see the mate). The live bot pool ran 13/199 = 6.53 practical
blunders per 100 (KTS87H + EX7VDH era). Two picker changes, all knobs data in
`presets/elo-profiles.json` (built-in fallback mirrored in `chess-sf-worker.py`):

1. **Material rule** (`GUARD_MAT_ON`), independent of the eval clamp: after the existing
   hang tests, a candidate is a hang if its SEE net is >= 3 pawns worse than the top
   move's (**obvious**) or its 5-ply PV material swing is >= 3 pawns worse (**subtle**),
   and the raw (unclamped, mate = 3000) eval drop is >= 40cp in decided positions
   (|raw top| >= 300cp) or >= 100cp otherwise. A first version used 40cp everywhere and
   fired too often in balanced positions (about 6.5 redraws per 100 moves). The queen test also uses
   the raw drop when the rule is on. **`GUARD_MATE_N`** = 2: the guard also catches
   allowing mate-in-2 (not only mate-in-1) when the top move does not allow mate.
2. **Win-chance guard** (`GUARD_WC_P`): a non-material Lichess-scale blunder
   (deep win-chance drop >= 15 points against the top move) that no other test flagged.
   About 70% of the practical blunders left after change 1 were of this kind, so without it
   the budget had little to act on.
3. **Per-game blunder budget** (`BUDGET_ON`, `B1_*`, `B2_*`): a bot move that reaches the
   board flagged (the guard kept it) or losing >= 15 win% is recorded as a (position, SAN)
   pair. The pairs are persisted per `gameId` (the loop sends `room:gameNumber`) under
   `CHESS_BUDGET_DIR` or `<worker dir>/.budget/`, so the count survives worker/loop
   restarts. The count is recomputed from the game's move list on each request. After 1
   such blunder, guard probabilities are raised to at least `B1_*` and `P_BLIND` is multiplied
   by `B1_BLIND`. After 2 or more, `B2_*` applies. A cached/pondered move drawn at a lower budget is
   discarded.

| anchor | MATE_N | MAT_ON | WC_P | budget | B1 obv/sub/queen/mate/wc, blind x | B2 obv/sub/queen/mate/wc, blind x |
|---|---|---|---|---|---|---|
| 800 | 1 | off | 0 | off | - | - |
| 1000 | 1 | off | 0 | off | - (locked calibration untouched) | - |
| 1200 | 2 | on | 0 | on | .85/.55/.95/.90/.40, x.5 | .95/.80/.99/.97/.70, x0 |
| 1400 | 2 | on | 0 | on | .93/.65/.99/.95/.60, x.7 | .97/.80/1/1/.85, x.4 |

1400 `T` was raised 5.7 -> 7.5 to keep ACPL at the previous self-play level (more small
errors, fewer blunders). 1200 got the new knobs but was **not re-measured**.

Regression (guard_kind replayed on game positions, 1400 profile): EX7VDH 5.Nb5 obvious,
10.Bxb4 obvious, **27.Nd4 obvious (material rule: 5 pawns, raw drop 94cp; old guard: none)**,
**31.Nxa5 obvious (material rule: 3 pawns, raw drop 109cp; old guard: none)**. KTS87H 34...Re2+
(sound sacrifice, best) not flagged, 32...Re4+ not flagged, 34...Qxg3+ subtle, 34...Rxf4+ obvious.
Synthetic back-rank mate-in-1 -> mate (1000: mate), mate-in-2 -> mate (1000: subtle, old
behaviour). The 1000 profile is unchanged by the budget and win-chance guard. budget_count and
persistence across restart pass, and the end-to-end worker protocol passes.

1400 self-play, depth 18, seeds 900-1200 (BG0 = seeds 701-706); practical = per 100:

| set | knobs | games | moves | ACPL | acc | I/M/B per 100 | practical (90% CI) |
|---|---|---|---|---|---|---|---|
| OLD (BG0+BO1-4) | 2026-10-15 behaviour, T5.7 | 30 | 3696 | 52.0 | 85.4 | 11.99/4.82/3.95 | 5.14 (4.57-5.74) |
| BM | material rule + budget, no WC, T5.7 | 24 | 3111 | 49.5 | 84.9 | 11.64/4.76/4.21 | 5.21 (4.56-5.88) |
| BW | + WC guard, T5.7 | 24 | 3026 | 46.8 | 85.7 | 12.62/5.12/3.14 | 3.93 (3.37-4.56) |
| T7 | + WC, T7.0 | 24 | 2979 | 52.8 | 85.6 | 13.86/5.07/3.39 | 4.50 (3.89-5.14) |
| T8 | + WC, T8.0 | 24 | 3054 | 56.9 | 84.9 | 13.36/5.44/3.34 | 4.39 (3.80-5.01) |
| BX | T8.0 + base WC_P .35 | 24 | 2904 | 53.9 | 84.5 | 12.95/6.06/3.48 | 4.27 (3.65-4.89) |
| **BF (shipped)** | **T7.5, seeds 900-1600** | **48** | **5655** | **51.1** | **85.6** | **12.68/5.68/3.41** | **4.46 (4.03-4.92)** |

ACPL is the move-weighted mean of 6-game chunks and acc is the mean of chunk means. In BF the
budget was active on 69% of bot moves: most games see an early >= 15 win% slip by
one side, so B1/B2 are effectively the steady state after the opening. Guard events per
5655 moves: hang redraws 538, material-rule redraws 244, win-chance redraws 65, queen 26, mate 4;
kept flagged moves 138 + 29 (win-chance). Self-play blunder rates have run below live
(the old 1400 self-play 5.15 against 6.53 in the latest live pool), so, scaling by that ratio as a rough
estimate only, expect live near 5.7 practical blunders per 100. Validate with human games. ACPL still reads strong against the
Lichess 1400 band (51 vs 60.2), as before.

**Market template:** the template copy of the worker/presets was **not** restaged. It needs
a restage with this worker, `presets/elo-profiles.json` and `chess-loop-teach.mjs`
(gameId in the choose message).

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
