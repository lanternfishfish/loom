#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Loom 驱动器：一个共享目录 + 两个角色 + 一个时钟。

它只做流程，不做判断：
  - 起会话、记流水、跑时钟、写机器事实、判断"有没有 summary"
  - 不解析自然语言，不做策略，不做质量验收

用法：
  loom.py -p "目标"               首次：建骨架 → planner 拆分 → 主循环
  loom.py -c                     接回上次中断的位置
  loom.py -c -p "…" [-p "…"]     接回 + 注入人的话（落 human.log）
  loom.py --status               打印队列与任务状态
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import string
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TPL_DIR = ROOT / "templates"
PRM_DIR = ROOT / "prompts"

SESSION_LOG_MAX = 200      # sessions.log 超过就裁头
HUMAN_LOG_MAX = 60         # human.log 超过就归档（planner 每轮都要读它）
DEFAULT_TASK_BUDGET = 30   # 分钟
DEFAULT_PLANNER_TIMEOUT = 20
WRAPUP_GRACE = 300         # 收尾会话给的最长时间（秒）
PENDING = None             # 进程内占位

STOP = False               # Ctrl+C 置位


# ---------------------------------------------------------------- 基础设施

class CliError(RuntimeError):
    """起会话这件事本身失败（命令找不到、无法执行）——属于环境问题，不是任务失败。"""


def split_spec(spec: str) -> list[str]:
    """把 --claude 的值拆成 argv。Windows 路径里的反斜杠不能被 shlex 当转义吃掉。"""
    s = spec.strip()
    if not s:
        return []
    if os.path.exists(s) or shutil.which(s):
        return [s]
    if os.name != "nt":
        return shlex.split(s)
    out, buf, quote = [], "", None
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
            else:
                buf += ch
        elif ch in "\"'":
            quote = ch
        elif ch.isspace():
            if buf:
                out.append(buf)
                buf = ""
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


def resolve_launcher(spec: str) -> tuple[list[str], bool]:
    """解析成 (argv 前缀, 是否 Windows 批处理)。

    npm 在 Windows 上装的是 claude.cmd（批处理），CreateProcess 不能直接执行它，
    必须经 cmd.exe；这也是"在哪个目录下跑"完全无关的那类问题。
    """
    parts = split_spec(spec)
    if not parts:
        raise CliError("没有指定 claude 命令")
    exe, rest = parts[0], parts[1:]
    if os.path.isabs(exe) and os.path.exists(exe):
        found = exe
    else:
        found = shutil.which(exe) or (exe if os.path.exists(exe) else None)
    if not found:
        raise CliError(
            f"找不到命令 {exe!r}。\n"
            "  这里需要的是 claude 的**可执行入口**，不是会话。\n"
            "  Windows 上 npm 装的是 claude.cmd —— 它不在 PATH 里就会这样。\n"
            "  用 --claude <claude.cmd 的完整路径> 指定，或设环境变量 LOOM_CLAUDE。"
        )
    is_batch = os.name == "nt" and found.lower().endswith((".cmd", ".bat"))
    return [found, *rest], is_batch


def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 60}分{sec % 60}秒" if sec >= 60 else f"{sec}秒"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def say(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def read_text(p: Path, default: str = "") -> str:
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


def write_text(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(s, encoding="utf-8")
    tmp.replace(p)


def prompt(name: str, **kw) -> str:
    raw = read_text(PRM_DIR / f"{name}.txt")
    if not raw:
        raise SystemExit(f"缺少提示词模板：{PRM_DIR / (name + '.txt')}")
    return string.Template(raw).safe_substitute(**kw)


def pid_alive(pid, pid_start) -> bool:
    """pid 还在不在；能拿到启动时刻就一并核对（防 pid 复用）。"""
    if not pid:
        return False
    pid = int(pid)
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
            cur_start = fields[19]
            if pid_start and str(pid_start) != cur_start:
                return False          # 号还在，但不是那个进程
            return True
        except Exception:
            return True
    if os.name == "nt":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return code.value == 259   # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def kill_pid(pid: int) -> None:
    """杀掉上一任 loom 留下的孤儿进程（我们按 pid 杀，没有 Popen 可等）。"""
    try:
        pid = int(pid)
    except Exception:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        except Exception:
            return
        time.sleep(1.0)
        if not os.path.exists(f"/proc/{pid}"):
            return


def proc_start_token(pid: int):
    """记录进程启动时刻，用于识别 pid 复用；拿不到就返回 None。"""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19]
    except Exception:
        return None


