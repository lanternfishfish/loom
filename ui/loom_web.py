#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Loom Web：一个页面看 work/ 的机器状态，并且在跑的时候能打断、能递一句话。

它不发明任何新通道。页面上那两个按钮，等价于终端里的：
  打断      = Ctrl+C
  带着话续跑 = Ctrl+C 之后 `loom.py -c -p "……"`

所有状态都从 work/ 里读，页面只是同一批文件的另一个视图。
它不写 work/ 下的任何文件，也不改 loom.py 的流程。

用法：
  python ui/loom_web.py                          # 默认 ./work，127.0.0.1:8787
  python ui/loom_web.py --workdir work --port 9000
  python ui/loom_web.py --claude tests/fake_claude.cmd   # 用假 claude 试界面
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # loom/ 仓库根
WEB = HERE / "web"
IS_WIN = os.name == "nt"


# ---------------------------------------------------------------- 读文件

def read_text(p: Path, default: str = "") -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return default


def read_json(p: Path) -> dict:
    try:
        return json.loads(read_text(p, "{}"))
    except Exception:
        return {}


def clip(s: str, n: int = 2000) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n…（截断，共 {len(s)} 字）"


def iso_ts(s) -> float | None:
    """loom 的 .state 里 started_at 是 float 时间戳，updated 是 ISO 字符串，两种都要认。"""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return float(s)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(s)).timestamp()
    except Exception:
        return None


