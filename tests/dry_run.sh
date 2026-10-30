#!/usr/bin/env bash
# 干跑：用假 claude 跑通 loom.py 的闭环，不消耗任何真实 token。
#   ./dry_run.sh               正常路径：bootstrap → worker → planner → DONE
#   FAKE_MODE=fail_summary ./dry_run.sh   测收尾复用
#   FAKE_MODE=crash ./dry_run.sh          测 checkpoint
#   FAKE_MODE=no_commit ./dry_run.sh      测 checkpoint 兜底
#   FAKE_MODE=slow BUDGET=0.2 ./dry_run.sh 测时钟阶梯（预算 12 秒）
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LOOM="$HERE/../loom.py"
FAKE="$HERE/fake_claude.py"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

# Windows 上必须用正斜杠：shlex 会把反斜杠当转义符，路径会被吃坏
if command -v cygpath >/dev/null 2>&1; then
  LOOM="$(cygpath -m "$LOOM")"; FAKE="$(cygpath -m "$FAKE")"
fi

SB="$HERE/sandbox"
if [ "${KEEP:-0}" != "1" ]; then rm -rf "$SB"; fi
mkdir -p "$SB"

GOAL="${GOAL:-写一个 hello world 到 OUT.txt}"
cd "$SB"
exec "$PY" "$LOOM" -p "$GOAL" --claude "\"$PY\" \"$FAKE\""
