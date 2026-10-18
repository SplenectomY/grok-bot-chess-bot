#!/usr/bin/env python3
"""Long-lived Stockfish worker for chess-loop-teach.mjs.

2026-10-09: default mode is now the calibrated human-Elo picker ("elo" mode,
target Lichess rating, default 1000). See ELO MODE below. The old Skill-Level
pipeline is kept as "legacy skill" mode (choose/ponder with `skill` and no `elo`).

Protocol: one JSON object per stdin line → one JSON object per stdout line.
Commands: ping, choose, ponder, cache_get, cache_put, quit.

  choose  {fen, skill=6, legal?:[SAN], weAreWhite?, moves?:[SAN], startFen?}
          → {ok, san, alts, score(white-POV cp, full-strength), skillBestSan,
             primarySan, fullBestSan, source: skill|guard|teach, guard, cached,
             events:[str], deviated?}
  ponder  {fen (opponent to move), skill, maxLines=4} → caches our replies
  cache_get / cache_put {fen, skill, san, ...} / ping / quit

Two engines:
  * SKILL engine (Skill Level N): its actual UCI `bestmove` is the Skill-N pick.
    NOTE: the MultiPV `info` lines of a Skill-Level search are the engine's real
    top lines — line 1 is NOT the skill pick. (Pre-2026-10-07 code used
    infos[0] as "skill-best", i.e. it never actually played Skill-6 moves.)
  * FULL engine (Skill Level 20): short full-strength eval used for every
    gate decision. All gate scores are from OUR point of view (side to move).

Move pipeline:
  1. Skill pick = Skill-N bestmove (search runs in parallel with the FULL search).
  2. Sanity guard: if the skill pick drops ≥ GUARD_DROP_CP vs the full-strength
     top move (or SEE says it loses ≥ GUARD_SEE_LOSS pawns of material vs top —
     hanging a piece or not recapturing — and drops ≥ GUARD_SEE_DROP_CP),
     replace it with prob GUARD_REPLACE_P (GUARD_REPLACE_P_MAJOR when the SEE
     loss is a rook or more) by the best full-gated MultiPV alt within
     GUARD_ALT_CP of top; otherwise keep the slip (~1000-player realism).
  3. Teach overlay: FEN-legal thematic candidates (checks, captures, hanging
     Q/R threats, legal Qxd8+, forks/pins). A candidate may override the
     primary only if (full-strength, our POV) it is ≥ primary − TEACH_GATE_CP,
     it does not lose material vs the primary by SEE (≥ TEACH_MATERIAL_TOL
     pawns; exception: it is itself ~the full-strength top move, i.e. a sound
     sac), it does not hang Q/R / allow mate-in-1, it does not itself trip the
     guard, and it is not a junk shuffle.
  4. Cache (FEN+skill, filled by ponder) stores the gate verdict; every cache
     hit is RE-GATED with a fresh full-strength check — a cache never bypasses
     the gate.
  5. Threefold avoid only among full-gated alts.
"""
from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

import chess
import chess.engine

REPO_DIR = Path(__file__).resolve().parent
CHESS_HOME = Path(os.environ.get("CHESS_HOME") or REPO_DIR)
PRESETS_FILE = Path(os.environ.get("CHESS_PRESETS") or (REPO_DIR / "presets" / "elo-profiles.json"))


def _find_stockfish() -> str:
    """STOCKFISH_BIN env > $CHESS_HOME/bin/stockfish (setup.sh) > `stockfish` on PATH."""
    env = os.environ.get("STOCKFISH_BIN")
    if env:
        return env
    for local in (CHESS_HOME / "bin" / "stockfish", REPO_DIR / "bin" / "stockfish"):
        if local.exists():
            return str(local)
    import shutil
    return shutil.which("stockfish") or "stockfish"


SF_BIN = _find_stockfish()

# --- gates (centipawns, our POV, full-strength eval) ---
GATE_CP = 80                # threefold-avoid alts: within this of the chosen move
TEACH_GATE_CP = 50          # teach move may be at most this much worse than primary
TEACH_MATERIAL_TOL = 1      # reject teach if SEE net material ≥ this many pawns worse
SOUND_SAC_CP = 15           # ...unless the teach move is within this of the full top move
GUARD_DROP_CP = 250         # skill pick dropping ≥ this vs full top → guard trigger
GUARD_SEE_DROP_CP = 150     # ...or SEE-hangs a piece ≥3 and drops ≥ this
GUARD_ALT_CP = 80           # guard replacement must be within this of full top
GUARD_SEE_LOSS = 2          # SEE material loss (pawns) vs top that counts as "hangs a piece"
GUARD_REPLACE_P = 0.70      # probability the guard replaces a hanging skill pick
GUARD_MAJOR_LOSS = 5        # SEE loss ≥ this (rook/queen, incl. not recapturing one) ...
GUARD_REPLACE_P_MAJOR = 1.0  # ... is replaced with this probability (set 0.70 to unify)
AHEAD_CP = 100
SHUFFLE_CP = 30

# --- think times (seconds). Skill + full searches run in parallel. ---
THINK_CHOOSE = 0.50         # skill engine (MultiPV MULTIPV) — wall time ~= this
THINK_FULL = 0.50           # full engine MultiPV FULL_MULTIPV (parallel with above)
THINK_EXTRA_BASE = 0.15     # full engine, root_moves=extra candidates
THINK_EXTRA_PER = 0.05
THINK_EXTRA_MAX = 0.40
THINK_CACHE_FULL = 0.30     # re-gate on cache hit
THINK_PONDER = 0.45
MULTIPV = 4
FULL_MULTIPV = 4
FULL_HASH_MB = 64
THREEFOLD_DEVIATE_P = (0.30, 0.70)
MATE_CLAMP = 20_000

PIECE_VAL = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
             chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100}

# cache: "fenkey|skill" -> {san, alts, score, skillBestSan, primarySan, source, guard, events, ...}
CACHE: dict[str, dict] = {}
ENGINE: chess.engine.SimpleEngine | None = None       # Skill-Level engine
FULL_ENGINE: chess.engine.SimpleEngine | None = None  # full strength (Skill 20)
RNG = random.Random(int(os.environ["CHESS_WORKER_SEED"])) if os.environ.get("CHESS_WORKER_SEED") else random.Random()


def fen_key(fen: str) -> str:
    """Normalized position key (no halfmove/fullmove)."""
    parts = fen.strip().split()
    if len(parts) >= 4:
        return " ".join(parts[:4])
    return fen.strip()


def cache_key(fen: str, skill: int) -> str:
    return f"{fen_key(fen)}|{skill}"


def our_score_cp(score: chess.engine.PovScore, we_are_white: bool) -> int:
    pov = score.white() if we_are_white else score.black()
    return int(pov.score(mate_score=100_000))


def ensure_engine(skill: int) -> chess.engine.SimpleEngine:
    global ENGINE
    if ENGINE is None:
        ENGINE = chess.engine.SimpleEngine.popen_uci(SF_BIN)
    ENGINE.configure({"Skill Level": int(skill)})
    return ENGINE


def _popen_retry(tries: int = 3) -> chess.engine.SimpleEngine:
    """Spawn Stockfish; the UCI handshake can exceed the timeout when the shared
    box is overloaded, so retry a few times before giving up."""
    last = None
    for _ in range(tries):
        try:
            return chess.engine.SimpleEngine.popen_uci(SF_BIN, timeout=ENGINE_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def ensure_full_engine() -> chess.engine.SimpleEngine:
    """Separate full-strength instance (Skill Level 20) for all gate evals."""
    global FULL_ENGINE
    if FULL_ENGINE is None:
        FULL_ENGINE = _popen_retry()
        FULL_ENGINE.configure({"Skill Level": 20, "Hash": FULL_HASH_MB})
    return FULL_ENGINE

def white_cp(info: dict) -> int:
    return int(info["score"].white().score(mate_score=100_000))


def piece_safe(board: chess.Board, sq: int, color: bool) -> bool:
    """True if square is not attacked by the opponent, or is adequately defended."""
    attackers = board.attackers(not color, sq)
    if not attackers:
        return True
    defenders = board.attackers(color, sq)
    # Simple: if attacked and undefended → hanging; if more attackers than defenders → unsafe
    return len(defenders) >= len(attackers)


def hangs_major(board: chess.Board, move: chess.Move, we_are_white: bool) -> bool:
    """After move, do we hang our queen/rook, or allow mate in 1?"""
    us = chess.WHITE if we_are_white else chess.BLACK
    them = not us
    board.push(move)
    try:
        # Mate in 1 for opponent?
        if board.is_checkmate():
            return True
        for reply in board.legal_moves:
            board.push(reply)
            try:
                if board.is_checkmate():
                    return True
            finally:
                board.pop()

        # Our queen/rook hanging (attacked, not adequately defended, or capturable with gain)
        for pt in (chess.QUEEN, chess.ROOK):
            for sq in board.pieces(pt, us):
                attackers = board.attackers(them, sq)
                if not attackers:
                    continue
                defenders = board.attackers(us, sq)
                # Undefended or under-defended major → hang
                if len(defenders) < len(attackers):
                    return True
                # Captured by lesser/equal value piece for free-ish: if any attacker
                # is not itself hanging after capture of equal-or-lesser — conservative:
                # if attacked by lower/equal value and not more defenders, treat as hang
                our_val = 9 if pt == chess.QUEEN else 5
                for asq in attackers:
                    ap = board.piece_at(asq)
                    if ap is None:
                        continue
                    aval = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
                            chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 99}[ap.piece_type]
                    if aval <= our_val and len(defenders) <= len(attackers):
                        # SEE-lite: if we can recapture, still risky for gate; only
                        # flag when undefended or attacker is cheaper
                        if len(defenders) == 0 or aval < our_val:
                            return True
        return False
    finally:
        board.pop()


