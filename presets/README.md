# Elo presets

`elo-profiles.json` holds the knobs for the human-calibrated picker in
`chess-sf-worker.py` (elo mode). The worker loads this file at start-up
(override the path with `CHESS_PRESETS=/path/file.json`); if it is missing or
malformed the worker falls back to its built-in copy of the same table and
reports `"presets": "built-in"` in its ready line.

| Anchor (Lichess) | Status | Self-play measured vs Lichess target (ACPL / acc / blunders per 100) |
|---|---|---|
| 800 | field-test (2026-10-10) | 82.1 / 74.4% / 9.40 vs 82.8 / 69.9% / 9.46 (1106 moves) |
| 1000 | **calibrated** (2026-10-09) | 79.6 / 76.0% / 9.05 vs 75.3 / 72.0% / 9.33 (1426 moves) |
| 1200 | field-test (2026-10-10) | 65.3 / 77.1% / 6.62 vs 67.6 / 74.6% / 8.32 (1223 moves) |
| 1400 | field-test (2026-10-10) | 60.4 / 83.7% / 4.02 vs 60.2 / 76.9% / 7.18 (1094 moves) |

`status`: **calibrated** means validated and shipped as the default. **field-test** means
the anchor matched the target ACPL in self-play but hasn't been validated in
human games. Accuracy reads high in self-play at every level, and for 1200/1400
the inaccuracy/mistake/blunder rates are under target, so those presets may
play somewhat stronger than their band.

Targets between anchors interpolate every knob linearly (`K` is rounded);
targets outside the range clamp to the nearest anchor (below 800 → 800,
above 1400 → 1400; sub-1000 targets interpolate between 800 and 1000). Any knob can be
overridden for one run with `CHESS_ELO_OVERRIDES='{"T":5.0}'`.

Schema per anchor: `status` (calibrated | field-test), `calibrated` (bool), `knobs` (required: T, ALPHA, KAPPA, K,
P_BLIND, BLIND_TEMPT, BLIND_MIN_LOSS, TEACH_BONUS, TEACH_MAX_LOSS,
GUARD_QUEEN_P, GUARD_MATE1_P), and informational `measured` / `target` /
`method` / `note`. See ../CALIBRATION.md for how to calibrate a new anchor.

`opening-book-1400.json` is the human opening book used by the 1200/1400 presets
(`BOOK_MOVES`): Lichess rated Blitz+Rapid games, average rating 1400-1799, first 12 plies,
moves with >= 5% share and >= 50 games that lose <= 50cp vs Stockfish (depth 18).
Built offline by `tools/opening-book/build_book.py`; see CALIBRATION.md.
