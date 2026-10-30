#!/usr/bin/env bash
# 场景：loom 被 kill -9（模拟关机/崩溃），随后 `-c -p` 续跑。
# 期望：stale pid 被识别为 interrupted；从 sessions.log 接回 worker；人的话落 human.log。
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
LOOM="$HERE/../loom.py"
FAKE="$HERE/fake_claude.py"
PY="${PYTHON:-python}"
command -v "$PY" >/dev/null 2>&1 || PY=python3
if command -v cygpath >/dev/null 2>&1; then
  LOOM="$(cygpath -m "$LOOM")"; FAKE="$(cygpath -m "$FAKE")"
fi

SB="$HERE/sandbox_kill.$(date +%H%M%S)"
mkdir -p "$SB"; cd "$SB"
echo "工作区：$SB"

CLAUDE="\"$PY\" \"$FAKE\""

echo "### 1. 起循环（worker 会一直睡着，等着被杀）"
FAKE_MODE=slow FAKE_BUDGET_MIN=5 "$PY" "$LOOM" -p "写一个 hello world 到 OUT.txt" --claude "$CLAUDE" >run1.log 2>&1 &
LPID=$!
sleep 4

echo "### 2. kill -9 整个 loom（模拟机器崩了）"
kill -9 "$LPID" 2>/dev/null
sleep 1
CHILD=$("$PY" -c "import json;print(json.load(open('work/tasks/task-01-hello/.state')).get('pid') or '')" 2>/dev/null)
echo "    遗留子进程 pid=$CHILD"
[ -n "$CHILD" ] && kill -9 "$CHILD" 2>/dev/null
sleep 1

echo "--- 被杀后 .state ---"; cat work/tasks/task-01-hello/.state; echo
if [ -n "$CHILD" ]; then
  ps -p "$CHILD" >/dev/null 2>&1 && echo "--- 残留子进程 $CHILD 还活着 ---" || echo "--- 残留子进程 $CHILD 已死 ---"
fi
echo "### 3. 人什么都没做，直接 -c -p 续跑"
"$PY" "$LOOM" -c -p "人插一句：做完就收工" --claude "$CLAUDE" >run2.log 2>&1
echo "--- run2 ---"
tail -12 run2.log

echo "### 4. 结果"
echo "--- .state ---"; cat work/tasks/task-01-hello/.state
echo "--- sessions.log ---"; tail -10 work/sessions.log
echo "--- human.log ---"; cat work/human.log
echo "--- git log ---"; git -C work log --oneline
echo "--- PROJECT ---"; grep -E "^(PROJECT|next_task):" work/STATE.md