def is_junk_shuffle(board: chess.Board, move: chess.Move, eval_delta_cp: int) -> bool:
    """Rook (or same piece) shuffle: to empty square, no check, tiny eval change, already safe."""
    piece = board.piece_at(move.from_square)
    if piece is None:
        return False
    # Focus on rook shuffles (the catastrophic failure mode); also catch other non-developing shuffles
    if piece.piece_type not in (chess.ROOK, chess.KING, chess.QUEEN, chess.BISHOP, chess.KNIGHT):
        return False
    if board.is_capture(move):
        return False
    # Castling is development, not shuffle
    if board.is_castling(move):
        return False
    # Already safe on from-square?
    if not piece_safe(board, move.from_square, board.turn):
        return False
    board.push(move)
    try:
        gives_check = board.is_check()
    finally:
        board.pop()
    if gives_check:
        return False
    if abs(eval_delta_cp) >= SHUFFLE_CP:
        return False
    # Destination empty (non-capture already) — pure reposition with no progress
    # Extra: for rooks, if moving along back rank between a/h with no file open purpose
    if piece.piece_type == chess.ROOK:
        return True
    # Other pieces: only flag if eval essentially flat and destination not a center develop
    rank = chess.square_rank(move.to_square)
    file_ = chess.square_file(move.to_square)
    # Developing into center ranks 2-5 / files c-f is OK
    if piece.color == chess.WHITE:
        developing = rank >= 2 and 2 <= file_ <= 5
    else:
        developing = rank <= 5 and 2 <= file_ <= 5
    return not developing and abs(eval_delta_cp) < SHUFFLE_CP


def attacks_hanging_major(board: chess.Board, move: chess.Move) -> bool:
    """After move, do we attack an undefended (or under-defended) enemy Q/R?"""
    us = board.turn
    them = not us
    board.push(move)
    try:
        for pt in (chess.QUEEN, chess.ROOK):
            for sq in board.pieces(pt, them):
                if sq in board.attacks(move.to_square) or board.is_attacked_by(us, sq):
                    attackers = board.attackers(us, sq)
                    defenders = board.attackers(them, sq)
                    if attackers and len(defenders) < len(attackers):
                        return True
                    if attackers and len(defenders) == 0:
                        return True
        return False
    finally:
        board.pop()


def is_simple_fork(board: chess.Board, move: chess.Move) -> bool:
    """Knight/pawn/queen fork: after move, attack 2+ of {K, Q, R} (or K+major)."""
    piece = board.piece_at(move.from_square)
    if piece is None:
        return False
    if piece.piece_type not in (chess.KNIGHT, chess.PAWN, chess.QUEEN, chess.BISHOP):
        return False
    us = board.turn
    them = not us
    board.push(move)
    try:
        attacked = board.attacks(move.to_square)
        majors = 0.0
        hits_king = False
        for sq in attacked:
            p = board.piece_at(sq)
            if p is None or p.color != them:
                continue
            if p.piece_type == chess.KING:
                hits_king = True
            elif p.piece_type in (chess.QUEEN, chess.ROOK):
                majors += 1.0
            elif p.piece_type in (chess.BISHOP, chess.KNIGHT):
                majors += 0.5
        # Two majors, or king+major
        if hits_king and majors >= 1.0:
            return True
        if majors >= 2.0:
            return True
        return False
    finally:
        board.pop()


def is_simple_pin_or_skewer_motif(board: chess.Board, move: chess.Move) -> bool:
    """Rough pin: sliding piece move that lines up through enemy toward K/Q/R."""
    piece = board.piece_at(move.from_square)
    if piece is None or piece.piece_type not in (chess.BISHOP, chess.ROOK, chess.QUEEN):
        return False
    us = board.turn
    them = not us
    board.push(move)
    try:
        # Look along rays from to_square
        directions = []
        if piece.piece_type in (chess.ROOK, chess.QUEEN):
            directions += [8, -8, 1, -1]
        if piece.piece_type in (chess.BISHOP, chess.QUEEN):
            directions += [9, 7, -9, -7]
        for d in directions:
            sq = move.to_square
            first = None
            second = None
            for _ in range(7):
                prev_file = chess.square_file(sq)
                sq2 = sq + d
                if sq2 < 0 or sq2 > 63:
                    break
                # Wrap guard for horizontal
                if abs(d) in (1, 7, 9) and abs(chess.square_file(sq2) - prev_file) > 1:
                    break
                sq = sq2
                p = board.piece_at(sq)
                if p is None:
                    continue
                if first is None:
                    if p.color == them:
                        first = p.piece_type
                    else:
                        break
                else:
                    if p.color == them:
                        second = p.piece_type
                    break
            if first and second:
                # Pin/skewer if second is king/queen/rook and first is lesser or equal
                if second in (chess.KING, chess.QUEEN, chess.ROOK):
                    return True
        return False
    finally:
        board.pop()


def nominate_teach(board: chess.Board, legal_sans: list[str] | None) -> list[tuple[str, int]]:
    """FEN-legal thematic candidates only — no global SAN wishlist.

    Returns list of (san, theme_score). Theme scores: checks/captures/hanging
    majors/forks/pins are strong; mere development/castling is weak and must
    not override skill-best (skill-best is the default).
    """
    legal_moves = list(board.legal_moves)
    san_of = {m: board.san(m) for m in legal_moves}
    if legal_sans is not None:
        allowed = set(legal_sans)
        legal_moves = [m for m in legal_moves if san_of[m] in allowed]

    scored: list[tuple[int, str]] = []
    for m in legal_moves:
        san = san_of[m]
        theme = 0
        board.push(m)
        gives_check = board.is_check()
        board.pop()
        if gives_check:
            theme += 5
        if board.is_capture(m):
            captured = board.piece_at(m.to_square)
            if captured is None and board.is_en_passant(m):
                theme += 2
            elif captured:
                vals = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
                        chess.ROOK: 4, chess.QUEEN: 6, chess.KING: 0}
                theme += vals.get(captured.piece_type, 1)
        if attacks_hanging_major(board, m):
            theme += 6
        # Open-file queen trade / back-rank only if that SAN is legal here
        if san in ("Qxd8+", "Qxd8", "Qxd1+", "Qxd1"):
            theme += 4
        if is_simple_fork(board, m):
            theme += 5
        if is_simple_pin_or_skewer_motif(board, m):
            theme += 3
        # Weak themes — recorded but must not beat skill-best alone
        piece = board.piece_at(m.from_square)
        if piece and piece.piece_type in (chess.KNIGHT, chess.BISHOP) and not board.is_capture(m):
            fr = chess.square_rank(m.from_square)
            if (piece.color == chess.WHITE and fr == 0) or (piece.color == chess.BLACK and fr == 7):
                theme += 1
        if board.is_castling(m):
            theme += 2

        if theme > 0:
            scored.append((theme, san))

    scored.sort(key=lambda x: (-x[0], x[1]))
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for theme, san in scored:
        if san in seen:
            continue
        seen.add(san)
        out.append((san, theme))
        if len(out) >= 4:
            break
    return out


# ---------------------------------------------------------------------------
# Static exchange / material check (our POV, in pawns)
# ---------------------------------------------------------------------------

def _captured_value(board: chess.Board, m: chess.Move) -> int:
    v = 0
    if board.is_en_passant(m):
        v = 1
    else:
        p = board.piece_at(m.to_square)
        if p is not None:
            v = PIECE_VAL[p.piece_type]
    if m.promotion:
        v += PIECE_VAL[m.promotion] - 1
    return v


def _swap_on(board: chess.Board, sq: int, depth: int = 0) -> int:
    """Side to move recaptures on sq with least valuable legal attacker; ≥0 gain."""
    if depth > 12:
        return 0
    caps = list(board.generate_legal_captures(to_mask=chess.BB_SQUARES[sq]))
    if not caps:
        return 0
    m = min(caps, key=lambda c: PIECE_VAL[board.piece_type_at(c.from_square) or chess.PAWN])
    gain = _captured_value(board, m)
    board.push(m)
    try:
        r = _swap_on(board, sq, depth + 1)
    finally:
        board.pop()
    return max(0, gain - r)


def _see_capture(board: chess.Board, m: chess.Move) -> int:
    gain = _captured_value(board, m)
    board.push(m)
    try:
        r = _swap_on(board, m.to_square)
    finally:
        board.pop()
    return gain - r


def material_net(board: chess.Board, move: chess.Move) -> int:
    """Pawns we net from `move` = what it captures − opponent's best SEE capture reply.

    Catches: hanging a piece (Bc5 into dxc5), capturing into a losing exchange
    (Nxc2 Rxc2), and not recapturing (Rd8/Kc7 when Rxc8/Kxd8 was available —
    the reference move nets +5, the candidate ~0).
    """
    gain = _captured_value(board, move)
    board.push(move)
    try:
        threat = 0
        for c in board.generate_legal_captures():
            v = _see_capture(board, c)
            if v > threat:
                threat = v
    finally:
        board.pop()
    return gain - threat


# ---------------------------------------------------------------------------
# Full-strength evaluation helper
# ---------------------------------------------------------------------------

def _our_cp(info: dict, us: bool) -> int:
    cp = int(info["score"].pov(us).score(mate_score=100_000))
    return max(-MATE_CLAMP * 5, min(MATE_CLAMP * 5, cp))


