# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""besh 命令行（遥控器 + 信标 + 工人）。

设计目标：对 tmux 用户来说零学习成本——session 换成 worker，
其余命令（list-windows / capture-pane / send-keys）保持不变。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .__about__ import __version__
from .client import Beacon
from .target import parse_target
from .util import (
    BeshError,
    DEFAULT_PORT,
    DEFAULT_SYNC_INTERVAL,
    getenv,
    human_size,
    human_time,
    shorten,
)


# --------------------------------------------------------------------- 输出工具


class Out:
    def __init__(self, as_json: bool = False):
        self.as_json = as_json

    def raw(self, data: Any) -> None:
        """结构化输出（--json）。"""
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))

    def table(self, headers: List[str], rows: List[List[Any]]) -> None:
        if not rows:
            print("(空)")
            return
        widths = [len(h) for h in headers]
        cells = [[("" if c is None else str(c)) for c in row] for row in rows]
        for row in cells:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], _display_width(cell))
        line = "  ".join(_pad(h, widths[i]) for i, h in enumerate(headers))
        print(line.rstrip())
        print("  ".join("-" * widths[i] for i in range(len(headers))))
        for row in cells:
            print("  ".join(_pad(c, widths[i]) for i, c in enumerate(row)).rstrip())

    def fail(self, msg: str) -> int:
        print(msg, file=sys.stderr)
        return 1


def _display_width(text: str) -> int:
    """中文按 2 列算，表格才对齐。"""
    width = 0
    for ch in text:
        width += 2 if ord(ch) > 0x2E80 else 1
    return width


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _unwrap(ret: Any, out: Out) -> Any:
    """统一处理 {"suc":bool,"msg":...} 与裸返回值。"""
    if isinstance(ret, dict) and "suc" in ret and not ret.get("suc"):
        msg = ret.get("msg") or ret.get("data") or "操作失败"
        raise BeshError(str(msg))
    return ret


# --------------------------------------------------------------------- 子命令


def cmd_serve(args: argparse.Namespace) -> int:
    from .serve import serve

    return serve(
        base_dir=args.dir,
        host=args.host,
        port=args.port,
        secret=getattr(args, "secret", None),
        reset=args.reset,
        force_web=args.force_web,
        no_run=args.no_run,
    )


def cmd_worker(args: argparse.Namespace) -> int:
    from .worker import run_worker

    return run_worker(
        args.worker_id,
        beacon(args),
        workdir=args.dir,
        sync=not args.no_sync,
        interval=args.interval,
        pane_interval=args.pane_interval,
        first_window=args.window,
        poll_wait=args.wait,
    )


def cmd_sync_loop(args: argparse.Namespace) -> int:
    from .worker import sync_loop

    sync_loop(args.dir, args.worker, args.interval)
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    out = Out(args.json)
    ret = _unwrap(beacon(args).call("list_workers"), out)
    rows = ret.get("data") or []
    if out.as_json:
        out.raw(rows)
        return 0
    if not rows:
        print("还没有工人。在闲置算力上运行：besh worker WORKER_ID")
        return 0
    table = []
    for row in rows:
        table.append(
            [
                row.get("worker"),
                "online" if row.get("online") else "offline",
                row.get("host") or "-",
                shorten(row.get("cwd") or "-", 28),
                ",".join(row.get("windows") or []) or "-",
                human_time(row.get("seen_at")),
            ]
        )
    out.table(["WORKER", "STATUS", "HOST", "CWD", "WINDOWS", "SEEN"], table)
    return 0


