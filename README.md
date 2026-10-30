# Loom

**轻量级 loop engineering 方案，主要用在渗透测试场景。**

一个单文件驱动器 + 两个角色 + 一个共享目录，让 agent 在同一个目标上连续跑几个小时，
上下文用完了自己续上，人想改方向随时能插一句。

> 名字取自织机：经线是张紧不动的框架（`CLAUDE.md` / `OBJECTIVE.md` 冻结），纬线是一次次会话，
> 梭子穿过一趟布上就多一行（`log.md` / `summary.md`）。线断了打个结继续织（`## 断点`）——
> 不会有哪一次断线把整匹布废掉。一台织机同时只有一个人踩，这就是单一写者。

设计与取舍见 [`docs/DESIGN.md`](docs/DESIGN.md)。

## 它解决的问题

单个 agent 会话干不了长活，原因不在模型不够聪明，在这三件事：

| 现实 | 后果 |
|---|---|
| 上下文有上限 | 跑到一半就装不下了，前面踩过的坑全忘 |
| 规划和执行抢同一份上下文 | 规划要全局视野，执行要细节，塞在一起两边都不够用 |
| 会话会断 | 超时、限额、机器重启、按错 Ctrl+C，断一次前功尽弃 |

loom 的做法很土：**把状态从会话里搬到文件里**。会话只负责"干一小段然后死掉"，谁死了都不影响全局。

## 为什么轻量

| | |
|---|---|
| 驱动器 | `loom.py`，958 行，单文件 |
| 依赖 | **零**（只用 Python 标准库） |
| 角色 | 2 个（planner / worker） |
| 提示词 | 7 个文件，合计 73 行 |
| 全局状态 | 1 个 `STATE.md`，上限 120 行 |
| 没有 | 调度器、插件系统、向量库、数据库、DAG 引擎、前端构建链 |

它不试图成为框架。**它只决定"谁在什么时候用什么上下文干活"**，其余全交给模型。

可选的 Web 面板在 `ui/`，和驱动器完全解耦：删掉它 `loom.py` 照常跑。

## 为什么适合渗透测试

不是硬套——是渗透测试的四个特征刚好对上这套设计：

- **天然串行。** 一次打一个目标、一个站、一台机器。不需要并发编排，也就不需要队列和 worker 池。
- **上下文烧得极快。** 大量响应包、payload、扫描输出。一个目标跑三小时，断点续跑不是加分项，是刚需。
- **方向经常变。** 人扫一眼流量就能看见一条捷径，或者发现某条路彻底死了。这时需要的是"停一下、说一句、继续"，不是实时注入通道。
- **中间产物就是证据。** dump、banner、hash、payload 本身就是交付物，落在文件里天经地义。

反过来，如果你要的是"一万个任务并发跑"，这玩意儿不合适，去看正经的编排框架。

## 快速上手

```bash
# 首次：给一个目标，它自己建骨架、初始化 git、拆任务、开跑
python loom.py -p "对 10.10.20.0/24 做一次内部渗透，目标是拿到域控上的 proof.txt"

# 中途看状态
python loom.py --status

# 人介入：Ctrl+C 停下 → 改文件 → 续跑（可以带一句话）
python loom.py -c
python loom.py -c -p "这台先放放，转去 web-02，那边有个上传点可能能打"
```

默认工作区是 `./work`，用 `--workdir` 改。

## 它做什么，不做什么

做：起会话、记流水、跑时钟、写机器事实、判断"有没有 summary"。

不做：**不解析自然语言、不做策略、不做质量验收。**

> planner 判"成没成"，loom 只判"有没有文件"。

这条边界是有意的。框架一旦开始判断"这个结果靠不靠谱"，它就会长出检查清单、阈值、评分表，
最后变成一个既不理解内容又妨碍模型的官僚机器。所以：**流程归代码，判断归模型。**

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

**单一写者**：每个文件只有一个角色能写。不靠锁，靠约定。三写者争一文件的问题在协议层就已经消失了。

## 两级接力

接力不是一次，是两次：

1. **任务之间** —— worker 写完 `summary.md` 退出 → planner 读所有 summary 和 `STATE.md` → 决定下一个任务。
   棒子就是 summary：不超过 30 行，只说"做成了什么、结论是什么、下一步该谁做什么"。
2. **任务之内** —— 一个任务跑不完一个上下文窗口。worker 觉得用掉一半时，在 `log.md` 末尾写 `## 断点`
   （当前假设 / 下一步的具体命令 / 已排除的路），然后退出。下一个会话接着做，任务状态不变。

