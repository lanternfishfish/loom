# Loom

一个共享目录 + 两个角色 + 一个时钟。与领域无关的 agent 长时间自治框架。

名字取自织机：经线是张紧不动的框架（`CLAUDE.md` / `OBJECTIVE.md`），纬线是一次次会话，
梭子穿过一趟就多一行布（`log.md` / `summary.md`）。线断了打个结继续织（`## 断点`），
不会有哪一次断线把整匹布废掉。一台织机同时只有一个人踩（单一写者）。

设计与取舍见 [`docs/DESIGN.md`](docs/DESIGN.md)。本目录是它的实现。

## 快速上手

```bash
# 首次：给一个目标，它自己建骨架、初始化 git、拆任务、开跑
python loom.py -p "把 X 做成 Y"

# 中途看状态
python loom.py --status

# 人介入：Ctrl+C 停下 → 改文件 → 续跑（可以带一句话）
python loom.py -c
python loom.py -c -p "这题别打了，转去搞 web-02"
```

默认工作区是 `./work`，用 `--workdir` 改。

## 它做什么，不做什么

做：起会话、记流水、跑时钟、写机器事实、判断"有没有 summary"。

不做：不解析自然语言、不做策略、不做质量验收。**planner 判"成没成"，loom 只判"有没有文件"。**

## 目录

```
loom.py                驱动器（单文件，只用标准库）
prompts/*.txt          7 个提示词：planner 启动 / planner 每轮 / worker / 收尾 ×2 / 恢复 / 提醒
templates/             建工作区时铺下去的骨架：CLAUDE.md、STATE.md、task.md、gitignore
docs/DESIGN.md         设计与取舍（为什么是这两个角色、为什么只有这些流程）
tests/fake_claude.py   假 claude：零成本验证流程
tests/dry_run.sh       正常路径干跑
tests/scenario_kill_resume.sh  kill -9 后续跑
```

## 跑起来之后，`work/` 长这样

```
work/
  CLAUDE.md          宪法（每会话必读）
  OBJECTIVE.md       目标 + 验收标准（只有人能改）
  STATE.md           任务表 + next_task + 决策日志（planner 独占写）
  human.log          人说过的话（loom 写，planner 读）
  sessions.log       会话流水（loom 写，append-only）
  tasks/task-NN-*/
    task.md          契约，写一次后冻结
    log.md           过程，worker 只追加
    summary.md       结论，≤30 行，带 status:
    .state           机器事实（sid / pid / status / attempts），loom 独占写
    solve/ artifacts/
```

## 时钟

预算写在 `task.md` 的 `budget_min`（planner 定，默认 30）。剩余 50% / 25% / 5% 各提醒一次，归零后收尾。
`claude -p` 没有注入通道，所以**提醒 = 杀进程 + `--resume` 同一个 session**——这才是"每任务固定 session id"的真正理由。

## 中断与恢复

两种中断（人按 Ctrl+C / 机器崩了）共用一套：`-c` 取 `sessions.log` 最后一条未退出的会话去 resume。

- `.state` 的 pid 还活着但 loom 已经没了 → 那是孤儿，**先杀掉再续接**（避免两个进程写同一个会话）
- pid 不在了 → 标记 `interrupted`，照样续接
- 人的话、停机期间被改过的文件，都会落进 `human.log`，所以下几任也看得见

## Windows：为什么要在 cmd 里绕一圈

`python loom.py -p "..."` 第一次跑很可能直接炸在 `FileNotFoundError: [WinError 2]`。
**和你在哪个目录下跑无关**，也和路径无关——原因是 npm 装的 `claude` 不是一个 exe：

| 文件 | 是什么 |
|---|---|
| `claude`（无扩展名） | 给 Git Bash 用的 `#!/bin/sh` 脚本 |
| `claude.cmd` | 给 cmd / PowerShell 用的批处理壳 |
| `claude.exe` | **不存在** |

`subprocess.Popen` 在 Windows 上不带 `shell=True` 时走 `CreateProcess`，**不会按 PATHEXT 去找 `.cmd`**，
所以 `["claude", ...]` 必然找不到。正确姿势是经 `cmd.exe /d /s /c` 转一手，而这一步的引号规则是个坑：

```
cmd /d /s /c ""C:\path\claude.cmd" --session-id xxx -p"
                ↑↑                        ↑
                两个引号                  一个引号，cmd 会剥掉首尾各一个
```

写成 `"""path"` 或 `""path args"` 都会让 cmd 把整串当成一个文件名，报
`'""C:\...\claude.cmd" --args' is not recognized`。

同时 **prompt 一律走 stdin**（`cat 1.txt | claude -p`），命令行里只留 uuid 和开关。
这样中文、换行、引号都不再经过 cmd 和 CRT 的任何一层转义。副作用是 `--claude` 可以直接写
`claude`（会自己 `shutil.which` 到 `claude.CMD`），也可以用 `--claude <完整路径>` 或环境变量
`LOOM_CLAUDE` 指定。

杀进程同理：Windows 上 `claude.cmd` 是 `cmd.exe` 的子壳，`node` 又是它的子进程，
只 kill 外层会留下没人管的 node，所以用 `taskkill /PID <pid> /T /F` 杀整棵树。

> 这套逻辑在 `tests/fake_claude.cmd` 上有专门的回归：它就是一个批处理壳，
> 用 `--claude <路径>\fake_claude.cmd` 跑 `dry_run.sh` 就能验证，不花 token。

## 自测（不花 token）

```bash
cd tests
bash dry_run.sh                      # 正常闭环：bootstrap → worker → DONE
FAKE_MODE=fail_summary bash dry_run.sh   # 不写 summary → 收尾复用原会话
FAKE_MODE=crash bash dry_run.sh          # exit 137 → checkpoint 兜底提交
FAKE_MODE=slow FAKE_BUDGET_MIN=0.1 bash dry_run.sh   # 6 秒预算 → 三级提醒 + 收尾
FAKE_MODE=stall bash dry_run.sh          # planner 空转 → 3 轮后停下
bash scenario_kill_resume.sh             # kill -9 loom 自己 → -c 接回
```

## 现在还没做

并行（单 worker 串行）、成本记账、跨项目记忆、运行中注入通道、多机同步。
不做是有意的，理由在设计的第十一节。
