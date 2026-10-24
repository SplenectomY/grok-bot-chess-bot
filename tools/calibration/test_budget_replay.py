#!/usr/bin/env python3
"""Blunder-budget counter replay test (2026-10-24).

Replays every bot move of four field games (MMPBHU, AHJVWV, EBW6BK, VVCV9P)
through the worker's post-choice counter (budget_verdict) using the same deep
view elo_choose builds (MultiPV over all legal moves, depth 12 / 0.55 s), and
compares with the old counter (loop-log budget-records) and the post-game
analysis (Stockfish depth 18, blunder = >= 15 win%-points, metrics.judge).

Pass: every analysis win-chance blunder is recorded, none of the known false
positives is recorded, and no other move is recorded -- except borderline
moves whose analysis loss is within BORDER win%-points of the 15-point line
(two depth-18 searches disagree by that much; listed as "border"), and extra
records of analysis mistakes (>= EXTRA_OK win%-points; listed as "warn").
A known false positive or an extra record below EXTRA_OK always fails.
Usage: python3 test_budget_replay.py [--threads N]
"""
import importlib.util, json, os, sys, time
import chess, chess.engine
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import WORKER

spec = importlib.util.spec_from_file_location("worker", WORKER)
W = importlib.util.module_from_spec(spec); spec.loader.exec_module(W)
threads = int(sys.argv[sys.argv.index("--threads") + 1]) if "--threads" in sys.argv else 1
W.ensure_full_engine().configure({"Threads": threads})
FX = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "budget_replay_games.json")))

def label(ply, san):
    n = (ply + 1) // 2
    return f"{n}.{san}" if ply % 2 else f"{n}...{san}"

BORDER = 2.5
EXTRA_OK = 10.0
def analysis_loss(g, ply):
    """Analysis win%-loss (metrics.judge scale), None for mate-encoded evals."""
    be, af = g["analysis_evals"][str(ply)]
    if abs(be) >= 50000 or abs(af) >= 50000:
        return None
    return 50 * (W._wc(be) - W._wc(af))

fails, rows, vtimes = 0, [], []

# budget_judge unit cases (our POV, analysis encoding; mate in n = 100000-n)
M = lambda n: 100000 - n
for best, ch, want, name in [
        (M(3), M(7), False, "slower mate while winning"),
        (M(2), M(5), False, "mate-in-2 -> mate-in-5"),
        (M(4), 1100, False, "missed mate, still +11"),
        (M(4), 250, True, "missed mate, now +2.5"),
        (-M(5), -M(2), False, "already mated either way"),
        (-950, -M(3), False, "walks into mate from -9.5 (already lost)"),
        (80, -M(4), True, "walks into mate from +0.8"),
        (1500, 700, False, "+15 -> +7 (still won)"),
        (-700, -1500, False, "-7 -> -15 (already lost)"),
        (80, -90, True, "+0.8 -> -0.9 (-15.5 win%)"),
        (450, 250, False, "+4.5 -> +2.5 (-12.5 win%)"),
        (430, 100, True, "+4.3 -> +1.0 (-24 win%)")]:
    got, why = W.budget_judge(best, ch)
    ok = got == want
    print(("PASS " if ok else "FAIL ") + f"judge {name}: {got} ({why})")
    fails += not ok
for room, g in FX.items():
    bc = chess.WHITE if g["bot"] == "w" else chess.BLACK
    b = chess.Board()
    old = {p: k for p, k in g["old_records"]}
    new = {}
    for i, san in enumerate(g["moves"]):
        ply = i + 1
        if b.turn == bc and not b.is_game_over():
            legal = list(b.legal_moves)
            mv = b.parse_san(san)
            if len(legal) > 1:
                W.ensure_full_engine().configure({"Clear Hash": None})
                deep_infos = W._all_lines(W.ensure_full_engine(), b,
                                          chess.engine.Limit(depth=W.ELO_DEEP_DEPTH, time=W.ELO_DEEP_TIME), legal)
                for m in legal:
                    if m not in deep_infos:
                        deep_infos.update(W._all_lines(W.ensure_full_engine(), b, chess.engine.Limit(time=0.1), [m]))
                deep = {m: W._cp_mate(deep_infos[m], bc) for m in legal}
                top = max(legal, key=lambda m: (deep[m], -legal.index(m)))
                t0 = time.time()
                kind, why = W.budget_verdict(b, mv, top, deep_infos, legal)
                dt = time.time() - t0
                if not why.startswith(("screen", "best")):
                    vtimes.append(dt)
                if kind or ply in old or ply in g["analysis_blunders"] or ply in g["analysis_hung_rule"] \
                        or ply in g["known_false_positives"]:
                    new[ply] = (kind, why)
        b.push_san(san)
    for ply in sorted(set(new) | set(old)):
        kind, why = new.get(ply, (None, ""))
        verdict = ("blunder (>=15 win%)" if ply in g["analysis_blunders"] else
                   "hung piece (material rule, <15 win%)" if ply in g["analysis_hung_rule"] else
                   "not a blunder")
        rows.append((room, label(ply, g["moves"][ply - 1]), old.get(ply, "-"), "RECORD" if kind else "-", verdict, why))
        want = ply in g["analysis_blunders"]
        al = analysis_loss(g, ply)
        if al is not None:
            rows[-1] = rows[-1][:4] + (f"{verdict} [{al:.1f}]",) + rows[-1][5:]
        if bool(kind) != want and ply not in g["analysis_hung_rule"]:
            if ply in g["known_false_positives"]:
                fails += 1
                rows[-1] = rows[-1][:5] + (why + "  <-- FAIL (known false positive)",)
            elif al is not None and abs(al - W.BUDGET_WP_BLUNDER) <= BORDER:
                rows[-1] = rows[-1][:5] + (why + "  (border)",)
            elif kind and al is not None and al >= EXTRA_OK:
                rows[-1] = rows[-1][:5] + (why + "  (warn: analysis mistake-level)",)
            else:
                fails += 1
                rows[-1] = rows[-1][:5] + (why + "  <-- FAIL",)

w = [max(len(str(r[i])) for r in rows) for i in range(5)]
print(f"{'game':<7} {'move':<10} {'old':<10} {'new':<7} {'analysis [win% lost]':<44} detail")
for r in rows:
    print(f"{r[0]:<7} {r[1]:<10} {r[2]:<10} {r[3]:<7} {r[4]:<44} {r[5]}")
if vtimes:
    print(f"verify searches: {len(vtimes)}, mean {sum(vtimes)/len(vtimes):.2f}s, max {max(vtimes):.2f}s")
print("FAILS:", fails)
W.reset_engines()
sys.exit(1 if fails else 0)
