#!/usr/bin/env python3
"""Score games with the same code as metrics.py, depth D.
usage: score.py DEPTH game1.json ... -> writes game.scored.json next to each"""
import json, sys, os, chess, chess.engine
D = int(sys.argv[1]); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); from _paths import stockfish_bin; SF = stockfish_bin()
eng = chess.engine.SimpleEngine.popen_uci(SF, timeout=120); eng.configure({"Threads": 1, "Hash": 128})
def ev(board):
    if board.is_game_over():
        return (-100000 if board.is_checkmate() else 0), None
    info = eng.analyse(board, chess.engine.Limit(depth=D))
    return info["score"].pov(board.turn).score(mate_score=100000), info["pv"][0]
for f in sys.argv[2:]:
    o = f.replace(".json", ".scored.json")
    if os.path.exists(o): continue
    g = json.load(open(f)); b = chess.Board(); plies = []
    cur = ev(b)
    for i, s in enumerate(g["moves"]):
        mover = "w" if b.turn else "b"; nl = b.legal_moves.count()
        mv = b.parse_san(s); bsan = b.san(cur[1]) if cur[1] else None
        b.push(mv); nxt = ev(b)
        plies.append(dict(ply=i + 1, mover=mover, san=s, before=cur[0], after=-nxt[0], nlegal=nl, bestSan=bsan))
        cur = nxt
    json.dump(dict(game=g, depth=D, plies=plies), open(o, "w"))
    print("scored", o, len(plies))
eng.quit()