def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        pid = int(pid)
    except Exception:
        return False
    if IS_WIN:
        try:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                               capture_output=True, text=True, timeout=6)
            return str(pid) in (r.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def kill_tree(pid) -> None:
    try:
        pid = int(pid)
    except Exception:
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def resolve_claude_spec(spec: str | None) -> str | None:
    """把 --claude 里的相对路径按仓库根解析成绝对路径。

    loom.py 起会话时 cwd 是 work/，所以 --claude "python tests/fake_claude.py"
    这种写法在 work/ 里会找不到文件。这里就地补全，顺便加引号防空格。
    """
    if not spec:
        return None
    parts = shlex.split(spec)
    if not parts:
        return None
    out = [parts[0]]                       # 第一个是可执行入口，交给 loom 自己找
    for p in parts[1:]:
        if ("/" in p or "\\" in p) and not os.path.isabs(p):
            cand = ROOT / p
            if cand.exists():
                out.append(f'"{cand.as_posix()}"')
                continue
        out.append(f'"{p}"' if " " in p else p)
    return " ".join(out)


# ---------------------------------------------------------------- 起停 loom

class Runner:
    """把一个 loom.py 进程管起来。同一时刻只允许一个。"""

    def __init__(self, work: Path, loom_py: Path, claude: str | None):
        self.work = work
        self.loom_py = loom_py
        self.claude = resolve_claude_spec(claude)
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.meta: dict = {}
        self.last: dict = {}
        self.rundir = work.parent / "loom-ui"
        self.console = self.rundir / "console.log"
        self._fh = None

    # -- 状态

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def reap(self) -> None:
        """子进程已经退出了就把 rc 记下来。调用方持锁。"""
        if self.proc is None:
            return
        rc = self.proc.poll()
        if rc is None:
            return
        ended = time.time()
        self.last = {
            "rc": rc,
            "mode": self.meta.get("mode"),
            "text": self.meta.get("text", ""),
            "started": self.meta.get("started"),
            "duration": round(ended - (self.meta.get("started") or ended), 1),
            "ended": ended,
            "argv": self.meta.get("argv", []),
        }
        self.proc = None
        self.meta = {}
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def state(self) -> dict:
        with self.lock:
            self.reap()
            if self.running():
                return {
                    "state": "running",
                    "pid": self.proc.pid,
                    "mode": self.meta.get("mode"),
                    "text": self.meta.get("text", ""),
                    "started": self.meta.get("started"),
                    "elapsed": round(time.time() - (self.meta.get("started") or time.time()), 1),
                    "console": str(self.console),
                }
            return {"state": "idle", "last": self.last, "console": str(self.console)}

    # -- 起

    def start(self, mode: str, text: str = "") -> tuple[bool, str]:
        with self.lock:
            self.reap()
            if self.running():
                return False, "已经有一个 loom 在跑，先打断它"

            argv = [sys.executable, str(self.loom_py), "--workdir", str(self.work)]
            if mode == "new":
                argv += ["-p", text]
            elif mode == "continue":
                argv += ["-c"]
                if text.strip():
                    argv += ["-p", text]
            else:
                return False, f"未知模式 {mode}"
            if self.claude:
                argv += ["--claude", self.claude]

            self.rundir.mkdir(parents=True, exist_ok=True)
            if self.console.exists():
                try:
                    self.console.replace(self.rundir / "console.prev.log")
                except Exception:
                    pass
            self._fh = open(self.console, "w", encoding="utf-8", errors="replace")
            self._fh.write(f"$ {' '.join(argv)}\n\n")
            self._fh.flush()

            flags = 0
            if IS_WIN:
                # 独立进程组：这样才能只给这一个 loom 发 CTRL_BREAK（= 打断）
                flags = subprocess.CREATE_NEW_PROCESS_GROUP

            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            try:
                self.proc = subprocess.Popen(
                    argv, cwd=str(ROOT), stdout=self._fh, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, creationflags=flags, env=env,
                )
            except Exception as exc:
                self._fh.close()
                self._fh = None
                return False, f"启动失败：{exc}"

            self.meta = {"mode": mode, "text": text, "started": time.time(), "argv": argv}
            return True, f"已启动 pid={self.proc.pid}"

    # -- 停

    def stop(self, wait_s: float = 30.0) -> tuple[bool, str]:
        with self.lock:
            self.reap()
            if not self.running():
                return False, "没有在跑的 loom"
            pid = self.proc.pid
            proc = self.proc

        sent = "CTRL_BREAK"
        try:
            if IS_WIN:
                os.kill(pid, signal.CTRL_BREAK_EVENT)
            else:
                os.kill(pid, signal.SIGINT)
        except Exception as exc:
            sent = f"信号失败（{exc}），改为强杀"
            kill_tree(pid)

        deadline = time.time() + wait_s
        while time.time() < deadline:
            if proc.poll() is not None:
                with self.lock:
                    self.reap()
                return True, f"已停止（{sent}，exit={proc.returncode}）"
            time.sleep(0.2)

        kill_tree(pid)
        time.sleep(0.5)
        with self.lock:
            self.reap()
        return True, "没在时限内退出，已连同子进程强杀"


# ---------------------------------------------------------------- 会话记录

def find_transcript(sid: str) -> Path | None:
    """claude 把会话落在 ~/.claude/projects/<slug>/<sid>.jsonl；按 sid 直接找。"""
    if not sid or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", sid):
        return None
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return None
    hits = list(base.glob(f"*/{sid}.jsonl"))
    if not hits:
        return None
    return max(hits, key=lambda p: p.stat().st_mtime)


def parse_transcript(path: Path, limit: int = 300) -> dict:
    out: list[dict] = []
    raw = read_text(path)
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") not in ("user", "assistant"):
            continue
        msg = d.get("message") or {}
        role = msg.get("role") or d.get("type")
        content = msg.get("content")
        blocks: list[dict] = []
        if isinstance(content, str):
            blocks.append({"k": "text", "text": clip(content, 4000)})
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    blocks.append({"k": "text", "text": clip(b.get("text", ""), 4000)})
                elif bt == "thinking":
                    blocks.append({"k": "think", "text": clip(b.get("thinking", ""), 1500)})
                elif bt == "tool_use":
                    blocks.append({
                        "k": "tool", "name": b.get("name") or "?",
                        "text": clip(json.dumps(b.get("input"), ensure_ascii=False), 1500),
                    })
                elif bt == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        txt = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                    else:
                        txt = c if isinstance(c, str) else str(c)
                    blocks.append({"k": "result", "text": clip(txt, 1500)})
        if blocks:
            out.append({"role": role, "ts": d.get("timestamp"), "blocks": blocks})
    return {"sid": path.stem, "total": len(out), "records": out[-limit:]}


# ---------------------------------------------------------------- 工作区视图

class View:
    def __init__(self, work: Path):
        self.work = work
        self.state = work / "STATE.md"
        self.tasks = work / "tasks"
        self.sessions = work / "sessions.log"
        self.human = work / "human.log"
        self.loomlog = work.parent / "loom.log"

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

    def task_rows(self) -> list[dict]:
        rows = []
        if not self.tasks.exists():
            return rows
        for d in sorted(p for p in self.tasks.iterdir() if p.is_dir()):
            st = read_json(d / ".state")
            summary = d / "summary.md"
            summ_status = "?"
            if summary.exists():
                m = re.search(r"^\s*status:\s*(\S+)", read_text(summary)[:600], re.M)
                summ_status = m.group(1).strip() if m else "?"
            status = st.get("status", "未开始")
            alive = pid_alive(st.get("pid")) if status == "running" else False
            # .state 写 running 但进程已经没了 = 上一任被打断/崩了。loom.py -c 也这么判，
            # 这里照着显示，不然界面上会出现"在跑"其实没人在跑的假象。
            if status == "running" and not alive:
                status = "interrupted"
            started = iso_ts(st.get("started_at"))
            updated = iso_ts(st.get("updated"))
            if status == "running" and started:
                duration = round(time.time() - started, 1)
            elif started and updated:
                duration = round(updated - started, 1)
            else:
                duration = None
            budget = 0
            contract = d / "task.md"
            m = re.search(r"^\s*budget_min:\s*([0-9.]+)", read_text(contract), re.M)
            if m:
                budget = round(float(m.group(1)) * 60)
            rows.append({
                "id": d.name,
                "status": status,
                "alive": alive,
                "attempts": int(st.get("attempts") or 0),
                "sid": st.get("sid") or "",
                "pid": st.get("pid"),
                "summary": summ_status if summary.exists() else None,
                "has_task": contract.exists(),
                "duration": duration,
                "budget": budget,
                "updated": st.get("updated"),
            })
        return rows

    def session_rows(self, limit: int = 200) -> list[dict]:
        rows = []
        for line in read_text(self.sessions).splitlines():
            parts = line.split("  ")
            if len(parts) < 5:
                continue
            rows.append({
                "ts": parts[0].strip(), "sid": parts[1].strip(),
                "role": parts[2].strip(), "task": parts[3].strip(),
                "event": "  ".join(parts[4:]).strip(),
            })
        return rows[-limit:][::-1]

    def overview(self, runner: Runner) -> dict:
        project, next_task = self.global_state()
        return {
            "work_exists": self.state.exists(),
            "workdir": str(self.work),
            "project": project,
            "next_task": next_task,
            "run": runner.state(),
            "tasks": self.task_rows(),
            "sessions": self.session_rows(),
            "now": time.time(),
        }

    # -- 白名单读文件

    FILE_ALLOW = re.compile(
        r"^(STATE\.md|OBJECTIVE\.md|CLAUDE\.md|human\.log|sessions\.log|"
        r"tasks/[A-Za-z0-9._-]+/(task\.md|log\.md|summary\.md|\.state))$"
    )

    def file(self, rel: str) -> dict:
        if rel == "loom.log":
            return {"path": rel, "text": clip(read_text(self.loomlog), 60000), "size": _size(self.loomlog)}
        if not self.FILE_ALLOW.match(rel):
            return {"path": rel, "error": "不在白名单内", "text": ""}
        p = self.work / rel
        return {"path": rel, "text": clip(read_text(p), 60000), "size": _size(p), "mtime": _mtime(p)}


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except Exception:
        return 0


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except Exception:
        return 0


# ---------------------------------------------------------------- HTTP

class App:
    def __init__(self, work: Path, runner: Runner):
        self.view = View(work)
        self.runner = runner

    def console_tail(self, offset: int) -> dict:
        p = self.runner.console
        try:
            size = p.stat().st_size
        except Exception:
            return {"offset": 0, "text": "", "size": 0}
        if offset > size:            # 文件被换掉了（新的一轮），从头给
            offset = 0
        if size - offset > 400_000:  # 落后太多就只给尾部
            offset = max(0, size - 400_000)
        try:
            with open(p, "rb") as f:
                f.seek(offset)
                chunk = f.read(400_000)
        except Exception:
            return {"offset": offset, "text": "", "size": size}
        return {"offset": offset + len(chunk), "text": chunk.decode("utf-8", "replace"), "size": size}


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        server_version = "loom-web/1.0"

        def log_message(self, fmt, *args):   # 不要把每个请求都打到终端
            pass

        # -- 工具

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _static(self, name: str, ctype: str):
            p = WEB / name
            if not p.exists():
                self._json({"error": "not found"}, 404)
                return
            self._send(200, p.read_bytes(), ctype)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return {}

        # -- 路由

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path

            if path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            if path == "/app.js":
                return self._static("app.js", "application/javascript; charset=utf-8")
            if path == "/app.css":
                return self._static("app.css", "text/css; charset=utf-8")

            if path == "/api/overview":
                return self._json(app.view.overview(app.runner))

            if path == "/api/console":
                off = int((q.get("off") or ["0"])[0] or 0)
                return self._json(app.console_tail(off))

            if path == "/api/file":
                rel = (q.get("p") or [""])[0]
                return self._json(app.view.file(rel))

            if path == "/api/transcript":
                sid = (q.get("sid") or [""])[0]
                p = find_transcript(sid)
                if not p:
                    return self._json({"sid": sid, "records": [], "total": 0,
                                       "error": "找不到这个会话的转录文件"})
                d = parse_transcript(p)
                d["file"] = str(p)
                return self._json(d)

            return self._json({"error": "no route"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            if u.path == "/api/run":
                b = self._body()
                mode = b.get("mode") or "continue"
                text = (b.get("text") or "").strip()
                if mode == "new" and not text:
                    return self._json({"ok": False, "msg": "新建需要先写目标"}, 400)
                ok, msg = app.runner.start(mode, text)
                return self._json({"ok": ok, "msg": msg}, 200 if ok else 409)

            if u.path == "/api/stop":
                ok, msg = app.runner.stop()
                return self._json({"ok": ok, "msg": msg}, 200 if ok else 409)

            return self._json({"error": "no route"}, 404)

    return H


def main() -> int:
    ap = argparse.ArgumentParser(description="Loom Web：看 work/ 的状态，并打断/续跑")
    ap.add_argument("--workdir", default="work", help="工作区目录（同 loom.py 的 --workdir）")
    ap.add_argument("--loom", default=str(ROOT / "loom.py"), help="驱动器路径")
    ap.add_argument("--claude", default=None, help="透传给 loom.py；不填用它自己的默认")
    ap.add_argument("--host", default="127.0.0.1", help="只监听本机，别随手改成 0.0.0.0")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--open", action="store_true", help="起来后自动开浏览器")
    args = ap.parse_args()

    work = Path(args.workdir).resolve()
    runner = Runner(work, Path(args.loom).resolve(), args.claude)
    app = App(work, runner)

    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    url = f"http://{args.host}:{args.port}/"
    print(f"Loom Web  {url}")
    print(f"  工作区  {work}{'' if work.exists() else '（还没有建，先在页面上填目标）'}")
    print(f"  驱动器  {args.loom}")
    print(f"  控制台  {runner.console}")
    print("  Ctrl+C 退出（只会退掉页面，正在跑的 loom 会一起被打断）")
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n退出。")
        runner.stop(wait_s=15)
    return 0


if __name__ == "__main__":
    sys.exit(main())