def _signal_soft(proc: subprocess.Popen) -> None:
    """请求它自己退出（好让转录落盘）。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        # 子进程在独立进程组里（CREATE_NEW_PROCESS_GROUP），CTRL_BREAK 不会波及 loom 自己
        try:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            return
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        return
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass


def _signal_hard(proc: subprocess.Popen) -> None:
    if os.name != "nt":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        return
    # Windows：claude.cmd 是 cmd.exe 的子壳，node 又是它的子进程。
    # 只 kill(cmd) 会留下没人管的 node —— 必须 /T 杀整棵树。
    try:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, timeout=20)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def kill_group(proc: subprocess.Popen, grace: float = 5.0) -> None:
    """先温和地请它退出，让它把转录落盘；超时才硬杀。"""
    if proc.poll() is not None:
        return
    _signal_soft(proc)
    try:
        proc.wait(timeout=3.0 if os.name == "nt" else grace)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_hard(proc)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


# ---------------------------------------------------------------- 工作区

class Work:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.state = self.root / "STATE.md"
        self.objective = self.root / "OBJECTIVE.md"
        self.claude_md = self.root / "CLAUDE.md"
        self.human = self.root / "human.log"
        self.human_archive = self.root / "human.archive.log"
        self.sessions = self.root / "sessions.log"
        self.tasks = self.root / "tasks"
        self.shared = self.root / "shared"
        self.loomhash = self.root / ".loomhash.json"
        self.loomlog = self.root.parent / "loom.log"

    # -- 目录

    def exists(self) -> bool:
        return self.objective.exists()

    def ensure(self) -> None:
        for d in (self.root, self.tasks, self.shared):
            d.mkdir(parents=True, exist_ok=True)
        for name, dst in (
            ("CLAUDE.md", self.claude_md),
            ("STATE.md", self.state),
            ("gitignore", self.root / ".gitignore"),
        ):
            if not dst.exists():
                write_text(dst, read_text(TPL_DIR / name))
        if not self.human.exists():
            write_text(self.human, "")
        if not self.sessions.exists():
            write_text(self.sessions, "")

    def bootstrap_objective(self, text: str) -> None:
        tpl = read_text(TPL_DIR / "OBJECTIVE.md")
        body = text.strip()
        out = tpl.replace("（一句话说清最终状态）", body)
        out = out.replace(
            "1. （可判真假的条目，越具体越好）\n2.",
            "1. 上述目标达成，且每一步的产物可复现。\n2. 每个任务的完成判据由 planner 写成可判真假的一句话。",
        )
        out = out.replace(
            "（由 loom.py 在首次运行时写入。补充说明请人直接编辑本文件。）",
            "由 loom.py 首次启动时写入 `-p` 的原文。要改目标请直接编辑本文件。",
        )
        out = out.replace(
            "（背景、已知线索、人为什么想做这件事）",
            "由 loom.py 首次启动时写入 `-p` 的原文。要改目标请直接编辑本文件。",
        )
        write_text(self.objective, out)

    # -- 全局状态（只读两行机器字段）

    def global_state(self) -> tuple[str, str | None]:
        project, next_task = "RUNNING", None
        for line in read_text(self.state).splitlines():
            m = re.match(r"^\s*PROJECT:\s*([A-Za-z]+)", line)
            if m:
                project = m.group(1).upper()
            m = re.match(r"^\s*next_task:\s*(\S+)", line)
            if m:
                v = m.group(1).strip()
                next_task = None if v in ("-", "—", "") else v
        return project, next_task

    # -- 会话流水

    def log_session(self, sid: str, role: str, task: str, event: str) -> None:
        line = f"{now()}  {sid}  {role}  {task}  {event}\n"
        with self.sessions.open("a", encoding="utf-8") as f:
            f.write(line)
        lines = self.sessions.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) > SESSION_LOG_MAX:
            self.sessions.write_text("".join(lines[-SESSION_LOG_MAX:]), encoding="utf-8")

    def last_session(self) -> dict | None:
        """从尾部向前扫第一个合法行（容忍崩溃留下的残缺尾行）。"""
        lines = read_text(self.sessions).splitlines()
        for line in reversed(lines):
            parts = [p.strip() for p in re.split(r"\s{2,}", line.strip()) if p.strip()]
            if len(parts) != 5:
                continue
            ts, sid, role, task, event = parts
            if not re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$", ts):
                continue
            return {"sid": sid, "role": role, "task": task, "event": event, "ts": ts}
        return None

    # -- 人说的话

    def log_human(self, text: str, via: str = "-p") -> None:
        line = f"{now()}  [via {via}]  {text.strip()}\n"
        with self.human.open("a", encoding="utf-8") as f:
            f.write(line)
        lines = self.human.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) > HUMAN_LOG_MAX:
            keep, old = lines[-HUMAN_LOG_MAX:], lines[:-HUMAN_LOG_MAX]
            with self.human_archive.open("a", encoding="utf-8") as f:
                f.writelines(old)
            self.human.write_text("".join(keep), encoding="utf-8")

    def human_note(self, texts: list[str]) -> str:
        if not texts:
            return ""
        body = "\n".join(f"- {t.strip()}" for t in texts)
        return (
            "【人的指令】\n"
            f"{body}\n"
            "这是人刚刚说的话，优先级高于文件里的旧计划。但它不能替代 OBJECTIVE.md 的验收标准：\n"
            "若它与目标冲突，照做，但不要宣布达成，并在 STATE.md 记一行 [needs-human] 指令与目标冲突。\n"
            "若你当前任务与它冲突：停手，写 summary.md（status: failed，原因\"人已改变方向\"）再退出。\n"
        )

    # -- 机械检测：人改过哪些文件

    def hash_watched(self) -> dict:
        watched = [self.objective, self.state] + sorted(self.tasks.glob("*/task.md"))
        out = {}
        for p in watched:
            if p.exists():
                out[str(p.relative_to(self.root))] = p.stat().st_mtime_ns
        return out

    def refresh_hash(self) -> None:
        write_text(self.loomhash, json.dumps(self.hash_watched(), ensure_ascii=False, indent=0))

    def diff_and_record_human_edits(self) -> list[str]:
        old = {}
        try:
            old = json.loads(read_text(self.loomhash, "{}"))
        except Exception:
            old = {}
        cur = self.hash_watched()
        changed = []
        for k, v in cur.items():
            if k in old and old[k] != v:
                changed.append(k)
                self.log_human(f"{k} 被改（mtime 变化）", via="文件")
        write_text(self.loomhash, json.dumps(cur, ensure_ascii=False, indent=0))
        return changed


# ---------------------------------------------------------------- 任务

class Task:
    def __init__(self, work: Work, task_id: str):
        self.work = work
        self.id = task_id
        self.dir = work.tasks / task_id
        self.state_file = self.dir / ".state"
        self.summary = self.dir / "summary.md"
        self.log = self.dir / "log.md"
        self.contract = self.dir / "task.md"

    def st(self) -> dict:
        try:
            return json.loads(read_text(self.state_file, "{}"))
        except Exception:
            return {}

    def save(self, **kw) -> None:
        cur = self.st()
        cur.update(kw)
        cur["updated"] = now()
        write_text(self.state_file, json.dumps(cur, ensure_ascii=False, indent=2))

    def budget_min(self) -> float:
        m = re.search(r"^\s*budget_min:\s*([0-9.]+)", read_text(self.contract), re.M)
        return float(m.group(1)) if m else float(DEFAULT_TASK_BUDGET)

    def has_summary(self) -> bool:
        """只看在不在 + 有没有 status: ——内容一个字都不读。"""
        if not self.summary.exists():
            return False
        head = self.summary.read_text(encoding="utf-8", errors="replace")[:600]
        return re.search(r"^\s*status:\s*\S+", head, re.M) is not None

    def summary_status(self) -> str:
        m = re.search(r"^\s*status:\s*(\S+)", read_text(self.summary), re.M)
        return m.group(1).strip() if m else "?"


# ---------------------------------------------------------------- 驱动器

class Loom:
    def __init__(self, work: Work, claude: str, claude_args: list[str]):
        self.work = work
        self.claude = claude
        self.claude_args = claude_args

    # -- 进程

    def spawn(self, sid: str, text: str, resume: bool = False,
              role: str = "", task: str = "-") -> subprocess.Popen:
        argv, is_batch = resolve_launcher(self.claude)
        argv += list(self.claude_args)
        argv += ["--resume", sid] if resume else ["--session-id", sid]
        # prompt 走 stdin（cat 1.txt | claude -p）：命令行里只剩 uuid 和开关，
        # 长中文、换行、引号都不再经过任何一层的转义。
        argv += ["-p"]

        log = self.work.loomlog.open("a", encoding="utf-8")
        log.write(f"\n===== {now()} {'resume' if resume else 'new'} {sid}\n{text}\n----- output\n")
        log.flush()

        common = dict(cwd=str(self.work.root), stdout=log, stderr=subprocess.STDOUT,
                      stdin=subprocess.PIPE)
        try:
            if is_batch:
                # 目标形式：cmd /d /s /c ""path" args"
                #   /s + 首字符是引号 → cmd 会剥掉首尾各一个引号，剩下 "path" args
                #   注意别把 argv[0] 的引号再叠一层（"""path"" 会被当成一个文件名）
                tail = subprocess.list2cmdline(argv[1:])
                comspec = os.environ.get("COMSPEC", "cmd.exe")
                line = f'{comspec} /d /s /c ""{argv[0]}"'
                if tail:
                    line += " " + tail
                line += '"'
                proc = subprocess.Popen(line, **common)
            elif os.name == "nt":
                proc = subprocess.Popen(
                    argv, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP, **common)
            else:
                proc = subprocess.Popen(argv, start_new_session=True, **common)
        except OSError as exc:
            log.write(f"[spawn 失败] {exc}\n")
            log.flush()
            log.close()
            self.work.log_session(sid, role or "?", task, "exit(spawn-failed)")
            raise CliError(f"启动会话失败：{exc}") from exc

        try:
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass          # 它已经自己退出了，不是我们的问题
        return proc

    def run_plain(self, sid: str, text: str, timeout_s: float, resume: bool = False,
                  role: str = "", task: str = "-") -> tuple[int, bool]:
        p = self.spawn(sid, text, resume=resume, role=role, task=task)
        try:
            rc = p.wait(timeout=timeout_s)
            return rc, False
        except subprocess.TimeoutExpired:
            kill_group(p)
            return -1, True
        except KeyboardInterrupt:
            kill_group(p)
            raise

    # -- planner

    def planner(self, kind: str, fact: str = "") -> None:
        sid = str(uuid.uuid4())
        text = prompt(
            "planner_bootstrap" if kind == "bootstrap" else "planner_round",
            workdir=str(self.work.root),
            fact=fact or "（无）",
        )
        self.work.log_session(sid, "planner", "-", "start")
        say(f"planner({kind})  session {sid[:8]}…")
        before = read_text(self.work.state)
        rc, timed_out = self.run_plain(sid, text, DEFAULT_PLANNER_TIMEOUT * 60, role="planner")
        self.work.log_session(sid, "planner", "-", f"exit({rc})" + ("*" if timed_out else ""))
        after = read_text(self.work.state)
        self.planner_no_progress = 0 if before != after else self.planner_no_progress + 1
        self.work.refresh_hash()

    # -- worker（带时钟阶梯）

    def worker(self, task: Task, sid: str, budget_s: float, first_text: str, resume: bool) -> tuple[int, bool]:
        p = self.spawn(sid, first_text, resume=resume, role="worker", task=task.id)
        task.save(pid=p.pid, pid_start=proc_start_token(p.pid))
        start = time.monotonic()
        marks = [(0.50, "remind"), (0.75, "remind"), (0.95, "remind"), (1.00, "wrapup")]
        idx = 0
        killed = False
        while p is not None:
            if STOP:
                kill_group(p)
                self.work.log_session(sid, "worker", task.id, "exit(human-stop)")
                return 130, True
            if idx >= len(marks):
                rc = p.wait()
                return rc, killed
            deadline = start + marks[idx][0] * budget_s
            remain = deadline - time.monotonic()
            if remain > 0:
                try:
                    rc = p.wait(timeout=remain)
                    return rc, killed
                except subprocess.TimeoutExpired:
                    continue
                except KeyboardInterrupt:
                    kill_group(p)
                    self.work.log_session(sid, "worker", task.id, "exit(human-stop)")
                    raise
            frac, kind = marks[idx]
            idx += 1
            left_s = budget_s * (1 - frac)
            kill_group(p)
            killed = True
            self.work.log_session(sid, "worker", task.id, f"{kind}{int(frac * 100)}")
            if kind == "remind":
                say(f"提醒 {int(frac * 100)}%：还剩 {fmt_dur(left_s)}")
                p = self.spawn(
                    sid,
                    prompt("remind", pct=f"{int(frac * 100)}%", left_min=fmt_dur(left_s)),
                    resume=True, role="worker", task=task.id,
                )
                task.save(pid=p.pid, pid_start=proc_start_token(p.pid))
            else:
                say("预算用尽 → 收尾")
                p = self.spawn(sid, prompt("wrapup", task_dir=self.rel(task.dir)),
                               resume=True, role="wrapup", task=task.id)
                task.save(pid=p.pid, pid_start=proc_start_token(p.pid))
                try:
                    p.wait(timeout=WRAPUP_GRACE)
                except subprocess.TimeoutExpired:
                    kill_group(p)
                self.work.log_session(sid, "wrapup", task.id, "exit")
                return -1, True
        return -1, killed

    def rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.work.root))
        except ValueError:
            return str(p)

    # -- 收尾

    def ensure_summary(self, task: Task, sid: str | None) -> None:
        if task.has_summary():
            return
        if sid:
            say("没有 summary → 复用原会话收尾")
            self.work.log_session(sid, "wrapup", task.id, "start")
            rc, _ = self.run_plain(sid, prompt("wrapup", task_dir=self.rel(task.dir)),
                                   WRAPUP_GRACE, resume=True, role="wrapup", task=task.id)
            self.work.log_session(sid, "wrapup", task.id, f"exit({rc})")
            if task.has_summary():
                return
        say("复用失败 → 只读文件兜底")
        fresh = str(uuid.uuid4())
        self.work.log_session(fresh, "wrapup", task.id, "start(fresh)")
        rc, _ = self.run_plain(fresh, prompt("wrapup_fresh", task_dir=self.rel(task.dir)),
                               WRAPUP_GRACE, role="wrapup", task=task.id)
        self.work.log_session(fresh, "wrapup", task.id, f"exit({rc})")

    # -- 跑一个任务

    def run_task(self, task_id: str, resumed_sid: str | None = None) -> None:
        task = Task(self.work, task_id)
        if not task.dir.exists():
            self.planner("round", fact=f"next_task 指向 {task_id}，但这个目录不存在")
            return

        if task.has_summary():
            self.close_task(task, "已有 summary，直接交给 planner")
            return

        st = task.st()
        sid = resumed_sid or st.get("sid") or str(uuid.uuid4())
        attempts = int(st.get("attempts") or 0) + 1
        budget_s = task.budget_min() * 60
        if resumed_sid and st.get("started_at"):
            elapsed = time.time() - float(st["started_at"])
            budget_s = max(budget_s - elapsed, budget_s * 0.25)
        task.save(sid=sid, status="running", attempts=attempts, started_at=st.get("started_at") or time.time())
        before = snapshot(self.work)

        if resumed_sid:
            note = self.work.human_note(self.pending_human)
            say(f"resume worker {task_id}  session {sid[:8]}…")
            rc, killed = self.worker(task, sid, budget_s, prompt("recovery", human_note=note), resume=True)
        else:
            self.work.log_session(sid, "worker", task_id, "start")
            say(f"worker {task_id}  session {sid[:8]}… 预算 {fmt_dur(budget_s)}")
            rc, killed = self.worker(
                task, sid, budget_s,
                prompt("worker", workdir=str(self.work.root), task_dir=self.rel(task.dir), budget=fmt_dur(budget_s)),
                resume=False
            )

        task.save(status="timed_out" if killed else "exited", pid=None, pid_start=None)
        self.work.log_session(sid, "worker", task_id, f"exit({rc})")
        if killed or rc not in (0,):
            git_checkpoint(self.work, before, "worker", task_id, sid)
        if not task.has_summary():
            self.ensure_summary(task, sid)
        self.work.refresh_hash()
        self.close_task(task, f"exit={rc} killed={killed} summary={'有' if task.has_summary() else '无'} attempts={attempts}")

    def close_task(self, task: Task, fact: str) -> None:
        if task.has_summary():
            fact += f"，summary status={task.summary_status()}"
        if int(task.st().get("attempts") or 0) >= 3:
            fact += "，注意：本任务已尝试 3 次以上"
        self.planner("round", fact=f"{task.id}: {fact}")

    # -- 主循环

    def main_loop(self, max_cycles: int) -> int:
        for _ in range(max_cycles):
            project, next_task = self.work.global_state()
            if project in ("DONE", "BLOCKED"):
                say(f"PROJECT: {project} —— 循环结束")
                return 0
            if not next_task:
                self.planner("round", fact="队列空（STATE.md 里 next_task 是 -）")
                if self.stalled():
                    return 3
                continue
            self.run_task(next_task)
            if self.stalled():
                return 3
        say(f"达到最大轮数 {max_cycles}，停下")
        return 4

    def stalled(self) -> bool:
        """连续 3 轮 planner 没改写 STATE.md —— 多半是空转，停下来等人。"""
        if self.planner_no_progress >= 3:
            say("planner 连续 3 轮没有改写 STATE.md，判为空转，停下等人")
            say("可以 `-c -p \"…\"` 给一句话，或直接改 OBJECTIVE.md / STATE.md")
            return True
        return False

    planner_no_progress = 0
    pending_human: list[str] = []