第二种接力是这套东西最值钱的地方：**它让"任务比上下文长"成为常态而不是意外。**

## 时钟

预算写在 `task.md` 的 `budget_min`（planner 定，默认 30）。剩余 50% / 25% / 5% 各提醒一次，归零后收尾。

`claude -p` 没有注入通道，所以**提醒 = 杀进程 + `--resume` 同一个 session**——这才是"每任务固定 session id"的真正理由。
收尾也优先复用原会话（中间结果也是结果），只有它彻底叫不醒时才起新会话。

## 中断与恢复

两种中断（人按 Ctrl+C / 机器崩了）共用一套：`-c` 取 `sessions.log` 最后一条未退出的会话去 resume。

- `.state` 的 pid 还活着但 loom 已经没了 → 那是孤儿，**先杀掉再续接**（避免两个进程写同一个会话）
- pid 不在了 → 标记 `interrupted`，照样续接
- 人的话、停机期间被改过的文件，都会落进 `human.log`，所以下几任也看得见

**没有运行中注入通道**——能说话的前提是已经停了。多一条通道就多一个"人以为说过了、模型没听见"的失败面。

## Web 界面（可选）

`ui/loom_web.py` 起一个本地面板，同一个 `work/` 的另一个视图（零依赖，只用标准库）：

```bash
python ui/loom_web.py                        # 默认 ./work，http://127.0.0.1:8787
python ui/loom_web.py --workdir work --port 9000 --open
```

Windows 上还有个更省事的入口：双击 `ui/start.cmd`。它 `cd` 到仓库根、用 `./work`、起服务并开浏览器，
多给的参数会原样透传（`start.cmd --port 9000`）。就是一个批处理，删掉不影响别的。

**能看到什么**

左栏是一棵树，不是两个列表：**任务 → 会话**。一个任务下面挂它跑过的所有会话，时序从旧到新，
点三角展开；`.state` 里没有 task 记录的 planner 会话归到最下面的「编排」里。

| 区域 | 内容 |
|---|---|
| 左栏 | 任务树：每个任务的状态（优先看 `summary.md` 的 `status`，没有才退回 `.state`）、用时 / 预算 / 尝试次数，展开是它的会话序列 |
| 中栏 | 选中会话的**真实对话**——从 `~/.claude/projects/*/<sid>.jsonl` 读，含 thinking 和工具调用；另有任务契约 / 过程 / 总结 / 机器状态 / 运行日志 |
| 右栏 | `STATE.md` 与 `human.log` |
| 底部 | loom 自己的控制台输出，实时追加（没有输出时自己收起） |

视觉上是 macOS 那套：系统字体、浅灰底、细分隔线、圆角卡片、系统蓝做选中态；跟随系统深色。
没有框线、发光和终端配色——它是一个看板，不是终端。

URL 会跟着选中项走（`#task:task-02-explain-path`、`#task:<id>:<sid>`），刷新和贴给别人都能落回同一处。

面板本身**不存任何东西**：把它杀掉，`work/` 里的文件一个不少，重新拉起来看到的就是同一幕。
所谓"会话丢失"只会是服务停了，不会是数据没了——先看 `work/sessions.log` 还在不在。

**能做什么**：就两个按钮。

| 按钮 | 等价于 |
|---|---|
| 打断 | Ctrl+C |
| 继续 + 一句话 | `loom.py -c -p "……"` |

**它没有新增任何通道。** 页面上能做的事，终端里都能做，只是不用切窗口。打断之后输入框弹出来，
写一句、发送，就是 `-c -p`——照样落 `human.log`，下一任看得见。

三个限制，先说清楚：

- **只能打断它自己起的 loom**。你在终端里跑的实例它管不着，也不该偷偷去杀。
- 会话对话读的是 claude 自己的转录文件。换模型、换目录就没有了，页面会老实显示"找不到"。
- 默认只监听 `127.0.0.1`，没有登录。别把 `--host` 改成 `0.0.0.0`。

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

验收目标不是"能不能打穿"，是**接力断不断**。

## 现在还没做

并行（单 worker 串行）、成本记账、跨项目记忆、运行中注入通道、多机同步。
不做是有意的，理由在设计的第十一节。

## 边界

仅用于**授权范围内**的测试：自有靶场、CTF 比赛、有书面授权的客户目标。

这个项目不提供任何攻击能力。它只决定"谁在什么时候用什么上下文干活"，
具体做什么由你写给它的 `OBJECTIVE.md` 和模型自己决定。

## License

MIT
