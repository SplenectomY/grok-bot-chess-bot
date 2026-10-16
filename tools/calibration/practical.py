#!/usr/bin/env python3
"""Practical-blunder count (catches clamp-hidden material losses).
A bot move is a practical blunder if the win-chance metric calls it a blunder, OR
it loses >= PIECE (3) pawns of material within 5 plies (our move, reply, ours, reply, ours),
while the raw (unclamped, mate capped at 3000) eval drops >= MIN_DROP cp.
usage: practical.py files.scored.json ...   (bot side(s) per game['bot'])
       or import: practical(plies, color, moves) -> (n, count, list)"""
import json, sys, os, random, statistics as st, chess
exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "metrics.py")).read())
V = {1: 1, 2: 3, 3: 3, 4: 5, 5: 9, 6: 0}
PIECE, MIN_DROP, CAP = 3, 100, 3000
def mat(b): return sum(V[p.piece_type] * (1 if p.color else -1) for p in b.piece_map().values())
def practical(plies, color, moves):
    b = chess.Board(); m = [mat(b)]
    for s in moves: b.push_san(s); m.append(mat(b))
    sgn = 1 if color == "w" else -1
    n = 0; hits = []; flags = []
    for i, p in enumerate(plies):
        if p["mover"] != color or p["nlegal"] <= 1: continue
        n += 1
        j = min(i + 5, len(m) - 1)
        lost = sgn * (m[i] - m[j])
        cap = lambda v: max(-CAP, min(CAP, v))
        drop = cap(p["before"]) - cap(p["after"])
        metric = judge(p["before"], p["after"]) == "B"
        prac = lost >= PIECE and drop >= MIN_DROP
        f = metric or prac
        flags.append(1 if f else 0)
        if f: hits.append((p["ply"], p["san"], "metric" if metric else "material", lost, drop))
    return n, flags, hits
if __name__ == "__main__":
    allf = []; games = 0
    for f in sys.argv[1:]:
        d = json.load(open(f)); g = d["game"]; games += 1
        for c in (["w", "b"] if g["bot"] is None else [g["bot"]]):
            n, fl, h = practical(d["plies"], c, g["moves"]); allf += fl
    n = len(allf); k = sum(allf); rs = random.Random(1)
    boots = sorted(100 * sum(rs.choices(allf, k=n)) / n for _ in range(2000))
    print(f"practical blunders: {k}/{n} = {100*k/n:.2f}/100 (90%CI {boots[100]:.2f}-{boots[1900]:.2f})")
