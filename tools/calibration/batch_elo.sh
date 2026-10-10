#!/usr/bin/env bash
# Self-play calibration batch at a target Elo (ELO first; otherwise same as batch.sh).
# usage: batch_elo.sh ELO TAG NGAMES OPP PONDER 'OVERRIDES_JSON' [SEEDBASE] [PAR]
#   OPP: self | sf1320 | old6   PONDER: 0|1   OVERRIDES_JSON: e.g. '{"T":5.2}'
# Env: STOCKFISH_BIN, CHESS_PYTHON (default: repo .venv python or python3), SCORE_DEPTH (default 14)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PY="${CHESS_PYTHON:-}"
if [ -z "$PY" ]; then if [ -x "$REPO/.venv/bin/python" ]; then PY="$REPO/.venv/bin/python"; else PY=python3; fi; fi
export CHESS_PYTHON="$PY"
ELO=$1; shift
TAG=$1; N=$2; OPP=$3; PON=$4; OV=${5:-'{}'}; SB=${6:-100}; PAR=${7:-6}
DEPTH=${SCORE_DEPTH:-14}
mkdir -p "$TAG"
export CHESS_ELO_OVERRIDES="$OV"
seq 1 "$N" | xargs -P "$PAR" -I{} sh -c "\"$PY\" \"$HERE/selfplay.py\" \"$TAG/g{}.json\" $ELO $OPP \$(( $SB + {} )) 160 $PON > \"$TAG/g{}.log\" 2>&1"
ls "$TAG"/g*.json | grep -v scored | xargs -P "$PAR" -I{} "$PY" "$HERE/score.py" "$DEPTH" {} > /dev/null
"$PY" "$HERE/report.py" "$TAG"/*.scored.json | tee "$TAG/report.txt"