def cmd_windows(args: argparse.Namespace) -> int:
    out = Out(args.json)
    target = parse_target(args.target)
    ret = _unwrap(
        beacon(args).call("list_windows", worker=target.worker, refresh=True, timeout=args.timeout),
        out,
    )
    rows = ret.get("data") or []
    if out.as_json:
        out.raw(rows)
        return 0
    if not rows:
        print(f"工人 {target.worker} 没有窗口（或离线）")
        return 1
    table = []
    for row in rows:
        table.append(
            [
                row.get("name"),
                "yes" if row.get("active") else "-",
                row.get("command") or "-",
                shorten(row.get("path") or "-", 30),
                row.get("size") or "-",
            ]
        )
    print(f"{target.worker} · {len(rows)} windows ({'live' if ret.get('live') else 'cached'})")
    out.table(["NAME", "ACTIVE", "CMD", "PATH", "SIZE"], table)
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    target = parse_target(args.target)
    ret = _unwrap(
        beacon(args).call(
            "capture_pane",
            worker=target.worker,
            window=target.window,
            lines=args.start,
            escapes=args.escapes,
            refresh=not args.cached,
        ),
        Out(),
    )
    content = ret.get("content") or ""
    if args.json:
        print(json.dumps(ret, ensure_ascii=False, indent=2, default=str))
        return 0
    if not content:
        print(f"工人 {target.worker} 没有返回屏幕内容（离线？）", file=sys.stderr)
        return 1
    sys.stdout.write(content if content.endswith("\n") else content + "\n")
    return 0