class FullEval:
    """Full-strength (Skill 20) scores per root move, OUR point of view."""

    def __init__(self, board: chess.Board, infos):
        self.board = board
        self.us = board.turn
        self.cp: dict[chess.Move, int] = {}
        self.order: list[chess.Move] = []
        self._net: dict[chess.Move, int] = {}
        if not isinstance(infos, list):
            infos = [infos]
        for info in infos:
            pv = info.get("pv") or []
            if not pv or "score" not in info:
                continue
            mv = pv[0]
            if mv not in self.cp:
                self.cp[mv] = _our_cp(info, self.us)
                self.order.append(mv)
        if not self.order:
            raise RuntimeError("full eval returned no lines")
        self.top = max(self.order, key=lambda m: self.cp[m])
        self.top_cp = self.cp[self.top]

    def ensure(self, moves) -> None:
        missing = []
        for m in moves:
            if m is not None and m not in self.cp and m not in missing and m in self.board.legal_moves:
                missing.append(m)
        if not missing:
            return
        t = min(THINK_EXTRA_MAX, THINK_EXTRA_BASE + THINK_EXTRA_PER * len(missing))
        infos = ensure_full_engine().analyse(
            self.board, chess.engine.Limit(time=t), multipv=len(missing), root_moves=missing)
        if not isinstance(infos, list):
            infos = [infos]
        for info in infos:
            pv = info.get("pv") or []
            if pv and "score" in info and pv[0] not in self.cp:
                self.cp[pv[0]] = _our_cp(info, self.us)
        # A restricted search can occasionally beat the open MultiPV top
        for m in missing:
            if m in self.cp and self.cp[m] > self.top_cp:
                self.top, self.top_cp = m, self.cp[m]

    def net(self, m: chess.Move) -> int:
        if m not in self._net:
            self._net[m] = material_net(self.board, m)
        return self._net[m]


def full_eval(board: chess.Board, t: float, multipv: int) -> FullEval:
    infos = ensure_full_engine().analyse(board, chess.engine.Limit(time=t), multipv=multipv)
    return FullEval(board, infos)


def guard_trigger(fe: FullEval, mv: chess.Move) -> tuple[bool, str, float]:
    """Does this move hang material outright vs the full-strength top move?

    Returns (triggered, reason, replace_probability). Material loss is SEE
    relative to the top move, so "fails to recapture a rook" counts the same
    as "hangs a rook".
    """
    if mv == fe.top:
        return False, "", 0.0
    drop = fe.top_cp - fe.cp.get(mv, -MATE_CLAMP)
    net_mv, net_top = fe.net(mv), fe.net(fe.top)
    loss = net_top - net_mv
    p = GUARD_REPLACE_P_MAJOR if loss >= GUARD_MAJOR_LOSS else GUARD_REPLACE_P
    top_san = fe.board.san(fe.top)
    if drop >= GUARD_DROP_CP:
        return True, f"drop {drop}cp vs top {top_san} (SEE {net_mv:+d} vs {net_top:+d})", p
    if loss >= GUARD_SEE_LOSS and drop >= GUARD_SEE_DROP_CP:
        return True, f"SEE loses {loss} pawns vs top {top_san} ({net_mv:+d} vs {net_top:+d}), drop {drop}cp", p
    return False, "", 0.0


def teach_gate(fe: FullEval, cand: chess.Move, ref: chess.Move, we_are_white: bool) -> str | None:
    """Return None if the teach candidate may replace `ref`, else a rejection reason."""
    b = fe.board
    cp_c = fe.cp.get(cand)
    cp_r = fe.cp.get(ref)
    if cp_c is None or cp_r is None:
        return "no full-strength eval"
    if cp_c < cp_r - TEACH_GATE_CP:
        return f"eval {cp_c - cp_r:+d}cp vs {b.san(ref)} (gate -{TEACH_GATE_CP})"
    sound_sac = cp_c >= fe.top_cp - SOUND_SAC_CP
    net_c, net_r = fe.net(cand), fe.net(ref)
    if net_r - net_c >= TEACH_MATERIAL_TOL and not sound_sac:
        return f"material SEE {net_c:+d} vs {b.san(ref)} {net_r:+d}"
    if hangs_major(b, cand, we_are_white) and not sound_sac:
        return "hangs Q/R or allows mate-in-1"
    trig, why, _ = guard_trigger(fe, cand)
    if trig:
        return f"trips guard ({why})"
    if is_junk_shuffle(b, cand, cp_c - cp_r):
        return "junk shuffle"
    return None


def _white_cp(fe: FullEval, mv: chess.Move) -> int:
    cp = fe.cp.get(mv, fe.top_cp)
    return cp if fe.us == chess.WHITE else -cp


# ---------------------------------------------------------------------------
# Choose
# ---------------------------------------------------------------------------

def analyse_choose(board: chess.Board, skill: int, legal_sans: list[str] | None) -> dict:
    events: list[str] = []
    we_are_white = board.turn == chess.WHITE
    skill_eng = ensure_engine(skill)
    ensure_full_engine()
    legal_ok = set(legal_sans) if legal_sans else None

    def allowed(m: chess.Move) -> bool:
        return m in board.legal_moves and (legal_ok is None or board.san(m) in legal_ok)

    # 1. Skill search (async) || full-strength MultiPV search.
    with skill_eng.analysis(board, chess.engine.Limit(time=THINK_CHOOSE), multipv=MULTIPV) as an:
        fe = full_eval(board, THINK_FULL, FULL_MULTIPV)
        bm = an.wait()
        skill_infos = list(an.multipv)
    skill_pick = bm.move if bm and bm.move else None
    if skill_pick is None or not allowed(skill_pick):
        skill_pick = fe.top
    skill_lines = [i["pv"][0] for i in skill_infos if i.get("pv")]

    teach = nominate_teach(board, legal_sans)
    teach_moves = []
    for san, theme in teach:
        try:
            teach_moves.append((board.parse_san(san), san, theme))
        except Exception:
            continue
    fe.ensure([skill_pick, *skill_lines, *[m for m, _, _ in teach_moves]])

    skill_san = board.san(skill_pick)
    top_san = board.san(fe.top)

    # 2. Sanity guard on the skill pick itself.
    primary = skill_pick
    source = "skill"
    guard = "none"
    trig, why, p_replace = guard_trigger(fe, skill_pick)
    if trig:
        roll = RNG.random()
        if roll < p_replace:
            pool = []
            for m in dict.fromkeys([*skill_lines, *fe.order]):
                if m == skill_pick or m not in fe.cp or not allowed(m):
                    continue
                if fe.cp[m] < fe.top_cp - GUARD_ALT_CP:
                    continue
                if fe.net(fe.top) - fe.net(m) >= GUARD_SEE_LOSS:
                    continue
                pool.append(m)
            repl = max(pool, key=lambda m: fe.cp[m]) if pool else (fe.top if allowed(fe.top) else None)
            if repl is not None and repl != skill_pick:
                primary, source, guard = repl, "guard", "override"
                events.append(f"guard-override: skill {skill_san} {why} → {board.san(repl)} "
                              f"({fe.cp[repl] - fe.cp[skill_pick]:+d}cp; roll {roll:.2f}<{p_replace})")
        if guard == "none":
            guard = "kept"
            events.append(f"guard-kept-slip: skill {skill_san} {why} (roll {roll:.2f}≥{p_replace})")
    primary_cp = fe.cp[primary]

    # 3. Teach overlay — only strong themes, full-strength gated vs primary.
    STRONG_THEME = 3
    chosen = primary
    for mv, san, theme in teach_moves:
        if theme < STRONG_THEME or mv == primary or not allowed(mv):
            continue
        reason = teach_gate(fe, mv, primary, we_are_white)
        if reason:
            events.append(f"teach-reject {san}: {reason}")
            continue
        chosen = mv
        source = "teach"
        events.append(f"teach-override: {san} (theme {theme}, {fe.cp[mv] - primary_cp:+d}cp, "
                      f"SEE {fe.net(mv):+d} vs {fe.net(primary):+d}) over {board.san(primary)}")
        break

    # Gated alts (threefold-avoid pool): full-strength, our POV, vs chosen.
    chosen_cp = fe.cp[chosen]
    alts = [board.san(chosen)]
    for m in sorted(fe.cp, key=lambda x: -fe.cp[x]):
        if m == chosen or not allowed(m):
            continue
        if fe.cp[m] < chosen_cp - GATE_CP:
            continue
        if fe.net(chosen) - fe.net(m) >= TEACH_MATERIAL_TOL and fe.cp[m] < fe.top_cp - SOUND_SAC_CP:
            continue
        if hangs_major(board, m, we_are_white) and m != fe.top:
            continue
        if guard_trigger(fe, m)[0]:
            continue
        alts.append(board.san(m))

    return {
        "san": board.san(chosen),
        "alts": alts,
        "score": _white_cp(fe, chosen),
        "skillBestSan": skill_san,
        "primarySan": board.san(primary),
        "fullBestSan": top_san,
        "bestScore": _white_cp(fe, fe.top),
        "source": source,
        "guard": guard,
        "events": events,
        "gated": True,
        "gate": {
            "chosenCp": chosen_cp, "primaryCp": primary_cp, "topCp": fe.top_cp,
            "skillCp": fe.cp[skill_pick],
            "chosenSEE": fe.net(chosen), "primarySEE": fe.net(primary),
        },
    }


