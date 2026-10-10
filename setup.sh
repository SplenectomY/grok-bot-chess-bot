#!/usr/bin/env bash
# Chess Bot practice engine setup (Linux / macOS). Idempotent: safe to re-run.
#   - checks node >= 18 and python3
#   - creates .venv and installs requirements.txt (python-chess)
#   - downloads an official Stockfish release into bin/stockfish if none is found
#   - fetches the Agent Chess CLI (agent-chess.mjs; not vendored)
# Env: CHESS_HOME (default: this repo dir), STOCKFISH_BIN (use an existing engine),
#      STOCKFISH_TAG (default sf_17, the version the presets were calibrated with),
#      FORCE_CLI=1 (re-download agent-chess.mjs)
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
CHESS_HOME="${CHESS_HOME:-$REPO}"
SF_TAG="${STOCKFISH_TAG:-sf_17}"
CLI_URL="https://splenectomy.github.io/agent-chess/agent-chess.mjs"
say() { printf '[setup] %s\n' "$*"; }
die() { printf '[setup] ERROR: %s\n' "$*" >&2; exit 1; }
fetch() { if command -v curl >/dev/null; then curl -fsSL "$1" -o "$2"; else wget -qO "$2" "$1"; fi; }

# 1. node + python
command -v node >/dev/null || die "node not found (need Node.js 18+)"
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
[ "$NODE_MAJOR" -ge 18 ] || die "Node.js 18+ required (found $(node --version))"
command -v python3 >/dev/null || die "python3 not found"
say "node $(node --version), $(python3 --version)"

# 2. venv + python-chess
mkdir -p "$CHESS_HOME"
VENV="$CHESS_HOME/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  say "creating venv $VENV"
  python3 -m venv "$VENV" || die "python3 -m venv failed (Debian/Ubuntu: apt install python3-venv)"
fi
if ! "$VENV/bin/python" -c "import chess" 2>/dev/null; then
  "$VENV/bin/python" -m pip install -q --upgrade pip
  "$VENV/bin/python" -m pip install -q -r "$REPO/requirements.txt"
fi
say "python-chess $("$VENV/bin/python" -c 'import chess; print(chess.__version__)')"

# 3. Stockfish
SF=""
if [ -n "${STOCKFISH_BIN:-}" ] && [ -x "$STOCKFISH_BIN" ]; then SF="$STOCKFISH_BIN"
elif [ -x "$CHESS_HOME/bin/stockfish" ]; then SF="$CHESS_HOME/bin/stockfish"
elif command -v stockfish >/dev/null; then SF="$(command -v stockfish)"
fi
if [ -z "$SF" ]; then
  OS="$(uname -s)"; ARCH="$(uname -m)"; TAG="$SF_TAG"; ASSET=""
  case "$OS-$ARCH" in
    Linux-x86_64)
      if grep -qw avx2 /proc/cpuinfo 2>/dev/null; then ASSET=stockfish-ubuntu-x86-64-avx2.tar
      elif grep -qw sse4_1 /proc/cpuinfo 2>/dev/null && grep -qw popcnt /proc/cpuinfo; then ASSET=stockfish-ubuntu-x86-64-sse41-popcnt.tar
      else ASSET=stockfish-ubuntu-x86-64.tar; fi ;;
    Linux-aarch64|Linux-arm64) TAG=sf_19; ASSET=stockfish-linux-arm64-universal.tar.gz ;;  # no sf_17 linux arm build
    Darwin-arm64) ASSET=stockfish-macos-m1-apple-silicon.tar ;;
    Darwin-x86_64)
      if sysctl -n machdep.cpu.leaf7_features 2>/dev/null | grep -qw AVX2; then ASSET=stockfish-macos-x86-64-avx2.tar
      else ASSET=stockfish-macos-x86-64.tar; fi ;;
    *) die "no prebuilt Stockfish for $OS-$ARCH; install it and set STOCKFISH_BIN" ;;
  esac
  URL="https://github.com/official-stockfish/Stockfish/releases/download/$TAG/$ASSET"
  say "downloading Stockfish $TAG: $URL"
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  fetch "$URL" "$TMP/sf.tar"
  tar xf "$TMP/sf.tar" -C "$TMP"
  BIN="$(find "$TMP/stockfish" -maxdepth 1 -type f -name 'stockfish*' ! -name '*.txt' ! -name '*.md' ! -name '*.cff' | head -1)"
  [ -n "$BIN" ] || die "Stockfish binary not found in $ASSET"
  mkdir -p "$CHESS_HOME/bin"
  cp "$BIN" "$CHESS_HOME/bin/stockfish"; chmod +x "$CHESS_HOME/bin/stockfish"
  cp "$TMP/stockfish/Copying.txt" "$CHESS_HOME/bin/STOCKFISH-COPYING.txt" 2>/dev/null || true
  SF="$CHESS_HOME/bin/stockfish"
fi
SF_VER="$(echo quit | "$SF" 2>/dev/null | head -1)"
case "$SF_VER" in Stockfish*) say "engine: $SF ($SF_VER)";; *) die "Stockfish at $SF does not run";; esac

# 4. Agent Chess CLI (fetched, not vendored)
if [ ! -s "$CHESS_HOME/agent-chess.mjs" ] || [ "${FORCE_CLI:-0}" = 1 ]; then
  say "fetching $CLI_URL"
  fetch "$CLI_URL" "$CHESS_HOME/agent-chess.mjs.tmp" && mv "$CHESS_HOME/agent-chess.mjs.tmp" "$CHESS_HOME/agent-chess.mjs"
fi
node --check "$CHESS_HOME/agent-chess.mjs" || die "agent-chess.mjs failed node --check"

# 5. quick worker check
printf '%s\n' '{"cmd":"choose","fen":"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1","elo":1000,"weAreWhite":true,"moves":[]}' '{"cmd":"quit"}' \
  | STOCKFISH_BIN="$SF" CHESS_HOME="$CHESS_HOME" "$VENV/bin/python" -u "$REPO/chess-sf-worker.py" | sed -n 2p | grep -q '"ok": true' \
  && say "worker smoke test OK" || die "worker smoke test failed"

[ "$CHESS_HOME" = "$REPO" ] && ENVP="" || ENVP="CHESS_HOME=\"$CHESS_HOME\" "
[ "$SF" = "$CHESS_HOME/bin/stockfish" ] || ENVP="${ENVP}STOCKFISH_BIN=\"$SF\" "
echo
say "ready. Launch (ROOM = Agent Chess room code, 1000 = target Lichess rating):"
echo "  ${ENVP}node \"$REPO/chess-loop-teach.mjs\" ROOM 1000"