# ---------------------------------------------------------------- git

def git(work: Work, *args: str, check: bool = False) -> tuple[int, str]:
    try:
        r = subprocess.run(
            ["git", *args], cwd=str(work.root), capture_output=True, text=True, timeout=120
        )
        if check and r.returncode != 0:
            say(f"git {' '.join(args)} 失败：{(r.stderr or r.stdout).strip()[:200]}")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # git 不在、仓库坏了，都不该让循环崩掉
        say(f"git 调用异常：{e}")
        return -1, str(e)


def git_identity(work: Work) -> None:
    """机器上没配 git 身份时，给仓库补一个本地的；否则所有提交都会失败。"""
    rc, out = git(work, "config", "user.email")
    if rc == 0 and out.strip():
        return
    git(work, "config", "user.name", "Loom")
    git(work, "config", "user.email", "loom@localhost")
    say("仓库未配 git 身份，已设为 Loom <loom@localhost>（仅本仓库）")


def git_init(work: Work) -> None:
    if (work.root / ".git").exists():
        git_identity(work)
        return
    git(work, "init", "-q")
    git_identity(work)
    git(work, "add", "-A")
    rc, out = git(work, "commit", "-q", "-m", "[init] work 骨架：宪法 / 模板 / 提示词")
    if rc != 0:
        say(f"首次提交失败：{out.strip()[:200]}")
    else:
        say("已初始化 git 仓库")


