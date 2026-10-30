#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
假 claude：只用来验证 loom.py 的流程，不产生任何智能。

它按 loom.py 传给它的提示词判断自己该演哪个角色，然后读写工作区，
再把控制权交回 loom.py。这样可以零成本跑通：
bootstrap → worker → 收尾 → planner → PROJECT: DONE

故障注入（环境变量 FAKE_MODE）：
  fail_summary   worker 跑完但不写 summary（测收尾复用）
  crash          worker 以 137 退出（测 checkpoint）
  slow           worker 睡很久（测时钟阶梯）
  no_commit      worker 不提交（测 checkpoint 兜底）
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

MODE = os.environ.get("FAKE_MODE", "")
CALLS = Path(".fake_calls.log")


def args_parse(argv):
    sid, resume, prompt = None, False, ""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--session-id":
            sid = argv[i + 1]; i += 2; continue
        if a in ("--resume", "-r"):
            sid = argv[i + 1]; resume = True; i += 2; continue
        if a in ("-p", "--print"):
            # 真 claude 支持 `cat x | claude -p`：-p 后面没有值时从 stdin 读。
            # loom.py 现在一律走 stdin，所以这里必须照着改，否则测的就不是真路径。
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if nxt is not None and not nxt.startswith("-"):
                prompt = nxt
                i += 2
            else:
                prompt = sys.stdin.read()
                i += 1
            continue
        i += 1
    return sid, resume, prompt


def commit(msg):
    if MODE == "no_commit":
        return
    subprocess.run(["git", "add", "--", "."], capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], capture_output=True)


def write(p: Path, s: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def read(p: Path, d=""):
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return d


def do_planner_bootstrap():
    tdir = Path("tasks/task-01-hello")
    write(tdir / "task.md", f"""# 任务契约

budget_min: {os.environ.get("FAKE_BUDGET_MIN", "1")}

## 目标

在 work/OUT.txt 写入一行 `hello world`。

## 完成判据

`cat OUT.txt` 输出 `hello world`。

## 约束

- 不要改 STATE.md 之外的顶层文件。

## 上下文

第一个任务，用来说明契约长什么样。
""")
    write(tdir / "log.md", "")
    write(Path("STATE.md"), """# 全局状态

PROJECT: RUNNING
next_task: task-01-hello

## 任务表

| id | 状态 | 一句话结论 |
|---|---|---|
| task-01-hello | queued | 写 hello world |

## 全局判断

先做最小可验证的一步。

## 决策日志

- 2026-10-30 · 拆分出 task-01-hello · 最小可验证
""")
    commit("[plan] 初始拆分")
    print("planner bootstrap done: task-01-hello")


def do_planner_round(prompt):
    state = read(Path("STATE.md"))
    facts = []
    for s in Path("tasks").glob("*/summary.md"):
        st = re.search(r"status:\s*(\S+)", read(s))
        facts.append(f"{s.parent.name}={st.group(1) if st else '?'}")
    # 只要有 summary（done 或 partial）就算有结论，可以收工
    done = len(facts) > 0 and MODE != "stall"

    if done:
        state = re.sub(r"^PROJECT:.*$", "PROJECT: DONE", state, flags=re.M)
        state = re.sub(r"^next_task:.*$", "next_task: -", state, flags=re.M)
        state += "\n- 依据：" + "、".join(facts) + "；OUT.txt 存在\n"
        write(Path("STATE.md"), state)
        commit("[plan] 目标达成，宣布 DONE")
        print("planner: PROJECT DONE")
        return

    # 还有没关闭的任务：原样指向它
    pending = [p.parent.name for p in Path("tasks").glob("*/task.md")]
    target = pending[0] if pending else "-"
    state = re.sub(r"^next_task:.*$", f"next_task: {target}", state, flags=re.M)
    write(Path("STATE.md"), state)
    commit(f"[plan] 继续 {target}")
    print(f"planner: next={target}  facts={facts}")


def do_worker(prompt, sid):
    m = re.search(r"tasks/(task-[0-9A-Za-z\-]+)/", prompt)
    tdir = Path("tasks") / (m.group(1) if m else "task-01-hello")

    if MODE == "slow":
        for _ in range(600):
            time.sleep(1)

    write(Path("OUT.txt"), "hello world\n")
    with (tdir / "log.md").open("a", encoding="utf-8") as f:
        f.write(f"- {datetime.now():%H:%M:%S} 写入 OUT.txt，cat 验证通过\n")

    if MODE == "fail_summary":
        print("worker: 故意不写 summary")
        return
    if MODE == "crash":
        print("worker: 故意崩溃")
        sys.exit(137)

    write(tdir / "summary.md", """---
status: done
---
## 结论

OUT.txt 已写入 hello world，cat 验证通过。

## 做了什么 / 产出在哪

OUT.txt（仓库根目录），一行 `hello world`。

## 未解决 / 建议下一步

无。可以关闭本任务。
""")
    commit(f"[{tdir.name}][feat] 写入 OUT.txt 并验证")
    print("worker: done")


def do_wrapup(prompt):
    m = re.search(r"tasks/(task-[0-9A-Za-z\-]+)/", prompt)
    tdir = Path("tasks") / (m.group(1) if m else "task-01-hello")
    if not (tdir / "summary.md").exists():
        write(tdir / "summary.md", """---
status: partial
---
## 结论

超预算被收尾；OUT.txt 已写入，未做进一步验证。

## 做了什么 / 产出在哪

OUT.txt。

## 未解决 / 建议下一步

补一次 cat 验证即可。
""")
        commit(f"[{tdir.name}][docs] 收尾 summary")
    print("wrapup: summary written")


def main():
    argv = sys.argv[1:]
    sid, resume, prompt = args_parse(argv)
    with CALLS.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"sid": (sid or "")[:8], "resume": resume,
                            "head": prompt[:40]}, ensure_ascii=False) + "\n")

    # 先按最明确的身份判，别让正文里出现的"收尾"两个字把 worker 认成收尾会话
    if "你是 planner" in prompt:
        if "拆成" in prompt:
            do_planner_bootstrap()
        else:
            do_planner_round(prompt)
        return 0
    if "你是 worker" in prompt or "中断恢复" in prompt or "预算提醒" in prompt:
        do_worker(prompt, sid)
        return 0
    if prompt.lstrip().startswith("【") and ("收尾" in prompt or "兜底" in prompt):
        do_wrapup(prompt)
        return 0
    print("fake: 没识别出角色，什么也不做")
    return 0


if __name__ == "__main__":
    sys.exit(main())
