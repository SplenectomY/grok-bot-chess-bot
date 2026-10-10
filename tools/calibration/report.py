#!/usr/bin/env python3
"""Aggregate bot metrics over *.scored.json (bot side(s) only). usage: report.py files..."""
import json, sys, statistics as st, random
import os; exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "metrics.py")).read())
cpl = []; I = M = B = 0; accs = []; games = 0; per_game = []
src = {}; perwho=[]
for f in sys.argv[1:]:
    d = json.load(open(f)); g = d["game"]; plies = d["plies"]
    sides = ["w", "b"] if g["bot"] is None else [g["bot"]]
    games += 1
    for c in sides:
        s = summarize(plies, c)
        if not s["n"]: continue
        cpl += s["cpl"]; I += s["I"]; M += s["M"]; B += s["B"]
        accs.append(game_acc(plies, c)); per_game.append(s["acpl"])
        for r in g["recs"]:
            if r["side"] == c: src[r["source"]] = src.get(r["source"], 0) + 1
n = len(cpl)
rs = random.Random(1); boots = sorted(st.mean(rs.choices(cpl, k=n)) for _ in range(2000))
print(f"games={games} botSideGames={len(accs)} botMoves(nonforced)={n}")
print(f"ACPL={st.mean(cpl):.1f} (90%CI {boots[100]:.1f}-{boots[1900]:.1f})  meanGameAcc={st.mean(accs):.1f}%  medianGameAcc={st.median(accs):.1f}%")
print(f"per100: inacc={100*I/n:.2f} mist={100*M/n:.2f} blund={100*B/n:.2f}   (I/M/B={I}/{M}/{B})")
print("sources:", src)