def git_cleanup_lock(work: Work) -> None:
    """被硬杀时留下的 index.lock 会让之后所有提交失败，且报错像"仓库坏了"。"""
    lock = work.root / ".git" / "index.lock"
    if not lock.exists():
        return
    rc, out = git(work, "status", "--porcelain")
    if rc == 0:
        return
    if "index.lock" in out or rc != 0:
        try:
            lock.unlink()
            say("清掉遗留的 .git/index.lock")
        except Exception:
            pass


EXCLUDE_DIRS = {".git", "artifacts", "__pycache__", "node_modules", ".venv", "dist", "build"}


def snapshot(work: Work) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in work.root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(work.root)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        try:
            out[str(rel)] = p.stat().st_mtime_ns
        except OSError:
            pass
    return out


def git_checkpoint(work: Work, before: dict[str, int], role: str, task: str, sid: str) -> None:
    """异常退出兜底：只提交这次会话真正碰过的路径，不碰别人的未提交改动。"""
    after = snapshot(work)
    changed = [k for k, v in after.items() if before.get(k) != v]
    if not changed:
        return
    for rel in changed:
        git(work, "add", "--", rel)
    rc, _ = git(work, "diff", "--cached", "--quiet")
    if rc == 0:
        return
    git(work, "commit", "-q", "-m", f"[checkpoint] {role} {task} 异常退出 (sid {sid[:8]})")
    say(f"checkpoint: {len(changed)} 个文件")