# ===========================================================================
# ELO MODE (default since 2026-10-09): calibrated human-like picker
# ===========================================================================
# Target = Lichess rating (Rapid/Classical scale of tools/calibration/calib_table.txt).
# Per move:
#  1. DEEP view  = judge engine (full strength) MultiPV over ALL legal moves,
#                  Limit(depth=ELO_DEEP_DEPTH, time=ELO_DEEP_TIME).
#  2. SHALLOW view = separate engine, fresh hash every call, MultiPV ALL at
#                  depth ELO_SHALLOW_DEPTH (sees one-move hangs/recaptures,
#                  misses 2+-move tactics) -> source of "human" missed tactics.
#  3. loss(view, m) = max(Δwin%, KAPPA·Δcp/100) vs that view's best move
#     (win% = Lichess formula on cp clamped ±1000; cp clamped ±2100 with mates
#     scored 2100-n so throwing away a mate still costs something).
#     perceived(m) = ALPHA·loss_shallow + (1−ALPHA)·loss_deep − teach_bonus(m)
#  4. Candidates = top-K deep ∪ top-K shallow ∪ all captures ∪ all checks,
#     minus junk shuffles (unless deep top-2). Forcing moves are what humans look at.
#  5. On a "blind" turn (prob P_BLIND) one candidate's opponent reply is
#     ignored (perceived = material-only 1-ply view) -> occasional hung piece.
#  6. Sample P(m) ∝ exp(−perceived/T).
#  7. Guard (loosened): queen for nothing (SEE ≥ 8 pawns worse than top AND
#     ≥ 500cp drop) is re-drawn with prob GUARD_QUEEN_P; allowing mate-in-1 is
#     re-drawn with prob GUARD_MATE1_P. Everything else (hanging minors/rooks,
#     missed tactics) is allowed at the model's natural rate.
#  8. Won-position rules (deep top ≥ WIN_CP, our POV): never play a move that
#     repeats an earlier position (even a 2nd time) if any non-repeating move
#     scoring ≥ 0 exists; after two consecutive checks, further checks must be
#     ~best (no endless checking that throws away the win).
import math
import re

# Built-in fallback presets. The canonical copy lives in presets/elo-profiles.json
# (loaded below, overrides these). See CALIBRATION.md.
ELO_PROFILES: dict[int, dict] = {
    # FIELD-TEST 2026-10-10: sub-1000 targets interpolate toward this anchor (below 800: clamp to 800)
    800: dict(T=5.8, ALPHA=0.5, KAPPA=3.0, K=6, P_BLIND=0.40, BLIND_TEMPT=12.0, BLIND_MIN_LOSS=20.0, TEACH_BONUS=4.0,
              TEACH_MAX_LOSS=6.0, GUARD_QUEEN_P=0.85, GUARD_MATE1_P=0.6,
              GUARD_HANG_OBV_P=0.0, GUARD_HANG_SUB_P=0.0, GUARD_HANG_DROP=250.0,
              GUARD_MATE_N=1.0, GUARD_MAT_ON=0.0, BUDGET_ON=0.0, B1_OBV=0.0, B1_SUB=0.0, B1_QUEEN=0.0, B1_MATE=0.0, B1_BLIND=1.0, B2_OBV=0.0, B2_SUB=0.0, B2_QUEEN=0.0, B2_MATE=0.0, B2_BLIND=1.0, GUARD_WC_P=0.0, B1_WC=0.0, B2_WC=0.0),
    # CALIBRATED 2026-10-09 by self-play, see CALIBRATION.md
    1000: dict(T=5.2, ALPHA=0.5, KAPPA=3.0, K=6, P_BLIND=0.36, BLIND_TEMPT=12.0, BLIND_MIN_LOSS=20.0, TEACH_BONUS=4.0,
               TEACH_MAX_LOSS=6.0, GUARD_QUEEN_P=0.85, GUARD_MATE1_P=0.6,
               GUARD_HANG_OBV_P=0.0, GUARD_HANG_SUB_P=0.0, GUARD_HANG_DROP=250.0,
              GUARD_MATE_N=1.0, GUARD_MAT_ON=0.0, BUDGET_ON=0.0, B1_OBV=0.0, B1_SUB=0.0, B1_QUEEN=0.0, B1_MATE=0.0, B1_BLIND=1.0, B2_OBV=0.0, B2_SUB=0.0, B2_QUEEN=0.0, B2_MATE=0.0, B2_BLIND=1.0, GUARD_WC_P=0.0, B1_WC=0.0, B2_WC=0.0),
    # FIELD-TEST 2026-10-10: self-play calibrated vs Lichess Rapid/Classical (see CALIBRATION.md)
    1200: dict(T=4.6, ALPHA=0.5, KAPPA=3.0, K=6, P_BLIND=0.24, BLIND_TEMPT=12.0, BLIND_MIN_LOSS=20.0, TEACH_BONUS=4.0,
               TEACH_MAX_LOSS=5.5, GUARD_QUEEN_P=0.875, GUARD_MATE1_P=0.65,
               GUARD_HANG_OBV_P=0.55, GUARD_HANG_SUB_P=0.3, GUARD_HANG_DROP=200.0,
              GUARD_MATE_N=2.0, GUARD_MAT_ON=1.0, BUDGET_ON=1.0, B1_OBV=0.85, B1_SUB=0.55, B1_QUEEN=0.95, B1_MATE=0.9, B1_BLIND=0.5, B2_OBV=0.95, B2_SUB=0.8, B2_QUEEN=0.99, B2_MATE=0.97, B2_BLIND=0.0, GUARD_WC_P=0.0, B1_WC=0.4, B2_WC=0.7),
    # 1400 retuned 2026-10-12 (W46b10m12g) + hanging-piece guard 2026-10-15 (T 5.7, see CALIBRATION.md)
    # 2026-10-18: per-game blunder budget + material/mate-in-2/win-chance guard; T 5.7->7.5 keeps self-play ACPL (51.1 vs 52.0)
    1400: dict(T=7.5, ALPHA=0.5, KAPPA=3.0, K=6, P_BLIND=0.10, BLIND_TEMPT=10.0, BLIND_MIN_LOSS=12.0, TEACH_BONUS=4.0,
               TEACH_MAX_LOSS=5.0, GUARD_QUEEN_P=0.97, GUARD_MATE1_P=0.85,
               GUARD_HANG_OBV_P=0.85, GUARD_HANG_SUB_P=0.55, GUARD_HANG_DROP=150.0,
              GUARD_MATE_N=2.0, GUARD_MAT_ON=1.0, BUDGET_ON=1.0, B1_OBV=0.93, B1_SUB=0.65, B1_QUEEN=0.99, B1_MATE=0.95, B1_BLIND=0.7, B2_OBV=0.97, B2_SUB=0.8, B2_QUEEN=1.0, B2_MATE=1.0, B2_BLIND=0.4, GUARD_WC_P=0.0, B1_WC=0.6, B2_WC=0.85),
}
ELO_CALIBRATED = {1000}   # fully calibrated; 800/1200/1400 are field-test (self-play matched, awaiting human games)
PRESETS_SOURCE = "built-in"


def _load_presets() -> None:
    """Load presets/elo-profiles.json; on any problem keep the built-in table."""
    global ELO_PROFILES, ELO_CALIBRATED, PRESETS_SOURCE
    try:
        data = json.loads(PRESETS_FILE.read_text())
        profiles, calibrated = {}, set()
        for key, entry in data["profiles"].items():
            knobs = {k: float(v) for k, v in entry["knobs"].items()}
            knobs["K"] = int(knobs["K"])
            profiles[int(key)] = knobs
            if entry.get("calibrated") or entry.get("status") == "calibrated":
                calibrated.add(int(key))
        if profiles:
            ELO_PROFILES, ELO_CALIBRATED = profiles, calibrated
            PRESETS_SOURCE = str(PRESETS_FILE)
    except FileNotFoundError:
        pass
    except Exception as e:  # malformed file: warn, fall back
        print(f"presets load failed ({PRESETS_FILE}): {e}; using built-in", file=sys.stderr)


_load_presets()
ELO_DEFAULT = 1000
ELO_DEEP_DEPTH = 12
ELO_DEEP_TIME = 0.55
ELO_SHALLOW_DEPTH = 2
ELO_PONDER_TIME = 0.30
ENGINE_TIMEOUT = 8.0      # python-chess sync wrapper timeout; on timeout respawn+retry fits the loop's 25s request budget
BLIND_MIN_LOSS = 20.0    # win%-pts a move must lose to be a blind-spot candidate (Lichess blunder ≈ 15)
BLIND_MAX_EVAL = 10
BLIND_DEPTH = 6
WIN_CP = 300               # "clearly winning" (judge, our POV)
CHECK_SPAM_TOL = 2.0       # perceived-loss tolerance for a 3rd consecutive check when winning
CP_CLAMP_ELO = 2100
SHALLOW_ENGINE: chess.engine.SimpleEngine | None = None
_ELO_OVERRIDES = json.loads(os.environ.get("CHESS_ELO_OVERRIDES", "{}") or "{}")


def elo_profile(elo: int) -> dict:
    """Linear interpolation between calibrated anchors; clamped outside. Env
    CHESS_ELO_OVERRIDES='{"T":7}' overrides any knob (used for tuning)."""
    keys = sorted(ELO_PROFILES)
    if elo <= keys[0]:
        p = dict(ELO_PROFILES[keys[0]])
    elif elo >= keys[-1]:
        p = dict(ELO_PROFILES[keys[-1]])
    else:
        lo = max(k for k in keys if k <= elo)
        hi = min(k for k in keys if k >= elo)
        if lo == hi:
            p = dict(ELO_PROFILES[lo])
        else:
            f = (elo - lo) / (hi - lo)
            p = {k: (ELO_PROFILES[lo][k] * (1 - f) + ELO_PROFILES[hi][k] * f) for k in ELO_PROFILES[lo]}
            p["K"] = int(round(p["K"]))
    p.update(_ELO_OVERRIDES)
    p["elo"] = elo
    return p


