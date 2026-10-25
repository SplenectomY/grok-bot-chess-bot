#!/usr/bin/env python3
"""Simulated opening replay: the 1400 picker's moves 1-10, before vs after the
opening book + top-3 limit (2026-10-25).

N games per mode, bot White in even games and Black in odd ones. Opponent model
(identical in both modes, seeded per game): a book move sampled by Lichess
popularity when the position is in presets/opening-book-1400.json, otherwise a
random move among Stockfish's top 3 (depth 8) within 60cp of the best.
"before" = BOOK_MOVES 0 and OPEN_TOPK_MOVES 0 (836bb2d behaviour); "after" =
preset values. Per bot move we record the deep-view (depth 12 / 0.55 s, the
picker's own judge) cp loss of the chosen move vs the top move and whether it
is a quiet flank-pawn move (a/b/g/h pawn, no capture) or a book move.
Usage: python3 opening_replay.py --games 200 --jobs 6 [--out results.json]
"""
import argparse, importlib.util, json, os, random, sys, multiprocessing as mp
import chess, chess.engine
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import WORKER

FLANK_FILES = (0, 1, 6, 7)

def flank(b, m):
    p = b.piece_at(m.from_square)
    return p.piece_type == chess.PAWN and chess.square_file(m.from_square) in FLANK_FILES and not b.is_capture(m)

def run(args):
    mode, games = args
    spec = importlib.util.spec_from_file_location("worker", WORKER)
    W = importlib.util.module_from_spec(spec); spec.loader.exec_module(W)
    W.ensure_full_engine().configure({"Threads": 1})
    if mode == "before":
        W._ELO_OVERRIDES.update({"BOOK_MOVES": 0, "OPEN_TOPK_MOVES": 0})
    book = W.opening_book()
    opp = chess.engine.SimpleEngine.popen_uci(W.SF_BIN)
    rows = []
    for g in games:
        W.RNG.seed(1000 + g)
        orng = random.Random(5000 + g)
        bot_white = g % 2 == 0
        b = chess.Board()
        while b.fullmove_number <= 10 and not b.is_game_over():
            if (b.turn == chess.WHITE) == bot_white:
                r = W.elo_choose(b, 1400, None)
                m = b.parse_san(r["san"])
                sgn = 1 if b.turn == chess.WHITE else -1
                loss = sgn * (r["bestScore"] - r["score"])
                rows.append({"game": g, "move": b.fullmove_number, "san": r["san"], "source": r["source"],
                             "loss": loss, "flank": flank(b, m)})
            else:
                e = book.get(W._epd(b))
                if e:
                    ms = e["moves"]; x = orng.random() * sum(t["games"] for t in ms)
                    for t in ms:
                        x -= t["games"]
                        if x <= 0:
                            break
                    m = b.parse_san(t["san"])
                else:
                    infos = opp.analyse(b, chess.engine.Limit(depth=8), multipv=3)
                    sc = [(i["pv"][0], i["score"].pov(b.turn).score(mate_score=10000)) for i in infos if i.get("pv")]
                    ok = [mv for mv, s in sc if s >= sc[0][1] - 60]
                    m = orng.choice(ok)
            b.push(m)
    opp.quit(); W.reset_engines()
    return mode, rows

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=200)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    chunks = [list(range(i, a.games, a.jobs // 2 or 1)) for i in range(a.jobs // 2 or 1)]
    tasks = [(mode, c) for mode in ("before", "after") for c in chunks]
    with mp.Pool(len(tasks)) as pool:
        res = pool.map(run, tasks)
    data = {"before": [], "after": []}
    for mode, rows in res:
        data[mode] += rows
    summary = {}
    for mode, rows in data.items():
        n = len(rows)
        s = {"botMoves": n, "flankPer100": round(100 * sum(r["flank"] for r in rows) / n, 1),
             "meanCpLoss": round(sum(max(0, r["loss"]) for r in rows) / n, 1),
             "bookShare": round(sum(r["source"] == "book" for r in rows) / n, 3),
             "lossOver50Per100": round(100 * sum(r["loss"] > 50 for r in rows) / n, 1)}
        for lo, hi in ((1, 6), (7, 10)):
            sub = [r for r in rows if lo <= r["move"] <= hi]
            s[f"moves{lo}-{hi}"] = {"n": len(sub), "flankPer100": round(100 * sum(r["flank"] for r in sub) / len(sub), 1),
                                    "meanCpLoss": round(sum(max(0, r["loss"]) for r in sub) / len(sub), 1)}
        summary[mode] = s
    print(json.dumps(summary, indent=1))
    if a.out:
        json.dump({"summary": summary, "rows": data}, open(a.out, "w"), indent=0)