# ---------------------------------------------------------------- 命令

def cmd_bootstrap(work: Work, loom: Loom, objective: str) -> None:
    work.ensure()
    work.bootstrap_objective(objective)
    git_init(work)
    work.log_human(objective, via="-p(初始目标)")
    write_text(work.loomhash, json.dumps(work.hash_watched(), ensure_ascii=False))
    say(f"工作区就绪：{work.root}")
    loom.planner("bootstrap")


def cmd_continue(work: Work, loom: Loom, notes: list[str]) -> int:
    work.ensure()
    git_cleanup_lock(work)

    for n in notes:
        work.log_human(n)
        say(f"记录人的指令：{n}")
    loom.pending_human = list(notes)
    work.diff_and_record_human_edits()

    # 假活识别：running 但 pid 不在了 → interrupted
    for tdir in sorted(work.tasks.glob("*/")):
        task = Task(work, tdir.name)
        st = task.st()
        if st.get("status") != "running":
            continue
        if pid_alive(st.get("pid"), st.get("pid_start")):
            # 上一任 loom 已经没了，这个子进程没人看着；先杀掉，免得两个进程写同一个会话
            say(f"{task.id}: 上一任的子进程 {st.get('pid')} 还在，先杀掉再续接")
            kill_pid(st.get("pid"))
        task.save(status="interrupted", pid=None, pid_start=None)
        say(f"{task.id}: 标记为 interrupted（上一任 loom 已不在）")

    last = work.last_session()
    if last and not last["event"].startswith("exit"):
        say(f"接回 {last['role']} 会话 {last['sid'][:8]}…（{last['task']}，最后事件 {last['event']}）")
        if last["role"] == "worker":
            loom.run_task(last["task"], resumed_sid=last["sid"])
        else:
            note = work.human_note(notes)
            rc, _ = loom.run_plain(last["sid"], prompt("recovery", human_note=note),
                                   WRAPUP_GRACE, resume=True, role=last["role"])
            work.log_session(last["sid"], last["role"], "-", f"exit({rc})")
    else:
        say("没有在跑的会话 → 直接让 planner 重新判断")
        loom.planner("round", fact="人可能改过 OBJECTIVE/STATE/task.md，或队列为空；请重读后决定下一步")
    return loom.main_loop(100)