def ensure_shallow_engine() -> chess.engine.SimpleEngine:
    global SHALLOW_ENGINE
    if SHALLOW_ENGINE is None:
        SHALLOW_ENGINE = _popen_retry()
        SHALLOW_ENGINE.configure({"Skill Level": 20, "Hash": 1})
    return SHALLOW_ENGINE


def _wp(cp: int) -> float:
    c = max(-1000, min(1000, cp))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * c)) - 1)


def _cp_mate(info: dict, us: bool) -> int:
    s = info["score"].pov(us)
    v = s.score(mate_score=CP_CLAMP_ELO)
    return max(-CP_CLAMP_ELO, min(CP_CLAMP_ELO, int(v)))


def _all_lines(eng, board, limit, moves, new_game=False) -> dict:
    infos = eng.analyse(board, limit, multipv=max(1, len(moves)), root_moves=moves,
                        game=object() if new_game else None)
    if not isinstance(infos, list):
        infos = [infos]
    out = {}
    for info in infos:
        pv = info.get("pv") or []
        if pv and "score" in info and pv[0] not in out:
            out[pv[0]] = info
    return out


def _losses(cps: dict, kappa: float) -> dict:
    best = max(cps.values())
    wb = _wp(best)
    return {m: max(wb - _wp(c), kappa * (best - c) / 100.0) for m, c in cps.items()}


def _blind_material(board: chess.Board, m: chess.Move) -> int:
    """1-ply 'didn't see the reply' view: just what the move captures (pawns)."""
    return _captured_value(board, m)


def _our_recent_checks(moves: list[str] | None) -> int:
    """How many of OUR immediately preceding moves gave check (we are to move)."""
    if not moves:
        return 0
    n = 0
    i = len(moves) - 2
    while i >= 0 and (moves[i].endswith("+") or moves[i].endswith("#")):
        n += 1
        i -= 2
    return n


GUARD_HANG_DROP = 250      # hanging-piece guard: chosen move must lose >= this vs top (deep cp)
GUARD_HANG_MAT = 2         # ...and >= this many pawns of material (SEE net or deep-PV swing)


def _pv_material_swing(board: chess.Board, pv: list, plies: int = 4) -> int:
    """Material (pawns, our POV) lost along the first `plies` moves of a PV."""
    b = board.copy(stack=False)
    us = b.turn
    def mat(x):
        return sum(PIECE_VAL.get(p.piece_type, 0) * (1 if p.color == us else -1) for p in x.piece_map().values())
    m0 = mat(b)
    for mv in pv[:plies]:
        if mv not in b.legal_moves:
            break
        b.push(mv)
    return m0 - mat(b)


def hang_class(board, chosen, top, deep, deep_infos, min_drop=GUARD_HANG_DROP):
    """None | 'obvious' (piece en prise / attacked piece ignored, SEE-visible)
    | 'subtle' (material lost to a short forcing line: forks, 2-move tactics)."""
    drop = deep[top] - deep[chosen]
    if drop < min_drop or chosen == top:
        return None
    if material_net(board, top) - material_net(board, chosen) >= GUARD_HANG_MAT:
        return "obvious"
    pv_c = [chosen] + list(deep_infos[chosen].get("pv") or [])[1:]
    pv_t = [top] + list(deep_infos[top].get("pv") or [])[1:]
    if _pv_material_swing(board, pv_c, 5) - _pv_material_swing(board, pv_t, 5) >= GUARD_HANG_MAT + 1:
        return "subtle"
    return None


GUARD_MAT_LOSS = 3         # material rule: >= a minor piece (pawns) worse than top ...
GUARD_MAT_MIN_DROP = 40    # ... and raw (unclamped, mate=3000) eval drop >= this when the game is already decided
GUARD_MAT_DECIDED = 300    # |top raw eval| >= this = decided (evals saturate: a lost rook may cost only ~100cp)
GUARD_MAT_MIN_DROP_OPEN = 100  # raw drop needed in undecided positions (below this the engine sees compensation)
RAW_CAP = 3000


def _raw_cp(info: dict, us: bool) -> int:
    v = info["score"].pov(us).score(mate_score=RAW_CAP)
    return max(-RAW_CAP, min(RAW_CAP, int(v)))


def guard_kind(board, chosen, top, deep, raw, deep_infos, mate_of, P):
    """Classify a candidate for the guard: None | 'queen' | 'mate' | 'obvious' | 'subtle'.
    cp-drop tests use the clamped deep view; the material rule (GUARD_MAT_ON) is
    eval-clamp independent, so it still fires in already-lost/won positions."""
    if chosen == top:
        return None, ""
    net_c, net_t = material_net(board, chosen), material_net(board, top)
    drop = deep[top] - deep[chosen]
    raw_drop = raw[top] - raw[chosen]
    mat_on = P.get("GUARD_MAT_ON", 0) >= 0.5
    if net_t - net_c >= 8 and (max(drop, raw_drop) if mat_on else drop) >= 500:
        return "queen", "queen for nothing"
    n_mate = max(1, int(round(P.get("GUARD_MATE_N", 1))))
    mc, mt = mate_of.get(chosen), mate_of.get(top)
    if mc is not None and mc < 0 and abs(mc) <= n_mate and not (mt is not None and mt < 0):
        return "mate", f"allows mate-in-{abs(mc)}"
    hc = hang_class(board, chosen, top, deep, deep_infos, P.get("GUARD_HANG_DROP", GUARD_HANG_DROP))
    if hc:
        return hc, f"hangs material ({hc})"
    need = GUARD_MAT_MIN_DROP if abs(raw[top]) >= GUARD_MAT_DECIDED else GUARD_MAT_MIN_DROP_OPEN
    if mat_on and raw_drop >= need:
        if net_t - net_c >= GUARD_MAT_LOSS:
            return "obvious", f"hangs material (obvious, material {net_t - net_c}p, raw drop {raw_drop}cp)"
        pv_c = [chosen] + list(deep_infos[chosen].get("pv") or [])[1:]
        pv_t = [top] + list(deep_infos[top].get("pv") or [])[1:]
        sw = _pv_material_swing(board, pv_c, 5) - _pv_material_swing(board, pv_t, 5)
        if sw >= GUARD_MAT_LOSS:
            return "subtle", f"hangs material (subtle, PV material {sw}p, raw drop {raw_drop}cp)"
    return None, ""


# ---- per-game blunder budget --------------------------------------------
# A bot move that reaches the board while flagged (kept guard case, or a
# Lichess-scale blunder: >= BUDGET_WP_BLUNDER win%-points) is recorded as a
# (position, SAN) pair. The count for a game = recorded pairs that occur among
# OUR moves in that game's move list, so it survives worker/loop restarts
# (pairs persisted per gameId under BUDGET_DIR when the loop sends gameId).
BUDGET_WP_BLUNDER = 15.0
BUDGET_DIR = Path(os.environ.get("CHESS_BUDGET_DIR") or (Path(__file__).resolve().parent / ".budget"))
_BUDGET_MEM: dict[str, set] = {}
_BUDGET_HINT = {"n": 0}


def _budget_set(game_id: str | None) -> set:
    gid = game_id or "_anon"
    if gid not in _BUDGET_MEM:
        st = set()
        if game_id:
            try:
                st = set(json.loads((BUDGET_DIR / (re.sub(r"[^A-Za-z0-9_.-]", "_", gid) + ".json")).read_text()))
            except Exception:
                st = set()
        _BUDGET_MEM[gid] = st
    return _BUDGET_MEM[gid]


def budget_record(game_id: str | None, fen: str, san: str) -> None:
    st = _budget_set(game_id)
    st.add(f"{fen_key(fen)}|{san}")
    if game_id:
        try:
            BUDGET_DIR.mkdir(parents=True, exist_ok=True)
            f = BUDGET_DIR / (re.sub(r"[^A-Za-z0-9_.-]", "_", game_id) + ".json")
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(sorted(st)))
            tmp.replace(f)
        except Exception as e:
            print(f"budget persist failed: {e}", file=sys.stderr)


def budget_count(game_id: str | None, start_fen: str | None, moves: list[str] | None, us: bool) -> int:
    if not moves:
        return 0
    st = _budget_set(game_id)
    if not st:
        return 0
    b = chess.Board(start_fen) if start_fen else chess.Board()
    n = 0
    for san in moves:
        try:
            if b.turn == us and f"{fen_key(b.fen())}|{san}" in st:
                n += 1
            b.push_san(san)
        except Exception:
            break
    return n


def apply_budget(P: dict, n: int) -> dict:
    if n <= 0 or P.get("BUDGET_ON", 0) < 0.5:
        return P
    q = dict(P)
    pre = "B2_" if n >= 2 else "B1_"
    q["GUARD_HANG_OBV_P"] = max(P.get("GUARD_HANG_OBV_P", 0.0), P.get(pre + "OBV", 0.0))
    q["GUARD_HANG_SUB_P"] = max(P.get("GUARD_HANG_SUB_P", 0.0), P.get(pre + "SUB", 0.0))
    q["GUARD_QUEEN_P"] = max(P["GUARD_QUEEN_P"], P.get(pre + "QUEEN", 0.0))
    q["GUARD_MATE1_P"] = max(P["GUARD_MATE1_P"], P.get(pre + "MATE", 0.0))
    q["P_BLIND"] = P["P_BLIND"] * P.get(pre + "BLIND", 1.0)
    q["GUARD_WC_P"] = max(P.get("GUARD_WC_P", 0.0), P.get(pre + "WC", 0.0))
    q["budget_n"] = n
    return q