def cmd_send_keys(args: argparse.Namespace) -> int:
    target = parse_target(args.target)
    keys = list(args.keys or [])
    if args.literal:
        keys = [" ".join(keys)]
    ret = _unwrap(
        beacon(args).call(
            "send_keys",
            worker=target.worker,
            window=target.window,
            keys_json=json.dumps(keys, ensure_ascii=False),
            literal=args.literal,
            wait=args.wait,
        ),
        Out(),
    )
    if args.json:
        print(json.dumps(ret, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_type(args: argparse.Namespace) -> int:
    target = parse_target(args.target)
    text = " ".join(args.text)
    ret = _unwrap(
        beacon(args).call(
            "type_text",
            worker=target.worker,
            window=target.window,
            text=text,
            enter=not args.no_enter,
            wait=args.wait,
        ),
        Out(),
    )
    if args.json:
        print(json.dumps(ret, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    target = parse_target(args.target)
    command = " ".join(args.command)
    ret = beacon(args).call(
        "run", worker=target.worker, window=target.window, command=command, timeout=args.timeout
    )
    if args.json:
        print(json.dumps(ret, ensure_ascii=False, indent=2, default=str))
        return 0 if ret.get("suc") else 1
    content = ret.get("output") or ""
    if content:
        sys.stdout.write(content if content.endswith("\n") else content + "\n")
    if not ret.get("suc"):
        print(f"! {ret.get('msg')}", file=sys.stderr)
        return 1
    return int(ret.get("exit_code") or 0)


def cmd_new_window(args: argparse.Namespace) -> int:
    target = parse_target(args.target)
    name = args.name or (target.window or "main")
    command = " ".join(args.command or [])
    ret = _unwrap(
        beacon(args).call("new_window", worker=target.worker, window=name, command=command), Out()
    )
    if args.json:
        print(json.dumps(ret, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"已创建 {target.worker}:{name}")
    return 0


def cmd_kill_window(args: argparse.Namespace) -> int:
    target = parse_target(args.target)
    if not target.window:
        raise BeshError("请用 -t WORKER:WINDOW 指定要关闭的窗口")
    ret = _unwrap(beacon(args).call("kill_window", worker=target.worker, window=target.window), Out())
    if args.json:
        print(json.dumps(ret, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"已关闭 {target.worker}:{target.window}")
    return 0


def cmd_kill_worker(args: argparse.Namespace) -> int:
    target = parse_target(args.target)
    ret = _unwrap(beacon(args).call("kill_worker", worker=target.worker), Out())
    print(ret.get("msg") or "已通知工人退出")
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    if args.target:
        target = parse_target(args.target)
        ret = beacon(args).call(
            "wait_idle",
            worker=target.worker,
            window=target.window,
            timeout=args.timeout,
            _timeout=args.timeout + 30,
        )
        if args.json:
            print(json.dumps(ret, ensure_ascii=False, indent=2, default=str))
            return 0 if ret.get("suc") else 1
        content = ret.get("content") or ""
        if content:
            sys.stdout.write(content if content.endswith("\n") else content + "\n")
        if not ret.get("suc"):
            print(f"! {ret.get('msg')}", file=sys.stderr)
            return 1
        return 0
    if not args.cmd_id:
        print("! 需要 -t WORKER:WINDOW（等窗口空闲）或命令 ID（等命令回执）", file=sys.stderr)
        return 2
    ret = beacon(args).call(
        "wait_cmd", cmd_id=args.cmd_id, timeout=args.timeout, _timeout=args.timeout + 30
    )
    if args.json:
        print(json.dumps(ret, ensure_ascii=False, indent=2, default=str))
        return 0 if ret.get("suc") else 1
    data = ret.get("data") or {}
    out = data.get("output") or ""
    if out:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")
    if not ret.get("suc"):
        print(f"! {ret.get('msg')}", file=sys.stderr)
        return 1
    return int(data.get("exit_code") or 0)


def cmd_history(args: argparse.Namespace) -> int:
    out = Out(args.json)
    worker = args.target or ""
    if worker:
        worker = parse_target(worker).worker
    ret = _unwrap(beacon(args).call("command_history", worker=worker, limit=args.limit), out)
    rows = ret.get("data") or []
    if out.as_json:
        out.raw(rows)
        return 0
    out.table(
        ["TIME", "WORKER", "KIND", "STATUS", "EXIT", "OUTPUT"],
        [
            [
                (r.get("created_at") or "").replace("T", " "),
                r.get("worker"),
                r.get("kind"),
                r.get("status"),
                r.get("exit_code"),
                shorten(r.get("output") or "", 40),
            ]
            for r in rows
        ],
    )
    return 0


# --------------------------------------------------------------------- 对象存储


def cmd_files(args: argparse.Namespace) -> int:
    out = Out(args.json)
    target = parse_target(args.target)
    ret = _unwrap(
        beacon(args).call("storage_files", worker=target.worker, date=args.date, path=args.path),
        out,
    )
    rows = ret.get("data") or []
    if out.as_json:
        out.raw(ret)
        return 0
    print(f"{ret.get('remote')}  ({len(rows)} files)")
    out.table(
        ["SIZE", "MTIME", "PATH"],
        [[human_size(r.get("size")), r.get("mtime") or "-", r.get("path")] for r in rows],
    )
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    target = parse_target(args.target)
    remote = args.path
    local = args.output or os.path.basename(remote.rstrip("/")) or "download"
    client = beacon(args)
    # 优先用预签名直链：浏览器/curl 直连对象存储，不吃信标带宽
    link = client.call("storage_link", worker=target.worker, path=remote, date=args.date)
    if isinstance(link, dict) and link.get("suc") and link.get("url"):
        _http_download(link["url"], local)
        print(f"已下载 {local}（{human_size(os.path.getsize(local))}）")
        return 0
    ret = client.call("storage_get", worker=target.worker, path=remote, date=args.date)
    _unwrap(ret, Out())
    data = base64.b64decode(ret.get("b64") or "")
    os.makedirs(os.path.dirname(os.path.abspath(local)) or ".", exist_ok=True)
    with open(local, "wb") as f:
        f.write(data)
    print(f"已下载 {local}（{human_size(len(data))}）")
    return 0


def _http_download(url: str, local: str) -> None:
    import urllib.request

    os.makedirs(os.path.dirname(os.path.abspath(local)) or ".", exist_ok=True)
    with urllib.request.urlopen(url, timeout=300) as resp, open(local, "wb") as f:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)


def cmd_upload(args: argparse.Namespace) -> int:
    target = parse_target(args.target)
    local = args.local_file
    if not os.path.isfile(local):
        raise BeshError(f"本地文件不存在: {local}")
    remote = args.remote or os.path.basename(local)
    size = os.path.getsize(local)
    if size > 16 * 1024 * 1024:
        raise BeshError(
            f"文件 {human_size(size)} 过大（base64 通道上限 16MB）。"
            f"请在工人上直接运行 rclone，或先拆分文件。"
        )
    with open(local, "rb") as f:
        payload = base64.b64encode(f.read()).decode("ascii")
    ret = beacon(args).call(
        "storage_put", worker=target.worker, path=remote, b64=payload, date=args.date
    )
    _unwrap(ret, Out())
    print(f"已上传 {local} → {ret.get('remote')}（{human_size(size)}）")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    target = parse_target(args.target)
    ret = beacon(args).call(
        "storage_delete", worker=target.worker, path=args.path, date=args.date
    )
    _unwrap(ret, Out())
    print(f"已删除 {args.path}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    target = parse_target(args.target)
    ret = beacon(args).call("backup", worker=target.worker, wait=args.timeout)
    if args.json:
        print(json.dumps(ret, ensure_ascii=False, indent=2, default=str))
        return 0 if ret.get("suc") else 1
    _unwrap(ret, Out())
    print(ret.get("output") or "已触发备份")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    target = parse_target(args.target)
    ret = beacon(args).call(
        "restore", worker=target.worker, date=args.date, path=args.path, wait=args.timeout
    )
    if args.json:
        print(json.dumps(ret, ensure_ascii=False, indent=2, default=str))
        return 0 if ret.get("suc") else 1
    _unwrap(ret, Out())
    print(ret.get("output") or "已恢复")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    out = Out(args.json)
    target = parse_target(args.target)
    client = beacon(args)
    workers = _unwrap(client.call("list_workers"), out).get("data") or []
    row = next((w for w in workers if w.get("worker") == target.worker), None)
    if not row:
        raise BeshError(f"没有这个工人: {target.worker}")
    windows = _unwrap(
        client.call("list_windows", worker=target.worker, refresh=args.refresh, timeout=6), out
    )
    info = _unwrap(client.call("storage_info"), out)
    if out.as_json:
        out.raw({"worker": row, "windows": windows, "storage": info})
        return 0
    print(f"worker   : {row.get('worker')}  ({'online' if row.get('online') else 'offline'})")
    print(f"host     : {row.get('host') or '-'}")
    print(f"workdir  : {row.get('cwd') or '-'}")
    print(f"version  : {row.get('version') or '-'}")
    print(f"started  : {row.get('started_at') or '-'}")
    print(f"seen     : {human_time(row.get('seen_at'))}")
    print(f"sync     : {row.get('sync') or '-'}")
    print(f"pending  : {row.get('pending')}")
    print(f"storage  : {info.get('remote')} (rclone {'ok' if info.get('rclone') else '缺失'})")
    names = [w.get("name") for w in (windows.get("data") or [])]
    print(f"windows  : {', '.join(names) if names else '-'}")
    return 0


# --------------------------------------------------------------------- 集成配置


def cmd_mcp(args: argparse.Namespace) -> int:
    client = beacon(args)
    config = {
        "mcpServers": {
            "besh": {
                "type": "http",
                "url": f"{client.base}/mcp",
                "headers": {"Authorization": f"Bearer {client.secret}"},
            }
        }
    }
    if not args.write:
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return 0
    path = os.path.expanduser(args.write)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    data: Dict[str, Any] = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except (OSError, ValueError):
            data = {}
    data.setdefault("mcpServers", {}).update(config["mcpServers"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"已写入 {path}")
    print("重启客户端后生效；如果客户端要求先「信任」该服务，请到连接器页面点一下信任。")
    return 0


def cmd_skill(args: argparse.Namespace) -> int:
    try:
        from importlib.resources import files

        path = files("besh").joinpath("skills/besh/SKILL.md")
        text = path.read_text(encoding="utf-8")
    except Exception:
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills", "besh", "SKILL.md")
        with open(here, "r", encoding="utf-8") as f:
            text = f.read()
    if not args.install:
        print(text)
        return 0
    folder = os.path.expanduser(args.install)
    if os.path.basename(folder.rstrip("/\\")) != "besh":
        folder = os.path.join(folder, "besh")
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, "SKILL.md")
    with open(target, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"已安装技能到 {target}")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"besh {__version__}")
    return 0


# --------------------------------------------------------------------- 入口


def beacon(args: argparse.Namespace) -> Beacon:
    return Beacon(url=getattr(args, "url", None), secret=getattr(args, "secret", None))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="besh",
        description="Beacon Shell —— 把闲置算力变成可远程操作的工人",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  export besh_secret=123456 && besh serve\n"
            "  export besh_url=http://1.2.3.4:12242?secret=123456\n"
            "  besh worker phone-1\n"
            "  besh ls\n"
            "  besh run -t phone-1:window-0 'uname -a'\n"
            "  besh capture-pane -t phone-1:window-0 -p -S 50\n"
            "  besh send-keys -t phone-1:window-0 'ls -l' Enter\n"
        ),
    )
    parser.add_argument("-u", "--url", default=getenv("besh_url"), help="信标地址（默认读 besh_url）")
    parser.add_argument("--secret", default=getenv("besh_secret"), help="信标密钥（默认读 besh_secret）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("-v", "--version", action="version", version=f"besh {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    p = subs.add_parser("serve", help="在当前目录启动信标（生成 besh_beacon.py + www/）")
    p.add_argument("--dir", default=os.getcwd(), help="工作目录（默认当前目录）")
    p.add_argument("--host", default=None, help="监听地址，默认 0.0.0.0")
    p.add_argument("--port", type=int, default=None, help=f"监听端口，默认 {DEFAULT_PORT}")
    p.add_argument("--secret", default=argparse.SUPPRESS, help="访问密钥，默认读 besh_secret / .besh_secret")
    p.add_argument("--reset", action="store_true", help="重新生成 besh_beacon.py（旧文件会备份）")
    p.add_argument("--force-web", action="store_true", help="覆盖已存在的 www/index.html")
    p.add_argument("--no-run", action="store_true", help="只生成文件、不启动服务")
    p.set_defaults(func=cmd_serve)

    p = subs.add_parser("worker", help="以工人身份接入信标（在闲置算力上运行）")
    p.add_argument("worker_id", help="工人ID，如 phone-1")
    p.add_argument("--dir", default=None, help="工作目录，默认 ./WORKER_ID")
    p.add_argument("--no-sync", action="store_true", help="不启动对象存储定时备份")
    p.add_argument("--interval", type=float, default=2.0, help="每条命令之后的节流秒数")
    p.add_argument("--pane-interval", type=float, default=2.5, help="推送屏幕快照的间隔")
    p.add_argument("--window", default="main", help="首次启动时的窗口名")
    p.add_argument("--wait", type=int, default=20, help="长轮询等待秒数")
    p.set_defaults(func=cmd_worker)

    p = subs.add_parser("sync-loop", help="[内部] besh_sync 会话里跑的定时备份循环")
    p.add_argument("--worker", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--interval", type=int, default=DEFAULT_SYNC_INTERVAL)
    p.set_defaults(func=cmd_sync_loop)

    for alias in ("ls", "list-sessions", "list-workers"):
        p = subs.add_parser(alias, help="列出所有工人（= list-sessions）")
        p.set_defaults(func=cmd_ls)

    for alias in ("windows", "list-windows"):
        p = subs.add_parser(alias, help="列出工人的窗口")
        p.add_argument("-t", "--target", required=True, help="工人ID")
        p.add_argument("--timeout", type=int, default=8)
        p.set_defaults(func=cmd_windows)

    p = subs.add_parser("capture-pane", help="查看窗口内容（= tmux capture-pane）")
    p.add_argument("-t", "--target", required=True, help="WORKER 或 WORKER:WINDOW")
    p.add_argument("-p", action="store_true", help="打印到标准输出（兼容 tmux）")
    p.add_argument("-S", "--start", type=int, default=50, help="从窗口往回抓多少行（默认 50）")
    p.add_argument("-e", "--escapes", action="store_true", help="保留 ANSI 颜色转义")
    p.add_argument("--cached", action="store_true", help="只读缓存快照，不打扰工人")
    p.set_defaults(func=cmd_capture)

    p = subs.add_parser("send-keys", help="向窗口发送按键（窗口不存在则自动创建）")
    p.add_argument("-t", "--target", required=True)
    p.add_argument("-l", "--literal", action="store_true", help="把参数当字面文本发送")
    p.add_argument("-w", "--wait", type=int, default=0, help="等待执行完成的秒数")
    p.add_argument("keys", nargs="+", help="按键序列，如 'ls -l' Enter")
    p.set_defaults(func=cmd_send_keys)

    p = subs.add_parser("type", help="向窗口输入一段文本（可选回车），AI Agent 友好")
    p.add_argument("-t", "--target", required=True)
    p.add_argument("-n", "--no-enter", action="store_true", help="不发送回车")
    p.add_argument("-w", "--wait", type=int, default=0)
    p.add_argument("text", nargs="+")
    p.set_defaults(func=cmd_type)

    p = subs.add_parser("run", help="执行一条命令并打印输出（= ssh host cmd）")
    p.add_argument("-t", "--target", required=True)
    p.add_argument("--timeout", type=int, default=30)
    p.add_argument("command", nargs="+")
    p.set_defaults(func=cmd_run)

    p = subs.add_parser("new-window", help="新建窗口")
    p.add_argument("-t", "--target", required=True, help="WORKER 或 WORKER:WINDOW")
    p.add_argument("-n", "--name", default="", help="窗口名（默认取 -t 里的窗口）")
    p.add_argument("command", nargs="*", help="窗口启动时执行的命令")
    p.set_defaults(func=cmd_new_window)

    p = subs.add_parser("kill-window", help="关闭窗口")
    p.add_argument("-t", "--target", required=True, help="WORKER:WINDOW")
    p.set_defaults(func=cmd_kill_window)

    p = subs.add_parser("kill-worker", help="让工人退出（tmux 会话一并关闭）")
    p.add_argument("-t", "--target", required=True)
    p.set_defaults(func=cmd_kill_worker)

    p = subs.add_parser(
        "wait",
        help="等待结束：-t WORKER:WINDOW 等窗口空闲；或给命令 ID 等回执",
    )
    p.add_argument("cmd_id", nargs="?", default="", help="命令 ID（besh history 里可查）")
    p.add_argument("-t", "--target", default="", help="WORKER:WINDOW，等待该窗口回到提示符")
    p.add_argument("--timeout", type=int, default=60)
    p.set_defaults(func=cmd_wait)

    p = subs.add_parser("history", help="查看命令历史")
    p.add_argument("-t", "--target", default="", help="只看某个工人")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.set_defaults(func=cmd_history)

    p = subs.add_parser("info", help="查看工人详情")
    p.add_argument("-t", "--target", required=True)
    p.add_argument("--refresh", action="store_true", help="顺带刷新窗口列表")
    p.set_defaults(func=cmd_info)

    p = subs.add_parser("files", help="列出对象存储里的备份文件")
    p.add_argument("-t", "--target", required=True)
    p.add_argument("--date", default="", help="备份日期 YYYY-MM-DD，默认今天")
    p.add_argument("--path", default="", help="子目录")
    p.set_defaults(func=cmd_files)

    p = subs.add_parser("download", help="从对象存储下载文件")
    p.add_argument("-t", "--target", required=True)
    p.add_argument("path", help="对象存储里的路径")
    p.add_argument("-o", "--output", default="", help="保存到本地哪个文件")
    p.add_argument("--date", default="")
    p.set_defaults(func=cmd_download)

    p = subs.add_parser("upload", help="上传本地文件到对象存储")
    p.add_argument("-t", "--target", required=True)
    p.add_argument("local_file")
    p.add_argument("remote", nargs="?", default="", help="远端路径，默认用文件名")
    p.add_argument("--date", default="")
    p.set_defaults(func=cmd_upload)

    p = subs.add_parser("rm", help="删除对象存储里的一个文件")
    p.add_argument("-t", "--target", required=True)
    p.add_argument("path")
    p.add_argument("--date", default="")
    p.set_defaults(func=cmd_rm)

    p = subs.add_parser("backup", help="立刻让工人把工作目录备份到对象存储")
    p.add_argument("-t", "--target", required=True)
    p.add_argument("--timeout", type=int, default=60)
    p.set_defaults(func=cmd_backup)

    p = subs.add_parser("restore", help="把对象存储的备份恢复到工人的工作目录")
    p.add_argument("-t", "--target", required=True)
    p.add_argument("--date", default="")
    p.add_argument("--path", default="")
    p.add_argument("--timeout", type=int, default=120)
    p.set_defaults(func=cmd_restore)

    p = subs.add_parser("mcp", help="打印/写入 MCP 客户端配置")
    p.add_argument("--write", default="", help="写入指定 JSON 配置文件（会合并 mcpServers）")
    p.set_defaults(func=cmd_mcp)

    p = subs.add_parser("skill", help="打印或安装 AI Agent 用的命令行技能")
    p.add_argument("--install", default="", help="安装到目录，如 ~/.claude/skills")
    p.set_defaults(func=cmd_skill)

    p = subs.add_parser("version", help="显示版本")
    p.set_defaults(func=cmd_version)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except BeshError as exc:
        print(f"besh: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