def cmd_status(work: Work) -> int:
    if not work.exists():
        print(f"没有工作区：{work.root}")
        return 1
    project, next_task = work.global_state()
    print(f"工作区 : {work.root}")
    print(f"PROJECT: {project}   next_task: {next_task or '-'}")
    print("\n任务：")
    rows = sorted(work.tasks.glob("*/"))
    if not rows:
        print("  （无）")
    for tdir in rows:
        t = Task(work, tdir.name)
        st = t.st()
        mark = f"summary={t.summary_status()}" if t.has_summary() else "summary=无"
        print(f"  {t.id:<26} {st.get('status', '?'):<22} {mark:<18} attempts={st.get('attempts', 0)}")
    print("\n最近的会话：")
    for line in read_text(work.sessions).splitlines()[-8:]:
        print("  " + line)
    if work.human.exists():
        hl = read_text(work.human).splitlines()[-5:]
        if hl:
            print("\n人说过的话（最近）：")
            for line in hl:
                print("  " + line)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Loom 驱动器")
    ap.add_argument("-p", dest="human", action="append", default=[], metavar="TEXT",
                    help="首次运行：目标；已有工作区：人的介入指令（可重复）")
    ap.add_argument("-c", dest="continue_", action="store_true", help="接回上次中断的位置")
    ap.add_argument("--status", action="store_true", help="打印状态后退出")
    ap.add_argument("--workdir", default="work", help="工作区目录（默认 ./work）")
    ap.add_argument("--claude", default=os.environ.get("LOOM_CLAUDE", "claude"))
    ap.add_argument("--claude-arg", action="append", default=[], dest="claude_args",
                    help="透传给 claude 的参数，可重复")
    args = ap.parse_args()

    extra = args.claude_args or os.environ.get("LOOM_CLAUDE_ARGS", "").split()
    claude_args = extra or ["--dangerously-skip-permissions", "--effort", "max"]

    work = Work(Path(args.workdir))
    loom = Loom(work, args.claude, claude_args)

    def on_sigint(signum, frame):
        global STOP
        STOP = True
        say("收到 Ctrl+C，正在让子进程优雅退出…")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, on_sigint)

    try:
        if args.status:
            return cmd_status(work)
        # 先确认"能不能起会话"，别等骨架建完、git 提交完才在第一次 spawn 时炸
        argv0, _batch = resolve_launcher(args.claude)
        say(f"启动器：{argv0[0]}" + ("（批处理，经 cmd.exe 调用）" if _batch else ""))
        if args.continue_:
            return cmd_continue(work, loom, args.human)
        if not args.human:
            ap.error('首次运行需要 -p "目标"；续跑用 -c')
        if work.exists():
            say("工作区已存在：本次 -p 按【人的介入】处理，不修改 OBJECTIVE.md")
            return cmd_continue(work, loom, args.human)
        cmd_bootstrap(work, loom, "\n".join(args.human))
        return loom.main_loop(100)
    except CliError as exc:
        say(str(exc))
        return 2
    except KeyboardInterrupt:
        print()
        say("已停止。改完文件后用 `loom.py -c` 续跑（-c -p \"…\" 可以带一句话）。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