def elo_choose(board: chess.Board, elo: int, legal_sans: list[str] | None,
               hist_keys: list[str] | None = None, moves: list[str] | None = None,
               budget_n: int = 0) -> dict:
    P = apply_budget(elo_profile(elo), budget_n)
    us = board.turn
    we_are_white = us == chess.WHITE
    events: list[str] = []
    legal = list(board.legal_moves)
    if legal_sans:
        ok = set(legal_sans)
        legal = [m for m in legal if board.san(m) in ok] or list(board.legal_moves)
    san = {m: board.san(m) for m in legal}

    deep_infos = _all_lines(ensure_full_engine(), board,
                            chess.engine.Limit(depth=ELO_DEEP_DEPTH, time=ELO_DEEP_TIME), legal)
    for m in legal:  # extremely rare: a root move missing from MultiPV output
        if m not in deep_infos:
            deep_infos.update(_all_lines(ensure_full_engine(), board, chess.engine.Limit(time=0.1), [m]))
    deep = {m: _cp_mate(deep_infos[m], us) for m in legal if m in deep_infos}
    mate_of = {m: deep_infos[m]["score"].pov(us).mate() for m in deep}
    legal = [m for m in legal if m in deep]
    if len(legal) == 1:
        m = legal[0]
        return _elo_result(board, m, deep, {m: 0.0}, "forced", events, [san[m]], P, top=m)
    shallow_infos = _all_lines(ensure_shallow_engine(), board,
                               chess.engine.Limit(depth=ELO_SHALLOW_DEPTH), legal, new_game=True)
    shallow = {m: (_cp_mate(shallow_infos[m], us) if m in shallow_infos else deep[m]) for m in legal}

    Ld = _losses(deep, P["KAPPA"])
    Ls = _losses(shallow, P["KAPPA"])
    top = min(legal, key=lambda m: (Ld[m], san[m]))
    top_cp = deep[top]
    won = top_cp >= WIN_CP

    # Candidate set
    by_d = sorted(legal, key=lambda m: Ld[m])
    by_s = sorted(legal, key=lambda m: Ls[m])
    cands = list(dict.fromkeys(by_d[:P["K"]] + by_s[:P["K"]]))
    for m in legal:
        if board.is_capture(m) or board.gives_check(m) or m.promotion:
            if m not in cands:
                cands.append(m)
    cands = [m for m in cands if m in by_d[:2]
             or not is_junk_shuffle(board, m, deep[m] - top_cp)]

    perceived = {m: P["ALPHA"] * Ls[m] + (1 - P["ALPHA"]) * Ld[m] for m in cands}

    # Blind turn (prob P_BLIND): the player overlooks the opponent's single
    # refutation. For moves that lose ≥ BLIND_MIN_LOSS, re-score them with the
    # refuting reply (deep PV[1]) excluded; moves that only look fine "if he
    # doesn't see it" (hung piece, missed fork/pin) become tempting. Nonsense
    # moves stay bad because they are bad against every reply.
    if P["P_BLIND"] > 0 and RNG.random() < P["P_BLIND"]:
        pool = [m for m in sorted(legal, key=lambda x: Ld[x])
                if Ld[m] >= P.get("BLIND_MIN_LOSS", BLIND_MIN_LOSS) and len(deep_infos[m].get("pv") or []) >= 2][:BLIND_MAX_EVAL]
        blind_seen = []
        for b in pool:
            refute = deep_infos[b]["pv"][1]
            child = board.copy(stack=False)
            child.push(b)
            others = [r for r in child.legal_moves if r != refute]
            if not others:
                continue
            info = ensure_full_engine().analyse(child, chess.engine.Limit(depth=BLIND_DEPTH), root_moves=others)
            bcp = max(-CP_CLAMP_ELO, min(CP_CLAMP_ELO, int(info["score"].pov(us).score(mate_score=CP_CLAMP_ELO))))
            bl = max(_wp(top_cp) - _wp(bcp), P["KAPPA"] * (top_cp - bcp) / 100.0, 0.0)
            bl = max(0.0, bl - P.get("BLIND_TEMPT", 0.0))  # "looks attractive" bias
            perceived[b] = min(perceived.get(b, 1e9), bl)
            if b not in cands:
                cands.append(b)
            blind_seen.append(f"{san[b]}~{bl:.0f}")
        events.append(f"blind-turn: refutation overlooked for {blind_seen}")

    # Teach overlay: thematic FEN-legal moves get a bonus if within the band.
    recent_checks = _our_recent_checks(moves)
    teach_set = set()
    for tsan, theme in ([] if won else nominate_teach(board, legal_sans)):
        # no teach bonus once clearly winning: just convert (teach checks used to
        # throw away mates in won endings)
        if theme < 3:
            continue
        try:
            tm = board.parse_san(tsan)
        except Exception:
            continue
        if tm not in deep:
            continue
        if Ld[tm] > P["TEACH_MAX_LOSS"]:
            events.append(f"teach-skip {tsan}: deep loss {Ld[tm]:.1f} > {P['TEACH_MAX_LOSS']}")
            continue
        if hangs_major(board, tm, we_are_white) and Ld[tm] > 1.0:
            events.append(f"teach-skip {tsan}: hangs Q/R or allows mate-in-1")
            continue
        if won and board.gives_check(tm) and recent_checks >= 1 and Ld[tm] > CHECK_SPAM_TOL:
            events.append(f"teach-skip {tsan}: check spam in won position")
            continue
        teach_set.add(tm)
        if tm not in cands:
            cands.append(tm)
            perceived[tm] = P["ALPHA"] * Ls[tm] + (1 - P["ALPHA"]) * Ld[tm]
        perceived[tm] -= P["TEACH_BONUS"]

    # Won-position rules.
    if won and hist_keys:
        def reps(m):
            board.push(m)
            k = fen_key(board.fen())
            board.pop()
            return hist_keys.count(k)
        safe = [m for m in legal if reps(m) == 0 and deep[m] >= 0]
        if safe:
            dropped = [san[m] for m in cands if reps(m) > 0]
            if dropped:
                events.append(f"won-no-repeat: dropped {dropped}")
            cands = [m for m in cands if reps(m) == 0]
            if not cands:
                cands = sorted(safe, key=lambda m: Ld[m])[:3]
                for m in cands:
                    perceived.setdefault(m, Ld[m])
    if won and recent_checks >= 2:
        # after two consecutive checks, a further check must be the judge's top
        # move (or a capture/mate); otherwise play a quiet move.
        keep = [m for m in cands if not board.gives_check(m) or m == top or board.is_capture(m)
                or (mate_of.get(m) is not None and mate_of[m] > 0)]
        if keep and len(keep) < len(cands):
            events.append(f"won-check-spam: dropped {[san[m] for m in cands if m not in keep]}")
            cands = keep

    for m in cands:
        perceived.setdefault(m, P["ALPHA"] * Ls[m] + (1 - P["ALPHA"]) * Ld[m])

    def draw(pool):
        lo = min(perceived[m] for m in pool)
        w = [math.exp(-(perceived[m] - lo) / max(1e-6, P["T"])) for m in pool]
        r = RNG.random() * sum(w)
        for m, wt in zip(pool, w):
            r -= wt
            if r <= 0:
                return m, wt / sum(w)
        return pool[-1], w[-1] / sum(w)

    pool = list(cands)
    chosen, prob = draw(pool)
    raw = {m: _raw_cp(deep_infos[m], us) for m in legal}
    if P.get("budget_n"):
        events.append(f"budget: {P['budget_n']} blunder(s) on board this game -> guard obv/sub/queen/mate "
                      f"{P['GUARD_HANG_OBV_P']:.2f}/{P['GUARD_HANG_SUB_P']:.2f}/{P['GUARD_QUEEN_P']:.2f}/"
                      f"{P['GUARD_MATE1_P']:.2f}, winchance {P.get('GUARD_WC_P', 0.0):.2f}, P_BLIND {P['P_BLIND']:.3f}")
    # Guard: queen for nothing / allowing mate-in-N / hanging material.
    kept_kind = None
    for _ in range(6):
        kind, what = guard_kind(board, chosen, top, deep, raw, deep_infos, mate_of, P)
        if kind is None and P.get("GUARD_WC_P", 0.0) > 0 and chosen != top:
            # non-material Lichess-scale blunder (>= BUDGET_WP_BLUNDER win%-points
            # by the deep view): positional/tactical collapse the hang tests miss
            wdrop = _wp(top_cp) - _wp(deep[chosen])
            if wdrop >= BUDGET_WP_BLUNDER:
                kind, what = "winchance", f"drops {wdrop:.0f} win%"
        drop = top_cp - deep[chosen]
        p_block = (P["GUARD_QUEEN_P"] if kind == "queen" else P["GUARD_MATE1_P"] if kind == "mate" else
                   P.get("GUARD_HANG_OBV_P", 0.0) if kind == "obvious" else
                   P.get("GUARD_HANG_SUB_P", 0.0) if kind == "subtle" else
                   P.get("GUARD_WC_P", 0.0) if kind == "winchance" else 0.0)
        if kind is None:
            break
        if p_block <= 0:
            kept_kind = kind
            break
        roll = RNG.random()
        if roll < p_block and len(pool) > 1:
            events.append(f"guard-redraw: {san[chosen]} {what} (drop {drop}cp; roll {roll:.2f}<{p_block:.2f})")
            pool = [m for m in pool if m != chosen]
            chosen, prob = draw(pool)
        else:
            events.append(f"guard-kept-blunder: {san[chosen]} {what} (roll {roll:.2f}≥{p_block:.2f})")
            kept_kind = kind
            break
    blunder = kept_kind
    if blunder is None and chosen != top and _wp(top_cp) - _wp(deep[chosen]) >= BUDGET_WP_BLUNDER:
        blunder = "winchance"

    source = "teach" if chosen in teach_set else ("best" if chosen == top else "model")
    alts = [san[m] for m in sorted(cands, key=lambda m: perceived[m])]
    events.append(f"elo{elo}: {san[chosen]} p={prob:.2f} deepLoss={Ld[chosen]:.1f} "
                  f"shallowLoss={Ls[chosen]:.1f} ({deep[chosen] - top_cp:+d}cp vs {san[top]}) "
                  f"cands={len(cands)}")
    res = _elo_result(board, chosen, deep, Ld, source, events, alts, P, top=top)
    res["blunder"] = blunder
    res["gate"]["bud"] = int(P.get("budget_n", 0))
    return res


