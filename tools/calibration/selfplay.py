#!/usr/bin/env python3
"""Self-play / fixed-opponent driver for chess-sf-worker.py (real worker protocol).
usage: selfplay.py OUT.json ELO OPP SEED [maxplies] [ponder0/1]
OPP: self | sf1320 | old6 (legacy Skill-6 pipeline of the same worker)"""
import json, subprocess, sys, os, chess, chess.engine, time
out, elo, opp, seed = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
maxp = int(sys.argv[5]) if len(sys.argv) > 5 else 160
ponder = (sys.argv[6] == "1") if len(sys.argv) > 6 else True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import stockfish_bin, WORKER, PYTHON
SF = stockfish_bin()

class W:
    def __init__(self, path, seed):
        env = dict(os.environ, CHESS_WORKER_SEED=str(seed))
        self.p = subprocess.Popen([PYTHON, "-u", path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        self.hello = json.loads(self.p.stdout.readline())
    def req(self, m):
        self.p.stdin.write(json.dumps(m) + "\n"); self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())
    def quit(self):
        try: self.req({"cmd": "quit"})
        except Exception: pass

bot = W(WORKER, seed)
oppw = None; oppe = None
if opp == "old6": oppw = W(os.environ.get("CHESS_OLD_WORKER", WORKER), int(seed) + 7)
if opp == "sf1320":
    oppe = chess.engine.SimpleEngine.popen_uci(SF); oppe.configure({"UCI_LimitStrength": True, "UCI_Elo": 1320})
import random; rnd = random.Random(int(seed))
botcolor = {"self": None}.get(opp, rnd.choice(["w", "b"]))
b = chess.Board(); moves = []; recs = []; t0 = time.time()
while not b.is_game_over(claim_draw=False) and len(moves) < maxp:
    if b.can_claim_threefold_repetition() or b.is_repetition(3): break
    side = "w" if b.turn else "b"
    legal = [b.san(m) for m in b.legal_moves]
    if botcolor is None or side == botcolor:
        ts = time.time()
        r = bot.req({"cmd": "choose", "fen": b.fen(), "elo": elo, "legal": legal, "weAreWhite": b.turn, "moves": moves})
        if not r.get("ok"): print("ERR", r, file=sys.stderr); break
        san = r["san"]; recs.append(dict(ply=len(moves) + 1, side=side, san=san, source=r.get("source"), cached=r.get("cached"),
                     deviated=r.get("deviated"), events=r.get("events"), dt=round(time.time() - ts, 2), gate=r.get("gate")))
    elif oppw:
        r = oppw.req({"cmd": "choose", "fen": b.fen(), "skill": 6, "legal": legal, "weAreWhite": b.turn, "moves": moves}); san = r["san"]
    else:
        san = b.san(oppe.play(b, chess.engine.Limit(time=0.1)).move)
    b.push_san(san); moves.append(san)
    if ponder and (botcolor is None or side == botcolor) and not b.is_game_over():
        bot.req({"cmd": "ponder", "fen": b.fen(), "elo": elo, "maxLines": 3})
res = b.result(claim_draw=True) if b.is_game_over(claim_draw=True) else "*"
reason = ("checkmate" if b.is_checkmate() else "stalemate" if b.is_stalemate() else "threefold" if (b.can_claim_threefold_repetition() or b.is_repetition(3)) else
          "insufficient" if b.is_insufficient_material() else "fifty" if b.can_claim_fifty_moves() else "plycap")
json.dump(dict(elo=elo, opp=opp, seed=seed, bot=botcolor, moves=moves, result=res, reason=reason, recs=recs,
               secs=round(time.time() - t0), overrides=bot.hello.get("eloOverrides")), open(out, "w"), indent=1)
bot.quit(); oppw and oppw.quit(); oppe and oppe.quit()
print(out, res, reason, len(moves), "plies", round(time.time() - t0), "s")
