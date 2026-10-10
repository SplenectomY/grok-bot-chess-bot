#!/usr/bin/env python3
"""
Gate Agent Chess coaching notes before posting.

Primary: local Stockfish 17 (MultiPV).
Backups / supplements:
  - Lichess cloud-eval (cached SF) when local eng fails or as agreement check
  - Lichess syzygy tablebase when ≤7 pieces (exact DTZ/DTM)
  - Opening book: Lichess explorer (masters + rating-band lichess) when reachable
    (it may answer 401 without auth — then it is skipped for the run), else the
    local ECO index built from lichess-org/chess-openings TSVs in opening-data/
    (falls back to opening-data/eco-index.json). All book keys on both sides use
    board_key(): placement + side to move + castling, en-passant field dropped.

Rejects notes whose `better` is not clearly better than played, or whose tag
severity doesn't match engine/tablebase loss.

Usage:
  python3 verify-annotations.py --review review.json --notes notes.json \\
      [--depth 18] [--out verified.json]
  Exit 0 if nothing rejected; exit 2 if any rejected (keepers still written).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import chess
import chess.engine

ROOT = Path(__file__).resolve().parent
import os
import shutil

ROOT = Path(os.environ.get("CHESS_HOME") or ROOT)
SF = Path(os.environ.get("STOCKFISH_BIN") or (ROOT / "bin" / "stockfish" if (ROOT / "bin" / "stockfish").exists()
                                             else shutil.which("stockfish") or "stockfish"))
CLOUD = "https://lichess.org/api/cloud-eval"
TABLEBASE = "https://tablebase.lichess.ovh/standard"
EXPLORER_MASTERS = "https://explorer.lichess.ovh/masters"
EXPLORER_LICHESS = "https://explorer.lichess.ovh/lichess"
ECO_INDEX = Path(os.environ.get("CHESS_OPENING_DATA") or (ROOT / "opening-data")) / "eco-index.json"

TAG_MIN_LOSS = {
    "blunder": 200,
    "mistake": 90,
    "inaccuracy": 40,
    "missed-win": 150,
}
POSITIVE_TAGS = {"brilliant", "great", "best", "good", "interesting"}  # book handled separately


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--review", required=True)
    p.add_argument("--notes", required=True)
    p.add_argument("--depth", type=int, default=18)
    p.add_argument("--multipv", type=int, default=5)
    p.add_argument("--out")
    p.add_argument("--no-lichess", action="store_true", help="skip cloud-eval + tablebase")
    return p.parse_args()


def load_review(path):
    raw = Path(path).read_text()
    j = json.loads(raw[raw.find("{") :])
    return j, {m["at"]: m for m in j.get("moves", [])}


_HTTP_DISABLED: dict[str, int] = {}  # host -> status code that disabled it (401/403)


def http_json(url: str, timeout: float = 8.0) -> dict | None:
    """GET JSON; return None on any miss. A 401/403 (e.g. Lichess explorer now
    requires auth) disables that host for the rest of the run so we fall back
    to local data instead of retrying/crashing."""
    host = urllib.parse.urlsplit(url).netloc
    if host in _HTTP_DISABLED:
        return None
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ChessBot-verify/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _HTTP_DISABLED[host] = e.code
            print(f"note: {host} returned {e.code} (auth required) — skipping it this run; "
                  f"using local fallback", file=sys.stderr)
        return None
    except Exception:
        return None


def piece_count(board: chess.Board) -> int:
    return len(board.piece_map())


def parse_move(board: chess.Board, raw: str) -> chess.Move | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        if len(raw) in (4, 5) and raw[1].isdigit():
            mv = chess.Move.from_uci(raw)
        else:
            mv = board.parse_san(raw)
    except Exception:
        try:
            mv = chess.Move.from_uci(raw)
        except Exception:
            return None
    return mv if mv in board.legal_moves else None


# --- Tablebase (≤7 men) -------------------------------------------------

def _tb_result_for_mover(m: dict) -> str:
    """Lichess move.category is for the side to move AFTER the move.
    So category "loss" means opponent is lost ⇒ we win by playing it.
    Returns win/draw/loss from the mover's perspective.
    """
    cat = m.get("category")
    if cat in ("loss", "blessed-loss"):
        return "win"
    if cat in ("win", "cursed-win"):
        return "loss"
    if cat == "draw":
        return "draw"
    # Derive from dtz/dtm: negative ⇒ opponent to-move is losing ⇒ we win
    v = m.get("dtm") if m.get("dtm") is not None else m.get("dtz")
    if v is None:
        return "unknown"
    if int(v) < 0:
        return "win"
    if int(v) > 0:
        return "loss"
    return "draw"


def tablebase_best(board: chess.Board) -> tuple[chess.Move | None, int | None, str]:
    """Return (best_move, stm_cp_proxy, source). stm_cp_proxy uses mate-like scale."""
    fen = board.fen()
    url = TABLEBASE + "?" + urllib.parse.urlencode({"fen": fen})
    data = http_json(url)
    if not data or not data.get("moves"):
        return None, None, "tablebase-miss"
    moves = data["moves"]
    order = {"win": 0, "draw": 1, "loss": 2, "unknown": 3}

    def rank(m):
        res = _tb_result_for_mover(m)
        dtm = m.get("dtm")
        dtz = m.get("dtz")
        # Among wins, most negative dtm = fastest mate
        return (
            order.get(res, 9),
            dtm if dtm is not None else (dtz if dtz is not None else 9999),
            abs(dtz) if dtz is not None else 9999,
        )

    best = sorted(moves, key=rank)[0]
    mv = parse_move(board, best.get("uci") or best.get("san", ""))
    res = _tb_result_for_mover(best)
    if res == "win":
        dtm = best.get("dtm") or best.get("dtz") or -30
        cp = 100000 - min(abs(int(dtm)), 900) * 100
    elif res == "draw":
        cp = 0
    else:
        dtm = best.get("dtm") or best.get("dtz") or 30
        cp = -(100000 - min(abs(int(dtm)), 900) * 100)
    return mv, cp, f"tablebase:{res}"


def tablebase_eval_move(board: chess.Board, mv: chess.Move) -> int | None:
    b2 = board.copy()
    b2.push(mv)
    fen = b2.fen()
    data = http_json(TABLEBASE + "?" + urllib.parse.urlencode({"fen": fen}))
    if not data:
        return None
    # After our move, category is for opponent. Invert.
    cat = data.get("category") or "unknown"
    dtm = data.get("dtm")
    if cat in ("loss", "blessed-loss"):  # opponent losing => we winning
        d = abs(int(dtm)) if dtm is not None else 30
        return 100000 - min(d, 900) * 100
    if cat in ("win", "cursed-win"):  # opponent winning => we losing
        d = abs(int(dtm)) if dtm is not None else 30
        return -(100000 - min(d, 900) * 100)
    if cat == "draw":
        return 0
    return None


# --- Lichess cloud-eval --------------------------------------------------

def cloud_lines(board: chess.Board, multipv: int = 5) -> list[tuple[chess.Move, int]] | None:
    fen = board.fen()
    url = CLOUD + "?" + urllib.parse.urlencode({"fen": fen, "multiPv": multipv})
    data = http_json(url)
    if not data or not data.get("pvs"):
        return None
    out = []
    for pv in data["pvs"]:
        moves = (pv.get("moves") or "").split()
        if not moves:
            continue
        mv = parse_move(board, moves[0])
        if not mv:
            continue
        if "mate" in pv and pv["mate"] is not None:
            mate = int(pv["mate"])  # white POV in Lichess cloud
            cp_w = 100000 if mate > 0 else -100000
        else:
            cp_w = int(pv.get("cp") or 0)
        # Convert to side-to-move
        cp_stm = cp_w if board.turn == chess.WHITE else -cp_w
        out.append((mv, cp_stm))
    return out or None


# --- Opening explorer + local ECO ---------------------------------------

_ECO = None
ECO_TSV_GLOB = "[a-e].tsv"


def board_key(board: chess.Board) -> str:
    """Position key used on BOTH sides of every book/ECO lookup.

    EPD of placement + side to move + castling, with the en-passant field
    dropped. FEN sources disagree on ep (python-chess/some builders always
    write the pushed-over square, Lichess/chess.js/our review JSON write it
    only when an ep capture is legal), so keeping it made real book positions
    like 1.d4 c5 miss. Dropping it costs nothing for opening-book purposes and
    keeps transpositions matching by position.
    """
    return f"{board.board_fen()} {'w' if board.turn else 'b'} {board.castling_xfen()}"


def normalize_key(key: str) -> str:
    """Normalize any FEN/EPD-ish key (e.g. from an older eco-index.json) to board_key form."""
    parts = key.split()
    try:
        return board_key(chess.Board(" ".join(parts[:4] + ["0", "1"]) if len(parts) >= 4 else key))
    except Exception:
        return " ".join(parts[:3])


def _build_eco_from_tsv(paths) -> dict:
    """Build {positions, book_moves, book_positions} from lichess-org/chess-openings TSVs.

    positions:      key -> {eco, name, depth} for positions that END a named line
                    (shortest line wins on transpositions, so names are exact).
    book_moves:     key -> sorted uci moves played from that position in any line.
    book_positions: set of keys reached anywhere along any line (transposition check).
    """
    positions: dict[str, dict] = {}
    book_moves: dict[str, set] = {}
    book_positions: set[str] = set()
    for path in sorted(paths):
        for line in Path(path).read_text().splitlines()[1:]:
            cols = line.split("\t")
            if len(cols) < 3:
                continue
            eco, name, pgn = cols[0], cols[1], cols[2]
            b = chess.Board()
            book_positions.add(board_key(b))
            ok = True
            for tok in pgn.split():
                if tok.endswith(".") or tok[0].isdigit():
                    continue
                try:
                    mv = b.parse_san(tok)
                except Exception:
                    ok = False
                    break
                book_moves.setdefault(board_key(b), set()).add(mv.uci())
                b.push(mv)
                book_positions.add(board_key(b))
            if not ok:
                continue
            k = board_key(b)
            depth = len(b.move_stack)
            cur = positions.get(k)
            if cur is None or depth < cur["depth"]:
                positions[k] = {"eco": eco, "name": name, "depth": depth}
    return {
        "positions": positions,
        "book_moves": {k: sorted(v) for k, v in book_moves.items()},
        "book_positions": book_positions,
    }


def _normalize_index(raw: dict) -> dict:
    """Re-key a prebuilt eco-index.json with board_key() so both sides match."""
    positions: dict[str, dict] = {}
    for k, v in (raw.get("positions") or {}).items():
        nk = normalize_key(k)
        cur = positions.get(nk)
        if cur is None or (v.get("depth") or 999) < (cur.get("depth") or 999):
            positions[nk] = v
    book_moves: dict[str, set] = {}
    for k, v in (raw.get("book_moves") or {}).items():
        book_moves.setdefault(normalize_key(k), set()).update(v)
    book_positions = set(positions) | set(book_moves)
    return {
        "positions": positions,
        "book_moves": {k: sorted(v) for k, v in book_moves.items()},
        "book_positions": book_positions,
    }


def load_eco():
    global _ECO
    if _ECO is not None:
        return _ECO
    tsvs = list(ECO_INDEX.parent.glob(ECO_TSV_GLOB)) if ECO_INDEX.parent.exists() else []
    if tsvs:
        _ECO = _build_eco_from_tsv(tsvs)
    elif ECO_INDEX.exists():
        _ECO = _normalize_index(json.loads(ECO_INDEX.read_text()))
    else:
        _ECO = {"positions": {}, "book_moves": {}, "book_positions": set()}
    return _ECO


def eco_lookup(board: chess.Board) -> dict | None:
    eco = load_eco()
    return eco["positions"].get(board_key(board))


def eco_book_moves(board: chess.Board) -> set[str]:
    eco = load_eco()
    return set(eco["book_moves"].get(board_key(board), []))


def eco_is_book_position(board: chess.Board) -> bool:
    return board_key(board) in load_eco()["book_positions"]


def explorer_moves(board: chess.Board, *, masters: bool = True, ratings: str = "1200,1400,1600") -> list[dict] | None:
    """Return explorer move rows [{uci,san,white,draws,black,total}, ...] or None on miss/block."""
    fen = board.fen()
    if masters:
        url = EXPLORER_MASTERS + "?" + urllib.parse.urlencode({"fen": fen, "moves": 15})
    else:
        url = EXPLORER_LICHESS + "?" + urllib.parse.urlencode({
            "variant": "standard",
            "speeds": "blitz,rapid,classical",
            "ratings": ratings,
            "fen": fen,
            "moves": 15,
        })
    data = http_json(url)
    if not data or "moves" not in data:
        return None
    out = []
    for m in data["moves"]:
        w, d, b = int(m.get("white") or 0), int(m.get("draws") or 0), int(m.get("black") or 0)
        out.append({
            "uci": m.get("uci"),
            "san": m.get("san"),
            "white": w, "draws": d, "black": b,
            "total": w + d + b,
            "opening": (m.get("opening") or {}).get("name") if isinstance(m.get("opening"), dict) else None,
        })
    return out


def book_context(board: chess.Board, played: chess.Move, better: chess.Move | None, no_lichess: bool) -> dict:
    """Gather opening-book evidence for this position."""
    ctx = {
        "eco": eco_lookup(board),
        "eco_moves": eco_book_moves(board),
        "explorer": None,
        "explorer_src": None,
        "played_in_book": False,
        "better_in_book": None,
        "top_book": [],
    }
    after = board.copy()
    # name after played move if available
    after.push(played)
    ctx["eco_after"] = eco_lookup(after)

    played_uci = played.uci()
    # Direct book move from this position, or a transposition into any position
    # that appears along a book line (e.g. 1.d4 c5 2.e4 = 1.e4 c5 2.d4, B21).
    ctx["played_in_book"] = played_uci in ctx["eco_moves"] or eco_is_book_position(after)
    ctx["played_transposes"] = played_uci not in ctx["eco_moves"] and ctx["played_in_book"]

    if not no_lichess:
        rows = explorer_moves(board, masters=True)
        src = "explorer-masters"
        if rows is None:
            rows = explorer_moves(board, masters=False)
            src = "explorer-lichess"
        if rows:
            ctx["explorer"] = rows
            ctx["explorer_src"] = src
            totals = sum(r["total"] for r in rows) or 1
            ctx["top_book"] = [r for r in rows if r["total"] / totals >= 0.05 or r is rows[0]][:8]
            ctx["played_in_book"] = ctx["played_in_book"] or any(r["uci"] == played_uci for r in rows)
            if better is not None:
                ctx["better_in_book"] = any(r["uci"] == better.uci() for r in rows)

    if better is not None and ctx["better_in_book"] is None:
        b2 = board.copy()
        b2.push(better)
        ctx["better_in_book"] = better.uci() in ctx["eco_moves"] or eco_is_book_position(b2)

    return ctx


# --- Local Stockfish -----------------------------------------------------

def sf_eval_move(engine, board: chess.Board, mv: chess.Move, depth: int) -> int:
    inf = engine.analyse(board, chess.engine.Limit(depth=depth), root_moves=[mv])
    sc = inf["score"].pov(board.turn)
    if sc.is_mate():
        m = sc.mate()
        return 100000 if m and m > 0 else -100000
    return sc.score(mate_score=100000)


def sf_best(engine, board: chess.Board, depth: int, multipv: int) -> tuple[chess.Move, int, list[tuple[chess.Move, int]]]:
    infos = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)
    if isinstance(infos, dict):
        infos = [infos]
    ranked = []
    for info in infos:
        pv = info.get("pv") or []
        if not pv:
            continue
        sc = info["score"].pov(board.turn)
        if sc.is_mate():
            m = sc.mate()
            cp = 100000 if m and m > 0 else -100000
        else:
            cp = sc.score(mate_score=100000)
        ranked.append((pv[0], cp))
    if not ranked:
        raise RuntimeError("stockfish returned no lines")
    return ranked[0][0], ranked[0][1], ranked


def main():
    args = parse_args()
    _, by_at = load_review(args.review)
    notes = json.loads(Path(args.notes).read_text())
    if not isinstance(notes, list):
        sys.exit("notes must be a JSON array")

    engine = chess.engine.SimpleEngine.popen_uci(str(SF))
    kept, rejected = [], []
    sources_used = {"stockfish": 0, "cloud-eval": 0, "tablebase": 0, "eco": 0, "explorer": 0}

    try:
        for n in notes:
            at = n.get("at")
            if at in (None, "summary") or str(at).lower() in ("summary", "game", "overall"):
                kept.append(n)
                continue
            move = by_at.get(at)
            if not move:
                rejected.append((n, f"unknown ply {at}"))
                continue

            board = chess.Board(move["fenBefore"])
            played = parse_move(board, move.get("uci") or move.get("san", ""))
            if not played:
                rejected.append((n, f"cannot parse played move at {at}"))
                continue

            tag = (n.get("tag") or "").lower()
            better_raw = n.get("better")
            use_tb = (not args.no_lichess) and piece_count(board) <= 7

            best_mv = None
            best_stm = None
            src = "stockfish"
            played_stm = None
            better_mv = None
            better_stm = None

            # 1) Endgame: tablebase is authoritative
            if use_tb:
                tb_best, tb_cp, tb_src = tablebase_best(board)
                if tb_best is not None:
                    best_mv, best_stm, src = tb_best, tb_cp, tb_src
                    sources_used["tablebase"] += 1
                    played_stm = tablebase_eval_move(board, played)
                    if better_raw:
                        better_mv = parse_move(board, better_raw)
                        if better_mv:
                            better_stm = tablebase_eval_move(board, better_mv)

            # 2) Local Stockfish (always, unless TB already fully filled)
            if best_mv is None or played_stm is None:
                try:
                    sf_best_mv, sf_best_cp, _ = sf_best(engine, board, args.depth, args.multipv)
                    sources_used["stockfish"] += 1
                    if best_mv is None:
                        best_mv, best_stm, src = sf_best_mv, sf_best_cp, "stockfish"
                    if played_stm is None:
                        played_stm = sf_eval_move(engine, board, played, args.depth)
                    if better_raw and better_stm is None:
                        better_mv = parse_move(board, better_raw)
                        if better_mv is None:
                            rejected.append((n, f"illegal/unparseable better={better_raw!r}"))
                            continue
                        better_stm = sf_eval_move(engine, board, better_mv, args.depth)
                except Exception as e:
                    # 3) Cloud-eval backup if SF fails
                    if args.no_lichess:
                        rejected.append((n, f"stockfish failed: {e}"))
                        continue
                    lines = cloud_lines(board, args.multipv)
                    if not lines:
                        rejected.append((n, f"stockfish failed and cloud-eval miss: {e}"))
                        continue
                    sources_used["cloud-eval"] += 1
                    best_mv, best_stm = lines[0]
                    src = "cloud-eval"
                    # Approximate played/better from cloud list or skip precise
                    played_stm = next((cp for mv, cp in lines if mv == played), None)
                    if played_stm is None:
                        # can't score played precisely — reject rather than guess
                        rejected.append((n, "cloud-eval backup has no score for played move"))
                        continue
                    if better_raw:
                        better_mv = parse_move(board, better_raw)
                        if better_mv is None:
                            rejected.append((n, f"illegal better={better_raw}"))
                            continue
                        better_stm = next((cp for mv, cp in lines if mv == better_mv), None)
                        if better_stm is None:
                            rejected.append((n, "cloud-eval backup has no score for better move"))
                            continue

            # Optional: if SF succeeded, also peek cloud for disagreement flag (non-fatal)
            if not args.no_lichess and src == "stockfish" and better_mv is not None:
                cloud = cloud_lines(board, 3)
                if cloud:
                    sources_used["cloud-eval"] += 1
                    cloud_best = cloud[0][0]
                    # If cloud best == played and we called it a mistake with a different better, be stricter
                    if cloud_best == played and tag in TAG_MIN_LOSS and better_mv != played:
                        # Re-check loss; if SF loss is small, reject
                        pass  # main loss rules below handle this
                    time.sleep(0.15)  # be gentle on rate limits

            loss = best_stm - played_stm
            reasons = []

            # Opening book context (explorer when reachable, else local ECO)
            bctx = book_context(board, played, parse_move(board, better_raw) if better_raw else None, args.no_lichess)
            if bctx["eco"] or bctx["eco_moves"]:
                sources_used["eco"] += 1
            if bctx["explorer"]:
                sources_used["explorer"] += 1

            if tag == "book":
                if not bctx["played_in_book"]:
                    reasons.append(
                        "tag book but played move not in ECO/explorer book for this position"
                    )
                # Book moves may be slightly suboptimal — allow up to ~80cp vs engine best
                elif loss > 80:
                    reasons.append(
                        f"tag book but played loses {loss}cp vs engine best {best_mv.uci()} [{src}]"
                    )
            elif tag in POSITIVE_TAGS:
                if loss > 50:
                    reasons.append(
                        f"positive tag {tag} but played loses {loss}cp vs best {best_mv.uci()} [{src}]"
                    )
            elif tag in TAG_MIN_LOSS:
                need = TAG_MIN_LOSS[tag]
                if loss < need * 0.6:
                    reasons.append(
                        f"tag {tag} needs ~{need}cp loss, only {loss}cp (best {best_mv.uci()} [{src}])"
                    )
                # Don't call an inaccuracy/mistake if it's a main book move unless engine says disaster
                if bctx["played_in_book"] and loss < 120 and tag in ("inaccuracy", "mistake"):
                    reasons.append(
                        f"tag {tag} on a book move with only {loss}cp loss — prefer book/interesting or leave untagged"
                    )

            # Enrich note text with opening name once (non-destructive)
            eco_name = None
            if bctx.get("eco_after"):
                eco_name = f"{bctx['eco_after'].get('eco')} {bctx['eco_after'].get('name')}"
            elif bctx.get("eco"):
                eco_name = f"{bctx['eco'].get('eco')} {bctx['eco'].get('name')}"
            if eco_name and eco_name not in (n.get("text") or ""):
                n = dict(n)
                n["text"] = (n.get("text") or "").rstrip() + f" [{eco_name}]"

            if better_raw:
                if better_mv is None:
                    better_mv = parse_move(board, better_raw)
                if better_mv is None:
                    reasons.append(f"illegal better={better_raw}")
                elif better_stm is None:
                    reasons.append(f"could not score better={better_raw}")
                else:
                    if better_stm <= played_stm + 20:
                        reasons.append(
                            f"better {better_raw} ({better_stm}cp) not clearly better than "
                            f"{move.get('san')} ({played_stm}cp); best {best_mv.uci()} ({best_stm}cp) [{src}]"
                        )
                    # Auto-correct better toward engine/TB best when suggestion is a big miss
                    if best_stm - better_stm > 80:
                        n = dict(n)
                        n["better"] = board.san(best_mv)
                        n["text"] = (n.get("text") or "").rstrip() + f" [better auto-corrected to {n['better']} via {src}]"

            if reasons:
                rejected.append((n, "; ".join(reasons)))
            else:
                if better_mv and better_stm is not None and best_stm - better_stm <= 30:
                    n = dict(n)
                    n["better"] = board.san(best_mv)
                kept.append(n)
    finally:
        engine.quit()

    for n, why in rejected:
        print(f"REJECT {n.get('at')}: {why}", file=sys.stderr)
    print(f"kept={len(kept)} rejected={len(rejected)} sources={sources_used}", file=sys.stderr)

    out = json.dumps(kept, indent=2)
    if args.out:
        Path(args.out).write_text(out + "\n")
    else:
        print(out)
    return 2 if rejected else 0


if __name__ == "__main__":
    sys.exit(main())