def _elo_result(board, chosen, deep, Ld, source, events, alts, P, top):
    us = board.turn
    def w(cp):
        return cp if us == chess.WHITE else -cp
    return {
        "san": board.san(chosen),
        "alts": alts,
        "score": w(deep[chosen]),
        "bestScore": w(deep[top]),
        "skillBestSan": None,
        "primarySan": board.san(chosen),
        "fullBestSan": board.san(top),
        "source": source,
        "guard": "none",
        "events": events,
        "gated": True,
        "mode": "elo",
        "gate": {"chosenCp": deep[chosen], "topCp": deep[top], "deepLoss": round(Ld.get(chosen, 0.0), 2),
                 "elo": P.get("elo")},
    }


def reset_engines() -> None:
    global FULL_ENGINE, SHALLOW_ENGINE
    for name in ("FULL_ENGINE", "SHALLOW_ENGINE"):
        eng = globals().get(name)
        if eng is not None:
            try:
                eng.quit()
            except Exception:
                try:
                    eng.close()
                except Exception:
                    pass
    FULL_ENGINE = None
    SHALLOW_ENGINE = None


def elo_ponder(board: chess.Board, elo: int, max_lines: int) -> list:
    infos = ensure_full_engine().analyse(board, chess.engine.Limit(time=ELO_PONDER_TIME), multipv=max_lines)
    if not isinstance(infos, list):
        infos = [infos]
    return [i["pv"][0] for i in infos if i.get("pv")]


def won_safeguard(board: chess.Board, out: dict, hist_keys: list[str],
                  moves: list[str] | None) -> dict:
    """Mode-independent final check (also covers cache hits / legacy mode).

    If the chosen move repeats an earlier position (2nd or 3rd occurrence) or is
    a 3rd consecutive check, and the judge says we are clearly winning
    (≥ WIN_CP, our POV), replace it with the best move that does not repeat and
    scores ≥ 0 (and, for check spam, is not a non-best check)."""
    try:
        mv = board.parse_san(out["san"])
    except Exception:
        return out
    board.push(mv)
    k = fen_key(board.fen())
    board.pop()
    repeats = hist_keys.count(k) >= 1 if hist_keys else False
    spam = board.gives_check(mv) and _our_recent_checks(moves) >= 2
    if not (repeats or spam):
        return out
    us = board.turn
    legal = list(board.legal_moves)
    infos = _all_lines(ensure_full_engine(), board, chess.engine.Limit(depth=ELO_DEEP_DEPTH, time=0.4), legal)
    deep = {m: _cp_mate(i, us) for m, i in infos.items()}
    if not deep or max(deep.values()) < WIN_CP:
        return out

    def reps(m):
        board.push(m)
        kk = fen_key(board.fen())
        board.pop()
        return hist_keys.count(kk) if hist_keys else 0
    top_cp = max(deep.values())
    ok = [m for m in deep if reps(m) == 0 and deep[m] >= 0]
    top_mv = max(deep, key=lambda m: deep[m])
    def spam_ok(m):
        return (not board.gives_check(m) or m == top_mv or board.is_capture(m)
                or (infos[m]["score"].pov(us).mate() or 0) > 0)
    if spam:
        ok2 = [m for m in ok if spam_ok(m)]
        ok = ok2 or ok
    if not ok:
        return out
    if mv in ok and not repeats and (not spam or spam_ok(mv)):
        return out
    best = max(ok, key=lambda m: deep[m])
    out = dict(out)
    reason = "repeat" if repeats else "check-spam"
    out["events"] = [*out.get("events", []),
                     f"won-safeguard ({reason}): {out['san']} → {board.san(best)} "
                     f"({deep[best]:+d}cp our POV, top {top_cp:+d})"]
    out["san"] = board.san(best)
    out["score"] = deep[best] if us == chess.WHITE else -deep[best]
    out["deviated"] = True
    out["deviateReason"] = f"won-{reason}"
    return out


def history_keys_from_moves(start_fen: str | None, moves: list[str]) -> list[str]:
    board = chess.Board(start_fen) if start_fen else chess.Board()
    keys = [fen_key(board.fen())]
    for m in moves or []:
        try:
            board.push_san(m)
        except Exception:
            try:
                board.push_uci(m)
            except Exception:
                break
        keys.append(fen_key(board.fen()))
    return keys


def would_repeat(board: chess.Board, san: str, hist_keys: list[str]) -> tuple[bool, int]:
    try:
        mv = board.parse_san(san)
    except Exception:
        return False, 0
    board.push(mv)
    k = fen_key(board.fen())
    board.pop()
    count_before = hist_keys.count(k)
    count_after = count_before + 1
    return count_after >= 2, count_after


def apply_threefold_policy(
    board: chess.Board,
    entry: dict,
    hist_keys: list[str],
    we_are_white: bool,
) -> dict:
    """Deviate only among gated MultiPV alts when repetition + ahead."""
    san = entry["san"]
    score = entry.get("score", 0)
    our = score if we_are_white else -score
    creates, count_after = would_repeat(board, san, hist_keys)
    if not creates:
        return entry
    ahead = our > AHEAD_CP
    force_third = count_after >= 3 and ahead
    soft_second = count_after == 2 and ahead
    if not (force_third or soft_second):
        return entry
    p = RNG.uniform(*THREEFOLD_DEVIATE_P)
    if RNG.random() > p and not force_third:
        return entry
    for alt in entry.get("alts") or []:
        if alt == san:
            continue
        try:
            board.parse_san(alt)
        except Exception:
            continue
        c2, ca = would_repeat(board, alt, hist_keys)
        if ca < count_after or not c2:
            entry = dict(entry)
            entry["san"] = alt
            entry["deviated"] = True
            entry["deviateReason"] = "threefold-avoid"
            return entry
    return entry


def regate_cached(board: chess.Board, entry: dict) -> tuple[bool, str, FullEval | None]:
    """Re-run the full-strength gate on a cached entry. Cache can never bypass it."""
    try:
        mv = board.parse_san(entry["san"])
    except Exception:
        return False, "illegal cached SAN", None
    we_are_white = board.turn == chess.WHITE
    ref = None
    if entry.get("primarySan") and entry["primarySan"] != entry["san"]:
        try:
            ref = board.parse_san(entry["primarySan"])
        except Exception:
            ref = None
    try:
        fe = full_eval(board, THINK_CACHE_FULL, 3)
        fe.ensure([mv, ref])
    except Exception as e:
        return False, f"full eval failed: {e}", None
    if mv not in fe.cp:
        return False, "no full-strength eval for cached move", fe
    source = entry.get("source")
    if source == "teach" and ref is not None:
        reason = teach_gate(fe, mv, ref, we_are_white)
        if reason:
            return False, f"teach {entry['san']} re-gate failed: {reason}", fe
    elif source in ("skill", "guard"):
        trig, why, _ = guard_trigger(fe, mv)
        if trig and not (source == "skill" and entry.get("guard") == "kept"):
            return False, f"cached {source} move {entry['san']} trips guard: {why}", fe
        if source == "guard" and fe.cp[mv] < fe.top_cp - GUARD_ALT_CP:
            return False, f"guard replacement {entry['san']} now {fe.cp[mv] - fe.top_cp:+d}cp vs top", fe
    else:
        # Unknown provenance (e.g. cache_put): gate vs full-strength top.
        if mv != fe.top:
            reason = teach_gate(fe, mv, fe.top, we_are_white)
            if reason:
                return False, f"cached {entry['san']} (unverified) failed gate vs top: {reason}", fe
    return True, f"re-gate ok ({fe.cp[mv] - fe.top_cp:+d}cp vs top {board.san(fe.top)})", fe


def store_cache(ck: str, chosen: dict) -> None:
    """Only cache moves that passed the full-strength gate; keep the verdict."""
    if not chosen.get("gated"):
        return
    CACHE[ck] = {
        "san": chosen["san"],
        "alts": list(chosen.get("alts") or []),
        "score": chosen["score"],
        "skillBestSan": chosen.get("skillBestSan"),
        "primarySan": chosen.get("primarySan"),
        "fullBestSan": chosen.get("fullBestSan"),
        "source": chosen.get("source"),
        "guard": chosen.get("guard"),
        "events": list(chosen.get("events") or []),
        "gate": chosen.get("gate"),
        "blunder": chosen.get("blunder"),
    }


def _public(chosen: dict) -> dict:
    keys = ("san", "alts", "score", "skillBestSan", "primarySan", "fullBestSan",
            "source", "guard", "gate", "blunder")
    out = {k: chosen.get(k) for k in keys}
    out["events"] = list(chosen.get("events") or [])
    return out


