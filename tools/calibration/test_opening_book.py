#!/usr/bin/env python3
"""Opening book + early top-k tests (2026-10-25, after EUCETV).

  1. Book moves come out for both colors (1400 and 1200) in common positions
     (a teach move may take its normal-pick chance first):
     start position (White), after 1.e4 and after 1.d4 (Black), and they are
     moves listed in presets/opening-book-1400.json for that position.
  2. 800/1000 (BOOK_MOVES 0, OPEN_TOPK_MOVES 0) never use the book or the limit.
  3. Out of book before move 11: "book: out of book", and the normal pick is
     drawn from the top-3 pool (plus the deep top move) -- checked on 12 draws.
  4. From move 11 on: no book and no top-3 limit (EUCETV after 10...Nc6).
  5. Tactical override: an in-book position where the opponent has just hung
     the queen (book injected for the test) -> the book is not used and the
     bot takes the queen; same out of book with the top-3 limit active; and
     a capture that beats every (injected) book move by >= BOOK_TACTIC_CP.
Usage: python3 test_opening_book.py
"""
import importlib.util, json, os, re, sys
import chess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _paths import WORKER

spec = importlib.util.spec_from_file_location("worker", WORKER)
W = importlib.util.module_from_spec(spec); spec.loader.exec_module(W)
W.ensure_full_engine().configure({"Threads": 1})
BOOK = W.opening_book()

fails = 0
def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (" :: " + detail if detail else ""))
    fails += (not cond)

def pos(sans, start=chess.STARTING_FEN):
    b = chess.Board(start)
    for s in sans.split():
        b.push_san(s)
    return b

def pick(b, elo):
    r = W.elo_choose(b, elo, None)
    return r, [e for e in r["events"] if e.startswith(("book:", "open-top"))]

check("book file loaded", len(BOOK) >= 20, f"{len(BOOK)} positions from {W.BOOK_FILE}")

# 1. both colors. A qualifying teach move keeps its normal-pick chance, so
#    each position gets up to 6 tries: at least one book move, and every
#    non-book pick must be a teach move drawn through the book's teach roll.
for elo in (1400, 1200):
    for line in ("", "e4", "d4", "e4 e5", "e4 c5", "d4 d5"):
        b = pos(line) if line else chess.Board()
        ent = BOOK.get(W._epd(b))
        if not ent:
            check(f"{elo} book has '{line or 'start'}'", False, "position missing from the book")
            continue
        names = [m["san"] for m in ent["moves"]]
        got, other = [], []
        for _ in range(6):
            r, ev = pick(b, elo)
            if r["source"] == "book":
                got.append(r["san"])
                if r["san"] not in names:
                    other.append(("not in book", r["san"]))
                break
            if not any("teach move" in e and "drawn" in e for e in ev):
                other.append((r["san"], r["source"], ev))
        side = "White" if b.turn else "Black"
        check(f"{elo} {side} book move after '{line or 'start'}'", bool(got) and not other,
              f"book pick {got} options={names} other={other}")

# 2. 800/1000 unchanged
for elo in (800, 1000):
    r, ev = pick(chess.Board(), elo)
    check(f"{elo} start: no book, no top-k", r["source"] != "book" and not ev, f"{r['san']} {r['source']} {ev}")

# 3. out of book, moves <= 10: top-3 pool
b = pos("e4 a5 Nf3 a4 Bc4 h5")          # White's 4th move, well out of book
assert W._epd(b) not in BOOK and b.fullmove_number == 4
bad = []
for i in range(12):
    r, ev = pick(b, 1400)
    pool_ev = [e for e in ev if e.startswith("open-top3")]
    if r["source"] == "book" or not any(e == "book: out of book" for e in ev) or not pool_ev:
        bad.append(("events", r["san"], ev)); continue
    pool = re.search(r"pool (\[.*?\])", pool_ev[0]).group(1)
    pool = eval(pool)
    if r["san"] not in pool or len(pool) > 4:
        bad.append(("pool", r["san"], pool))
check("1400 out of book, move 4: 12 draws all from the top-3 pool", not bad, str(bad[:2]) if bad else f"last pool {pool}")

# 4. move 11+: normal behaviour
b = pos("e3 e5 h3 d5 a3 e4 Nc3 Be6 b3 Bd6 d3 f5 Bb2 Nf6 Qd2 Be5 f4 Bxc3 Bxc3 Nc6")
assert b.fullmove_number == 11
r, ev = pick(b, 1400)
check("1400 move 11 (EUCETV): no book, no top-3 limit", not ev, f"{r['san']} {ev}")

# 5. tactical override: hung queen
b = pos("e4 e5 Nf3 Qh4")                  # 2...Qh4?? 3.Nxh4 wins the queen
W._BOOK = dict(BOOK); W._BOOK[W._epd(b)] = {"games": 1000, "moves": [{"san": "Bc4", "games": 600}, {"san": "Nc3", "games": 400}]}
r, ev = pick(b, 1400)
check("1400 in book (injected) but queen hung: book not used, takes the queen",
      r["source"] != "book" and r["san"] == "Nxh4" and any("drop" in e or "skipped" in e or "no book" in e for e in ev),
      f"{r['san']} {r['source']} {ev}")
W._BOOK = dict(BOOK)
r, ev = pick(b, 1400)
check("1400 out of book, queen hung, top-3 limit active: takes the queen", r["san"] == "Nxh4", f"{r['san']} {ev}")

# 5b. tactical margin: 1.d4 e5?! 2.dxe5 wins a pawn (capture top). Book injected
#     with quiet moves and the live loss cap opened up, so only BOOK_TACTIC_CP
#     can stop the book.
b = pos("d4 e5")
W._BOOK = dict(BOOK); W._BOOK[W._epd(b)] = {"games": 1000, "moves": [{"san": "Nf3", "games": 600}, {"san": "e3", "games": 400}]}
W._ELO_OVERRIDES.update({"BOOK_MAX_LOSS_CP": 1000})
r, ev = pick(b, 1400)
check("1400 in book (injected), capture clearly best: book skipped by the tactic margin",
      r["source"] != "book" and any("skipped (tactic" in e for e in ev), f"{r['san']} {r['source']} {ev}")
W._ELO_OVERRIDES.pop("BOOK_MAX_LOSS_CP"); W._BOOK = dict(BOOK)

W.reset_engines()
print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
