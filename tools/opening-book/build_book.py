#!/usr/bin/env python3
"""Build presets/opening-book-1400.json from real Lichess games (run offline, once).

The Lichess opening explorer API (explorer.lichess.ovh) now answers 401 without
an account token, so this script rebuilds the same statistic from the public
Lichess game database (database.lichess.org, CC0): it streams the first
--mb megabytes of one monthly standard-rated dump, keeps rated Blitz and Rapid
games whose average rating is 1400-1799 (= explorer buckets ratings=1400,1600),
and counts the move played in every position of the first --plies plies.

Filter: a position needs >= --min-pos games; a move needs >= --min-share of the
position's games and >= --min-games games. Then every surviving move is checked
by Stockfish (best move single-PV, each candidate via root_moves, --depth): it is dropped if it loses
more than --max-loss cp vs the engine's best move. Only positions reachable
from the start through kept book moves are written.

Usage: python3 build_book.py [--month 2026-09] [--mb 400] [--plies 12] [--depth 18]
Needs: python-chess, zstandard, Stockfish (STOCKFISH_BIN or setup.sh).
"""
import argparse, collections, io, json, os, re, subprocess, sys, time, datetime
import chess, chess.engine, zstandard
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "calibration"))
from _paths import stockfish_bin, REPO

ap = argparse.ArgumentParser()
ap.add_argument("--month", default="2026-09")
ap.add_argument("--mb", type=int, default=400)
ap.add_argument("--plies", type=int, default=12)
ap.add_argument("--rmin", type=int, default=1400)
ap.add_argument("--rmax", type=int, default=1800)
ap.add_argument("--min-pos", type=int, default=300)
ap.add_argument("--min-share", type=float, default=0.05)
ap.add_argument("--min-games", type=int, default=50)
ap.add_argument("--max-loss", type=int, default=50)
ap.add_argument("--depth", type=int, default=18)
ap.add_argument("--threads", type=int, default=4)
ap.add_argument("--counts", default="", help="cache the raw counts here (JSON); reused if it exists")
ap.add_argument("--out", default=str(REPO / "presets" / "opening-book-1400.json"))
a = ap.parse_args()

def epd(b):
    return " ".join(b.fen().split()[:4])


if a.counts and os.path.exists(a.counts):
    cj = json.load(open(a.counts))
    counts = {k: collections.Counter(v) for k, v in cj["counts"].items()}
    url, scanned, kept = cj["url"], cj["scanned"], cj["kept"]
else:
    url = f"https://database.lichess.org/standard/lichess_db_standard_rated_{a.month}.pgn.zst"
    curl = subprocess.Popen(["curl", "-s", "-r", f"0-{a.mb * 1024 * 1024 - 1}", url], stdout=subprocess.PIPE)
    reader = io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(curl.stdout, read_across_frames=True),
                              encoding="utf-8", errors="replace")


    counts = collections.defaultdict(collections.Counter)  # epd -> Counter(san)
    TOK = re.compile(r"\{[^}]*\}|\d+\.\.\.|\d+\.|[!?]+|1-0|0-1|1/2-1/2|\*")
    hdr = {}; scanned = kept = 0
    t0 = time.time()
    try:
        for line in reader:
            if line.startswith("["):
                k, _, v = line[1:].partition(" ")
                hdr[k] = v.strip().rstrip("]").strip('"')
                continue
            if not line.strip() or not hdr:
                continue
            scanned += 1
            h, hdr = hdr, {}
            ev = h.get("Event", "")
            if not (ev.startswith("Rated Blitz") or ev.startswith("Rated Rapid")):
                continue
            try:
                avg = (int(h["WhiteElo"]) + int(h["BlackElo"])) / 2
            except (KeyError, ValueError):
                continue
            if not (a.rmin <= avg < a.rmax):
                continue
            toks = TOK.sub(" ", line).split()[:a.plies]
            b = chess.Board()
            try:
                for s in toks:
                    m = b.parse_san(s)
                    counts[epd(b)][b.san(m)] += 1
                    b.push(m)
            except ValueError:
                continue
            kept += 1
    except zstandard.ZstdError:
        pass  # truncated last frame of the byte range
    curl.kill()
    print(f"scanned {scanned} games, kept {kept} (rated blitz/rapid, avg {a.rmin}-{a.rmax - 1}) in {time.time()-t0:.0f}s", file=sys.stderr)

    if a.counts:
        json.dump({"url": url, "scanned": scanned, "kept": kept,
                   "counts": {k: v for k, v in counts.items() if sum(v.values()) >= 50}}, open(a.counts, "w"))

# frequency filter, walked from the start position
eng = chess.engine.SimpleEngine.popen_uci(stockfish_bin())
eng.configure({"Threads": a.threads, "Hash": 256})
book, dropped = {}, []
todo = [chess.Board()]; seen = set()
while todo:
    b = todo.pop()
    k = epd(b)
    if k in seen or len(b.move_stack) >= a.plies:
        continue
    seen.add(k)
    c = counts.get(k)
    if not c:
        continue
    tot = sum(c.values())
    if tot < a.min_pos:
        continue
    cand = [(s, n) for s, n in c.most_common() if n >= a.min_games and n / tot >= a.min_share]
    if not cand:
        continue
    # engine best (single PV), then each candidate searched on its own (root_moves)
    info = eng.analyse(b, chess.engine.Limit(depth=a.depth))
    best = info["score"].pov(b.turn).score(mate_score=10000)
    sc = {b.san(info["pv"][0]): best}
    for s_, _ in cand:
        if s_ not in sc:
            i2 = eng.analyse(b, chess.engine.Limit(depth=a.depth), root_moves=[b.parse_san(s_)])
            sc[s_] = i2["score"].pov(b.turn).score(mate_score=10000)
    best = max(sc.values())
    moves = []
    for s, n in cand:
        loss = best - sc.get(s, -10000)
        if loss > a.max_loss:
            dropped.append({"fen": k, "san": s, "games": n, "share": round(n / tot, 3), "lossCp": loss})
            continue
        moves.append({"san": s, "games": n, "share": round(n / tot, 3), "lossCp": loss})
        nb = b.copy(); nb.push_san(s); todo.append(nb)
    if moves:
        book[k] = {"games": tot, "ply": len(b.move_stack), "moves": moves}
eng.quit()
out = {
    "schema": 1,
    "description": "Human opening book for the 1200/1400 presets: Lichess rated Blitz+Rapid games, average rating "
                   f"{a.rmin}-{a.rmax - 1} (= explorer ratings=1400,1600), first {a.plies} plies. Moves need >= "
                   f"{a.min_share:.0%} share and >= {a.min_games} games in a position with >= {a.min_pos} games, and must "
                   f"lose <= {a.max_loss}cp vs Stockfish best (depth {a.depth}). Sampled by game count.",
    "source": url, "bytesRead": a.mb * 1024 * 1024, "gamesScanned": scanned, "gamesUsed": kept,
    "built": datetime.date.today().isoformat(), "plies": a.plies,
    "filters": {"minPosGames": a.min_pos, "minShare": a.min_share, "minGames": a.min_games,
                "maxLossCp": a.max_loss, "sfDepth": a.depth},
    "positions": book, "droppedBySanityCheck": dropped,
}
json.dump(out, open(a.out, "w"), indent=1)
print(f"book: {len(book)} positions, {sum(len(v['moves']) for v in book.values())} moves, "
      f"{len(dropped)} dropped by the Stockfish check -> {a.out}", file=sys.stderr)