def _mode(msg: dict) -> tuple[str, int, str]:
    """('elo', elo, key) or ('skill', skill, key). Elo wins if given; a bare
    `skill` (legacy loop) keeps the old Skill-Level pipeline."""
    if msg.get("elo") is not None:
        e = int(msg["elo"])
        return "elo", e, f"e{e}"
    if msg.get("skill") is not None:
        s = int(msg["skill"])
        return "skill", s, str(s)
    return "elo", ELO_DEFAULT, f"e{ELO_DEFAULT}"


def regate_cached_elo(board: chess.Board, entry: dict) -> tuple[bool, str, FullEval | None]:
    """Elo-mode cache hit: the move was already drawn by the model (incl. the
    guard roll). Re-check with a fresh judge eval that it is not much worse
    than when it was drawn (>150cp) and that an un-rolled queen/mate guard
    case did not appear."""
    try:
        mv = board.parse_san(entry["san"])
    except Exception:
        return False, "illegal cached SAN", None
    try:
        fe = full_eval(board, THINK_CACHE_FULL, 3)
        fe.ensure([mv])
    except Exception as e:
        return False, f"full eval failed: {e}", None
    if mv not in fe.cp:
        return False, "no judge eval for cached move", fe
    drop_now = fe.top_cp - fe.cp[mv]
    g = entry.get("gate") or {}
    drop_then = (g.get("topCp", 0) or 0) - (g.get("chosenCp", 0) or 0)
    if drop_now > drop_then + 150:
        return False, f"cached {entry['san']} now {-drop_now:+d}cp vs top (was {-drop_then:+d})", fe
    return True, f"re-gate ok ({-drop_now:+d}cp vs top {board.san(fe.top)})", fe


def cmd_choose(msg: dict) -> dict:
    fen = msg["fen"]
    mode, level, pkey = _mode(msg)
    legal = msg.get("legal")
    hist = msg.get("historyKeys") or []
    we_are_white = bool(msg.get("weAreWhite", True))
    moves = msg.get("moves")
    start_fen = msg.get("startFen")
    if moves is not None:
        hist = history_keys_from_moves(start_fen, moves)

    ck = f"{fen_key(fen)}|{pkey}"
    board = chess.Board(fen)
    if "weAreWhite" not in msg:
        we_are_white = board.turn == chess.WHITE

    pre_events: list[str] = []
    game_id = msg.get("gameId")
    bud = budget_count(game_id, start_fen, moves, board.turn) if mode == "elo" else 0
    _BUDGET_HINT["n"] = bud
    if bud:
        pre_events.append(f"budget-count {bud}")
    if ck in CACHE and mode == "elo" and CACHE[ck].get("blunder") and \
            bud > int((CACHE[ck].get("gate") or {}).get("bud", 0)):
        pre_events.append(f"cache-reject: cached {CACHE[ck]['san']} was a {CACHE[ck]['blunder']} drawn at budget "
                          f"{(CACHE[ck].get('gate') or {}).get('bud', 0)} < {bud}")
        del CACHE[ck]
    if ck in CACHE:
        entry = dict(CACHE[ck])
        try:
            board.parse_san(entry["san"])
            legal_ok = True
        except Exception:
            legal_ok = False
        if legal_ok and (not legal or entry["san"] in legal):
            ok, why, fe = (regate_cached_elo if mode == "elo" else regate_cached)(board, entry)
            if ok:
                out = _public(entry)
                if fe is not None:
                    try:
                        mv = board.parse_san(entry["san"])
                        out["score"] = _white_cp(fe, mv)
                    except Exception:
                        pass
                out["events"] = [*pre_events, *out["events"], f"cache {why}"]
                if mode == "skill":
                    out = apply_threefold_policy(board, out, hist, we_are_white)
                out = won_safeguard(board, out, hist, moves)
                out["cached"] = True
                out["mode"] = mode
                _budget_after(game_id, fen, out, entry, bud)
                return out
            pre_events.append(f"cache-reject: {why}")
        else:
            pre_events.append("cache-reject: cached SAN not legal here")
        del CACHE[ck]

    if mode == "elo":
        try:
            chosen = elo_choose(board, level, legal, hist, moves, budget_n=bud)
        except Exception as e:  # engine stall/timeout: respawn engines, retry once, then judge top
            pre_events.append(f"elo_choose error {type(e).__name__}: {e}; respawning engines")
            reset_engines()
            try:
                chosen = elo_choose(board, level, legal, hist, moves, budget_n=bud)
            except Exception as e2:
                reset_engines()
                fe = full_eval(board, 0.3, 1)
                pre_events.append(f"elo_choose retry failed ({type(e2).__name__}); playing judge top")
                chosen = {"san": board.san(fe.top), "alts": [board.san(fe.top)], "score": _white_cp(fe, fe.top),
                          "fullBestSan": board.san(fe.top), "primarySan": board.san(fe.top),
                          "source": "fallback", "guard": "none", "events": [], "gated": False, "gate": {}}
    else:
        chosen = analyse_choose(board, level, legal)
    store_cache(ck, chosen)
    out = _public(chosen)
    out["events"] = [*pre_events, *out["events"]]
    out["cached"] = False
    if mode == "skill":
        out = apply_threefold_policy(board, out, hist, we_are_white)
    out = won_safeguard(board, out, hist, moves)
    out["mode"] = mode
    if mode == "elo":
        _budget_after(game_id, fen, out, chosen, bud)
    return out


def _budget_after(game_id, fen, out, chosen, bud) -> None:
    """Record a flagged move that is actually being played; bump the ponder hint."""
    b = chosen.get("blunder")
    if b and out.get("san") == chosen.get("san"):
        budget_record(game_id, fen, out["san"])
        out["events"] = [*out.get("events", []), f"budget-record: {out['san']} ({b}) -> {bud + 1} this game"]
        _BUDGET_HINT["n"] = bud + 1
        out["blunder"] = b
    else:
        out["blunder"] = None


def cmd_ponder(msg: dict) -> dict:
    """Opponent to move: MultiPV their moves → our reply via the same pipeline → cache."""
    fen = msg["fen"]
    mode, level, pkey = _mode(msg)
    max_lines = int(msg.get("maxLines", 4))
    board = chess.Board(fen)
    if mode == "elo":
        opps = elo_ponder(board, level, max_lines)
    else:
        eng = ensure_engine(level)
        infos = eng.analyse(board, chess.engine.Limit(time=THINK_PONDER), multipv=max_lines)
        if not isinstance(infos, list):
            infos = [infos]
        opps = [i["pv"][0] for i in infos if i.get("pv")]
    stored = []
    for opp in opps:
        try:
            child = board.copy(stack=False)
            child.push(opp)
        except Exception:
            continue
        if child.is_game_over():
            continue
        reply = elo_choose(child, level, None, budget_n=_BUDGET_HINT["n"]) if mode == "elo" else analyse_choose(child, level, None)
        ck = f"{fen_key(child.fen())}|{pkey}"
        store_cache(ck, reply)
        stored.append({
            "opp": board.san(opp),
            "childKey": fen_key(child.fen()),
            "ourSan": reply["san"],
            "source": reply.get("source"),
            "events": reply.get("events") or [],
        })
    return {"pondered": len(stored), "lines": stored, "cacheSize": len(CACHE)}


def cmd_cache_get(msg: dict) -> dict:
    ck = f"{fen_key(msg['fen'])}|{_mode(msg)[2]}"
    if ck in CACHE:
        return {"hit": True, **CACHE[ck]}
    return {"hit": False}


def main() -> None:
    try:  # warm the judge + shallow engines so the first move is not slow
        ensure_full_engine()
        ensure_shallow_engine()
    except Exception as e:
        print(f"engine warm-up failed: {e}", file=sys.stderr)
    print(json.dumps({"ok": True, "ready": True, "sf": SF_BIN, "gateCp": TEACH_GATE_CP,
                      "guardP": GUARD_REPLACE_P, "guardDropCp": GUARD_DROP_CP,
                      "eloAnchors": sorted(ELO_PROFILES), "eloCalibrated": sorted(ELO_CALIBRATED), "eloDefault": ELO_DEFAULT,
                      "eloOverrides": _ELO_OVERRIDES, "presets": PRESETS_SOURCE}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"bad json: {e}"}), flush=True)
            continue
        cmd = msg.get("cmd")
        try:
            if cmd == "ping":
                out = {"ok": True, "pong": True, "cacheSize": len(CACHE)}
            elif cmd == "choose":
                out = {"ok": True, **cmd_choose(msg)}
            elif cmd == "ponder":
                out = {"ok": True, **cmd_ponder(msg)}
            elif cmd == "cache_get":
                out = {"ok": True, **cmd_cache_get(msg)}
            elif cmd == "cache_put":
                # Unverified provenance: no "source" → re-gated vs full top on every hit.
                ck = f"{fen_key(msg['fen'])}|{_mode(msg)[2]}"
                CACHE[ck] = {
                    "san": msg["san"],
                    "alts": msg.get("alts") or [],
                    "score": msg.get("score", 0),
                    "source": None,
                    "events": ["cache_put (unverified)"],
                }
                out = {"ok": True, "stored": ck}
            elif cmd == "quit":
                print(json.dumps({"ok": True, "bye": True}), flush=True)
                break
            else:
                out = {"ok": False, "error": f"unknown cmd {cmd}"}
        except Exception as e:
            import traceback
            traceback.print_exc(file=sys.stderr)
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print(json.dumps(out), flush=True)
    global ENGINE, FULL_ENGINE, SHALLOW_ENGINE
    for eng in (ENGINE, FULL_ENGINE, SHALLOW_ENGINE):
        if eng is not None:
            try:
                eng.quit()
            except Exception:
                pass
    ENGINE = None
    FULL_ENGINE = None
    SHALLOW_ENGINE = None


if __name__ == "__main__":
    main()
