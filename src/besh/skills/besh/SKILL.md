---
name: besh
description: 用命令行远程操作"闲置算力"里的 tmux 窗口。当需要把计算任务交给手机 Termux、云 IDE、云 Notebook 等没有公网 IP 的机器执行，或需要查看/注入这些机器上长任务的屏幕输出时使用（关键词：besh、信标、工人、worker、远程 tmux、闲置算力）。
---

# besh —— Beacon Shell 命令行技能

besh 把没有公网 IP 的机器（手机 Termux、免费云 IDE、云 Notebook）变成"工人"，
它们主动向有公网 IP 的"信标"轮询命令，命令在本机 tmux 窗口里执行。
你（Agent）扮演"遥控器"，用 tmux 风格的命令操作多个工人。

## 前提

信标地址和密钥通过环境变量提供（通常已经设置好）：

```bash
export besh_url=http://BEACON-HOST:12242?secret=PASSWORD
# 或者分开设置
export besh_secret=PASSWORD
```

先确认能连上：

```bash
besh ls
```

## 核心命令

```bash
# 1. 看有哪些工人（= tmux list-sessions）
besh ls

# 2. 看工人身上有哪些窗口
besh list-windows -t WORKER_ID

# 3. 执行一条命令并拿到输出与退出码（最常用）
besh run -t WORKER_ID "python train.py --epochs 3"

# 4. 看窗口现在的屏幕（-S 50 表示往回抓 50 行）
besh capture-pane -t WORKER_ID:main -p -S 50

# 5. 往窗口里打字/发按键（窗口不存在会自动创建）
besh send-keys -t WORKER_ID:main "ls -l" Enter
besh send-keys -t WORKER_ID:main -l 'echo "Hello $USER"' Enter
besh type -t WORKER_ID:main "hello world"      # 文本原样输入并回车

# 6. 窗口管理
besh new-window -t WORKER_ID:gpu
besh kill-window -t WORKER_ID:gpu
besh kill-worker -t WORKER_ID

# 7. 查看状态
besh info -t WORKER_ID
besh history -t WORKER_ID -n 20
```

## 对象存储（工作目录备份）

工人的工作目录会定时同步到 Cloudflare R2：`cf:besh/YYYY-MM-DD/WORKER_ID/*`。

```bash
besh backup  -t WORKER_ID                       # 立刻备份
besh files   -t WORKER_ID --date 2026-11-11     # 列出备份文件
besh download -t WORKER_ID out/result.json -o ./result.json
besh upload  -t WORKER_ID ./data.csv data.csv
besh restore -t WORKER_ID --date 2026-11-11     # 恢复工作目录
```

## 判断规则

- 需要**输出/退出码**时用 `besh run`；需要**保持交互**（REPL、top、vim）时用
  `besh send-keys` + `besh capture-pane`。
- `besh run` 靠往 shell 里插入随机哨兵来判断命令结束，因此**不要在同一个窗口
  同时跑两条 `run`**，也不要在 vim/less 等全屏程序里用它。
- 长任务用 `besh send-keys -t W:main -l "nohup make > build.log 2>&1 &" Enter`，
  之后用 `besh run -t W "tail -n 20 build.log"` 查进度。
- 工人离线（`besh ls` 显示 offline）时命令会排队，等它上线后自动执行；
  急的话用 `besh kill-worker -t W` 清掉队列并让工人退出。
- `-t` 的目标格式是 `工人ID` 或 `工人ID:窗口名`，不带窗口名时用当前活动窗口。

## MCP 方式（可选）

信标的每个函数同时也是 MCP 工具，可直接接入：

```
besh mcp                     # 打印 MCP 配置
besh mcp --write ~/.workbuddy/mcp.json
```

常用工具名：`list_workers`、`list_windows`、`capture_pane`、`run`、`type_text`、
`send_keys`、`new_window`、`kill_window`、`backup`、`storage_files`、`storage_link`。

## 出错时

| 现象 | 处理 |
| --- | --- |
| `信标拒绝了请求（HTTP 401）` | 密钥不对，检查 `besh_url` / `besh_secret` |
| `连接不上信标` | 地址/端口不对，或信标没启动 |
| `工人 W 没有响应` | 工人离线；`besh ls` 看状态，等它上线或重启 `besh worker` |
| `命令在 Ns 内没有结束` | 命令进了交互式程序；改用 `send-keys` + `capture-pane` |
| `未安装 rclone` | 备份功能不可用，但不影响命令执行 |
