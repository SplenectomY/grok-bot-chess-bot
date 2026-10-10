"""Shared path resolution for calibration tools (portable; no hardcoded dirs)."""
import os, shutil, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("CHESS_HOME") or HERE.parent.parent).resolve()
WORKER = os.environ.get("CHESS_SF_WORKER") or str(REPO / "chess-sf-worker.py")
PYTHON = os.environ.get("CHESS_PYTHON") or sys.executable


def stockfish_bin() -> str:
    env = os.environ.get("STOCKFISH_BIN")
    if env:
        return env
    for cand in (REPO / "bin" / "stockfish",):
        if cand.exists():
            return str(cand)
    found = shutil.which("stockfish")
    if found:
        return found
    raise SystemExit("Stockfish not found: set STOCKFISH_BIN or run setup.sh")


SF = stockfish_bin
