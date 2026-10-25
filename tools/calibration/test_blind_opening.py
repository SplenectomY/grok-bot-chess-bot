#!/usr/bin/env python3
"""No blind turns in the opening (2026-10-25, ARSHJ7 1...f6).

Blind turns (simulated overlooks) are skipped while board.fullmove_number <=
BLIND_OPEN_MOVES (10 at 1200/1400 = plies 1-20, the same "opening" the phase
split uses; 0 at 800/1000 = unchanged). The blind-turn RNG draw is forced to
fire (first RNG.random() of the pick -> 0.0) so every check exercises the blind branch.

Checks:
  1. ARSHJ7 after 1.d4 (Black, move 1): 1400 and 1200 log "blind-turn
     skipped: opening" and never "refutation overlooked", so 1...f6 can no
     longer come from a blind turn; 800/1000 still take the blind branch.
  2. Boundary: move 10 (ARSHJ7 after 10.c4, Black) is skipped; move 11
     (ARSHJ7 after 11.b3, Black) fires the blind turn again at 1400 and 1200.
  3. Presets file and built-in table agree on BLIND_OPEN_MOVES.
Usage: python3 test_blind_opening.py
"""
import importlib.util, json, os, random, sys
import chess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import WORKER, REPO

spec = importlib.util.spec_from_file_location("worker", WORKER)
W = importlib.util.module_from_spec(spec); spec.loader.exec_module(W)
W.ensure_full_engine().configure({"Threads": 1})

class ForceBlind(random.Random):
    """First draw after arm() returns 0.0. In a non-won position the first
    RNG draw in elo_choose is the blind-turn draw; later draws are normal."""
    armed = False
    def arm(self):
        self.armed = True
    def random(self):
        if self.armed:
            self.armed = False
            return 0.0
        return super().random()
W.RNG = ForceBlind(1)

ARSHJ7 = ("d4 f6 e4 b6 d5 Na6 Bxa6 Bxa6 Nf3 e5 Na3 Bc5 Be3 Bb4+ "
          "c3 Bc5 Bxc5 bxc5 c4 Qb8 b3").split()   # first 21 plies (through 11.b3)
def pos(n):
    b = chess.Board()
    for s in ARSHJ7[:n]:
        b.push_san(s)
    return b

fails = 0
def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (" :: " + detail if detail else ""))
    fails += (not cond)

def run(board, elo):
    W.RNG.arm()
    r = W.elo_choose(board, elo, None)
    ev = r.get("events") or []
    return r, [e for e in ev if e.startswith("blind-turn")]

b1 = pos(1)            # 1.d4, Black to move, move 1
assert b1.fullmove_number == 1 and b1.turn == chess.BLACK
for elo in (1400, 1200):
    r, bt = run(b1, elo)
    check(f"{elo} move 1 (ARSHJ7 1...f6 position): blind turn skipped",
          any("skipped: opening" in e for e in bt) and not any("overlooked" in e for e in bt), f"{bt} chose {r['san']}")
for elo in (800, 1000):
    r, bt = run(b1, elo)
    check(f"{elo} move 1: blind branch unchanged (still runs)",
          any("overlooked" in e for e in bt) and not any("skipped" in e for e in bt), f"{bt}")

b10 = pos(19)          # after 10.c4, Black's 10th move
assert b10.fullmove_number == 10 and b10.turn == chess.BLACK
r, bt = run(b10, 1400)
check("1400 move 10 (last opening move): skipped", any("skipped: opening" in e for e in bt), f"{bt}")

b11 = pos(21)          # after 11.b3, Black's 11th (11...Qb4+ available)
assert b11.fullmove_number == 11 and b11.turn == chess.BLACK
for elo in (1400, 1200):
    r, bt = run(b11, elo)
    check(f"{elo} move 11 (after the opening): blind turn fires",
          any("overlooked" in e for e in bt) and not any("skipped" in e for e in bt), f"{bt}")

pf = json.load(open(os.path.join(REPO, "presets", "elo-profiles.json")))
for k, e in pf["profiles"].items():
    want = 10.0 if k in ("1200", "1400") else 0.0
    check(f"presets {k} BLIND_OPEN_MOVES = {want:g}", e["knobs"].get("BLIND_OPEN_MOVES") == want,
          str(e["knobs"].get("BLIND_OPEN_MOVES")))
    check(f"profile {k} loaded value", W.elo_profile(int(k)).get("BLIND_OPEN_MOVES") == want)

W.reset_engines()
print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
