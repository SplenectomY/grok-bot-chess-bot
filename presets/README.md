# Elo presets

`elo-profiles.json` holds the knobs for the human-calibrated picker in
`chess-sf-worker.py` (elo mode). The worker loads this file at start-up
(override the path with `CHESS_PRESETS=/path/file.json`); if it is missing or
malformed the worker falls back to its built-in copy of the same table and
reports `"presets": "built-in"` in its ready line.

| Anchor (Lichess) | Status | Notes |
|---|---|---|
| 1000 | **calibrated** (2026-10-09) | ACPL 79.6 / acc 76.0% / 9.05 blunders per 100 over 1426 moves vs Lichess-1000 target 75.3 / 72.0% / 9.33 |
| 1200 | **calibration in progress** | Not in the file yet; final values will land in a follow-up commit. Until then 1200 is interpolated between 1000 and 1400. |
| 1400 | uncalibrated (provisional) | Extrapolated from the 1000 runs; validate before trusting |

Targets between anchors interpolate every knob linearly (`K` is rounded);
targets outside the range clamp to the nearest anchor. Any knob can be
overridden for one run with `CHESS_ELO_OVERRIDES='{"T":5.0}'`.

Schema per anchor: `calibrated` (bool), `knobs` (required: T, ALPHA, KAPPA, K,
P_BLIND, BLIND_TEMPT, BLIND_MIN_LOSS, TEACH_BONUS, TEACH_MAX_LOSS,
GUARD_QUEEN_P, GUARD_MATE1_P), and informational `measured` / `target` /
`method` / `note`. See ../CALIBRATION.md for how to calibrate a new anchor.
