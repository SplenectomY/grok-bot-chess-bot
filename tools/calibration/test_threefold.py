#!/usr/bin/env python3
"""Threefold / won-position regression tests for chess-sf-worker.py (real protocol)."""
import json, subprocess, os, sys, chess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import stockfish_bin, WORKER as W, PYTHON
def worker(seed):
    p = subprocess.Popen([PYTHON, "-u", W], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                         env=dict(os.environ, CHESS_WORKER_SEED=str(seed)))
    p.stdout.readline(); return p
def req(p, m):
    p.stdin.write(json.dumps(m) + "\n"); p.stdin.flush(); return json.loads(p.stdout.readline())
def pos(start, moves):
    b = chess.Board(start)
    for m in moves: b.push_san(m)
    return b
fails = 0
def check(name, cond, detail):
    global fails
    print(("PASS " if cond else "FAIL ") + name + " :: " + detail); fails += (not cond)

# 1. Up a rook, position about to repeat (2nd and 3rd occurrence)
START = "4k3/8/8/8/8/8/8/R3K3 w - - 0 1"
for reps, hist in ((2, ["Ra2", "Kd8", "Ra1", "Ke8"]), (3, ["Ra2", "Kd8", "Ra1", "Ke8", "Ra2", "Kd8", "Ra1", "Ke8"])):
    b = pos(START, hist)
    for mode in ({"elo": 1000}, {"skill": 6}):
        p = worker(1)
        req(p, {"cmd": "cache_put", "fen": b.fen(), "san": "Ra2", **mode})
        r = req(p, {"cmd": "choose", "fen": b.fen(), "moves": hist, "startFen": START, "weAreWhite": True, **mode})
        bb = b.copy(); bb.push_san(r["san"])
        check(f"cache-forced repeat (Ra2 would be occurrence #{reps}) {mode}", r["san"] != "Ra2" and not bb.is_repetition(2),
              f"played {r['san']} deviated={r.get('deviated')} ev={[e for e in r['events'] if 'won' in e]}")
        req(p, {"cmd": "quit"})
    # fresh Elo-mode draws, many seeds
    bad = []
    for seed in range(12):
        p = worker(100 + seed)
        r = req(p, {"cmd": "choose", "fen": b.fen(), "moves": hist, "startFen": START, "weAreWhite": True, "elo": 1000})
        bb = b.copy(); bb.push_san(r["san"])
        if bb.is_repetition(2): bad.append(r["san"])
        req(p, {"cmd": "quit"})
    check(f"12 fresh Elo-1000 draws never repeat (occurrence #{reps})", not bad, f"repeating picks: {bad}")

# 2. Not winning (equal K vs K+R? -> use bare kings + pawns equal): repetition allowed / untouched
START2 = "2r3k1/5pp1/7p/8/8/7P/5PP1/1R4K1 w - - 0 1"
hist2 = ["Rb2", "Rc7", "Rb1", "Rc8"]
b2 = pos(START2, hist2)
p = worker(3)
req(p, {"cmd": "cache_put", "fen": b2.fen(), "san": "Rb2", "elo": 1000})
r = req(p, {"cmd": "choose", "fen": b2.fen(), "moves": hist2, "startFen": START2, "weAreWhite": True, "elo": 1000})
check("equal position: repetition allowed, safeguard does not force deviation", r["san"] == "Rb2" and not (r.get("deviateReason") or "").startswith("won"),
      f"played {r['san']} ({r.get('deviateReason')})")
req(p, {"cmd": "quit"})

# 3. Check spam in a won position: after two consecutive checks, a non-best check is replaced
START3 = "8/5k2/8/8/8/8/1R6/1R4K1 w - - 0 1"
hist3 = ["Rb7+", "Ke6", "R7b6+", "Kd5"]   # our last two moves were checks
b3 = pos(START3, hist3)
import chess.engine
e = chess.engine.SimpleEngine.popen_uci(stockfish_bin())
infos = e.analyse(b3, chess.engine.Limit(depth=16), multipv=b3.legal_moves.count()); e.quit()
sc = {i["pv"][0]: i["score"].pov(chess.WHITE).score(mate_score=2100) for i in infos}
top = max(sc.values())
checks = sorted([m for m in sc if b3.gives_check(m) and sc[m] < top - 60], key=lambda m: sc[m], reverse=True)
spam = b3.san(checks[0])
p = worker(4)
req(p, {"cmd": "cache_put", "fen": b3.fen(), "san": spam, "elo": 1000})
r = req(p, {"cmd": "choose", "fen": b3.fen(), "moves": hist3, "startFen": START3, "weAreWhite": True, "elo": 1000})
check(f"3rd consecutive non-best check {spam} ({sc[checks[0]]} vs top {top}) replaced", r["san"] != spam,
      f"played {r['san']} ev={[x for x in r['events'] if 'won' in x]}")
req(p, {"cmd": "quit"})
print("FAILS:", fails)
