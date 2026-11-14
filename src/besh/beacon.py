# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""信标（beacon）服务端逻辑。

`besh serve` 生成的 ``besh_beacon.py`` 只做三件事：填充 ``cfg``、
把本模块的 ``@sqless.api`` 函数交给 sqless 跑起来、启动 HTTP/MCP 服务。

信标的全部状态（工人、命令队列、屏幕缓存）都在内存里，运行时**不写磁盘**
（见 :mod:`besh.store`），因此长时间运行也不会把服务器磁盘写满。

sqless 会把每个 ``@sqless.api`` 函数同时暴露成：

* HTTP 接口  ``GET/POST /api/<函数名>``
* MCP 工具    ``POST /mcp``

所以遥控器、Web 界面、AI Agent 走的是同一套入口。
"""

# 注意：这里不能写 ``from __future__ import annotations``。
# sqless 生成 MCP inputSchema 时按类型对象做 dict 查找（int→integer、str→string…），
# 加了 future 导入后注解变成字符串，全部会退化成 object，客户端调用会被类型校验拒绝。

import asyncio
import base64
import json
import os
import re
import time
import zlib
from typing import Any, Dict, List, Optional

import sqless

from . import storage
from .store import Store
from .util import (
    BeshError,
    extract_marker,
    now_iso,
    safe_id,
    safe_window,
    to_bool,
    to_int,
)

STATE: Dict[str, Any] = {
    "cfg": {},
    "dbs": None,
    "store": None,
    "started_at": "",
    "app": "",
}

# 每个工人一个"有新命令了"的唤醒事件：长轮询才能做到毫秒级下发
_WAKE: Dict[str, asyncio.Event] = {}
# 命令级事件：capture/run 等等待工人的回执
_RESULT: Dict[str, asyncio.Event] = {}
# requeue_stale 的节流
_REQUEUE_AT: Dict[str, float] = {}
# 工人心跳写库的节流（每次 poll 都写库会让磁盘 IO 飙升）
_TOUCH_AT: Dict[str, float] = {}
# 命令历史/过期数据清理的节流
_PRUNE_AT: float = 0.0

# 心跳最小写入间隔（秒）；重连或 meta 变化时会立即写。在线判定阈值是 45s，
# 这里留足余量：空闲工人每 20 秒最多一次小事务，磁盘几乎无感。
TOUCH_SECONDS = 20.0
# 清理间隔（秒）
PRUNE_SECONDS = 600.0


# --------------------------------------------------------------------- 生命周期


def make_cfg(app_file: str, host: str = "", port: int = 0) -> dict:
    """按 README 的约定生成 sqless 的 cfg（相对 besh_beacon.py 所在目录）。

    入口程序即配置文件：host/port 由 besh_beacon.py 顶部的 CFG_HOST /
    CFG_PORT 传入，用户直接改文件即可；环境变量 besh_host / besh_port
    只在入口程序没写值时兜底。
    """
    from .util import DEFAULT_PORT, getenv, getenv_int, load_env

    path_this = os.path.dirname(os.path.abspath(app_file)).replace("\\", "/")
    load_env(path_this)  # 加载信标目录下的 .env（不覆盖真实环境变量）
    secret = getenv("besh_secret") or _read_secret(path_this) or ""
    cfg = {
        "host": str(host or "").strip() or getenv("besh_host") or "0.0.0.0",
        "port": int(port or getenv_int("besh_port") or DEFAULT_PORT),
        "secret": secret,
        "app_file": os.path.abspath(app_file),
        "path_base_db": os.path.realpath(f"{path_this}/db"),
        "path_base_fs": os.path.realpath(f"{path_this}/fs"),
        "path_base_www": os.path.realpath(f"{path_this}/www"),
        "max_filesize": 200,
        # 这两个接口允许免密访问，方便探活与浏览器首屏
        "open_get_prefix": ["/api/hello"],
    }
    return cfg


def _read_secret(folder: str) -> str:
    path = os.path.join(folder, ".besh_secret")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:  # pragma: no cover
            return ""
    return ""


def bind(cfg: dict, dbs: Any) -> Store:
    """把 cfg/dbs 注入本模块。多次调用是安全的（sqless 会二次导入 app）。"""
    app = str(cfg.get("app_file") or "")
    if STATE.get("store") is not None and app and STATE.get("app") == app:
        return STATE["store"]
    STATE["cfg"] = cfg
    STATE["dbs"] = dbs
    STATE["store"] = Store(dbs)
    STATE["started_at"] = now_iso()
    STATE["app"] = app
    STATE["store"].prune()
    return STATE["store"]


def store() -> Store:
    if STATE.get("store") is None:
        raise BeshError("信标尚未初始化：请通过 `besh serve` 启动")
    return STATE["store"]


def startup() -> None:
    cfg = STATE.get("cfg") or {}
    secret = cfg.get("secret") or ""
    if not secret:
        print("!" * 72)
        print("!! 没有设置密钥，任何人连上这个端口都能控制所有工人。")
        print("!! 请先 `export besh_secret=你的密码` 再运行 `besh serve`。")
        print("!" * 72)
    print(f"besh beacon ready · worker 入口 http://<HOST>:{cfg.get('port')}/?secret=<SECRET>")
    print(f"storage remote: {storage.remote_base()} · rclone: {'ok' if storage.available() else '缺失'}")


def shutdown() -> None:
    for ev in list(_WAKE.values()):
        ev.set()


# --------------------------------------------------------------------- 内部工具


def _wake(worker: str) -> None:
    ev = _WAKE.get(worker)
    if ev is not None:
        ev.set()


# 上次写入库的 meta 摘要：meta 没变就不写，避免每次 poll 都落盘
_TOUCH_META: Dict[str, int] = {}


def _touch(worker: str, meta: Optional[dict] = None) -> None:
    """写工人心跳，但做节流：meta 变化立即写，否则每 TOUCH_SECONDS 写一次。

    长轮询下每个工人每 20 秒就会 poll 一次，若每次都 upsert，磁盘写入会
    随工人数线性上升（WAL 持续增长、频繁 checkpoint）。
    """
    now = time.monotonic()
    digest = None
    if meta is not None:
        digest = hash(json.dumps(meta, sort_keys=True, default=str))
    changed = digest is not None and _TOUCH_META.get(worker) != digest
    if not changed and now - _TOUCH_AT.get(worker, 0.0) < TOUCH_SECONDS:
        return
    _TOUCH_AT[worker] = now
    if digest is not None:
        _TOUCH_META[worker] = digest
    store().touch(worker, meta or {})
    windows = (meta or {}).get("windows")
    if isinstance(windows, list) and windows:
        names = [str(w.get("name") or "") for w in windows if isinstance(w, dict)]
        store().prune_panes(worker, names)


def _prune_due() -> bool:
    global _PRUNE_AT
    now = time.monotonic()
    if now - _PRUNE_AT < PRUNE_SECONDS:
        return False
    _PRUNE_AT = now
    return True


def _wake_event(worker: str) -> asyncio.Event:
    ev = _WAKE.get(worker)
    if ev is None:
        ev = asyncio.Event()
        _WAKE[worker] = ev
    return ev


def _json_loads(text: Any, default: Any) -> Any:
    if isinstance(text, (dict, list)):
        return text
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _submit(worker: str, kind: str, args: Optional[dict] = None) -> dict:
    cmd = store().add(worker, kind, args or {})
    _wake(worker)
    return cmd


async def _request(worker: str, kind: str, args: Optional[dict] = None, timeout: float = 8.0) -> Optional[dict]:
    """下发命令并等工人回执；超时返回 None。"""
    cmd = store().add(worker, kind, args or {})
    ev = _RESULT.get(cmd["id"])
    if ev is None:
        ev = asyncio.Event()
        _RESULT[cmd["id"]] = ev
    _wake(worker)
    try:
        await asyncio.wait_for(ev.wait(), max(0.1, timeout))
    except asyncio.TimeoutError:
        return None
    finally:
        _RESULT.pop(cmd["id"], None)
    return store().get_cmd(cmd["id"])


async def _await_result(cmd_id: str, timeout: float = 8.0) -> Optional[dict]:
    row = store().get_cmd(cmd_id)
    if row and row.get("status") in ("done", "error"):
        return row
    ev = _RESULT.get(cmd_id)
    if ev is None:
        ev = asyncio.Event()
        _RESULT[cmd_id] = ev
    try:
        await asyncio.wait_for(ev.wait(), max(0.1, timeout))
    except asyncio.TimeoutError:
        return store().get_cmd(cmd_id)
    finally:
        _RESULT.pop(cmd_id, None)
    return store().get_cmd(cmd_id)


def _parse_keys(keys_json: str) -> List[str]:
    """``keys_json`` 是 JSON 数组（遥控器风格），也接受一句普通文本。"""
    if isinstance(keys_json, (list, tuple)):
        return [str(k) for k in keys_json]
    text = str(keys_json or "")
    if not text:
        return []
    if text.strip().startswith("["):
        loaded = _json_loads(text, None)
        if isinstance(loaded, list):
            return [str(k) for k in loaded]
    return [text]


def _windows_from(meta_windows: Any) -> List[dict]:
    if not isinstance(meta_windows, list):
        return []
    out = []
    for item in meta_windows:
        if isinstance(item, dict):
            out.append(item)
    return out


def _active_window(worker: str) -> str:
    row = store().get_worker(worker)
    if row:
        for win in _windows_from(row.get("windows")):
            if win.get("active"):
                return str(win.get("name") or "")
        wins = _windows_from(row.get("windows"))
        if wins:
            return str(wins[0].get("name") or "")
    return ""


def _extract_run(content: str, marker: str) -> tuple:
    """从屏幕内容里切出命令输出与退出码（实现见 util.extract_marker）。"""
    return extract_marker(content, marker)


def _need_worker(worker: str) -> str:
    worker = safe_id(worker, "工人ID")
    return worker


# --------------------------------------------------------------------- 基础接口


@sqless.api
def hello() -> dict:
    """探活：返回信标版本、工人数量、对象存储是否可用。"""
    from .__about__ import __version__

    workers = []
    try:
        workers = store().list_workers()
    except Exception:
        pass
    return {
        "suc": True,
        "name": "besh beacon",
        "version": __version__,
        "started_at": STATE.get("started_at"),
        "workers": len(workers),
        "online": len([w for w in workers if w.get("online")]),
        "storage": storage.available(),
        "remote": storage.remote_base(),
    }


@sqless.api
def list_workers(online_seconds: int = 45) -> dict:
    """列出所有工人（相当于 tmux 的 list-sessions），含在线状态、主机、工作目录、窗口数。"""
    rows = store().list_workers(online_seconds=max(5, to_int(online_seconds, 45)))
    data = []
    for row in rows:
        data.append(
            {
                "worker": row.get("worker"),
                "online": bool(row.get("online")),
                "host": row.get("host") or "",
                "cwd": row.get("cwd") or "",
                "pid": row.get("pid") or 0,
                "version": row.get("version") or "",
                "seen_at": row.get("seen_at") or "",
                "started_at": row.get("started_at") or "",
                "sync": row.get("sync") or "",
                "active": row.get("active") or "",
                "windows": [w.get("name") for w in _windows_from(row.get("windows"))],
                "pending": store().pending_count(str(row.get("worker") or "")),
            }
        )
    return {"suc": True, "data": data}


@sqless.api
async def list_windows(worker: str, refresh: bool = True, timeout: int = 8) -> dict:
    """列出工人的窗口（相当于 tmux list-windows -t WORKER）。"""
    worker = _need_worker(worker)
    if to_bool(refresh, True):
        row = await _request(worker, "windows", {}, timeout=max(1, to_int(timeout, 8)))
        if row and row.get("status") == "done":
            windows = _json_loads(row.get("output"), [])
            if isinstance(windows, list):
                return {"suc": True, "worker": worker, "live": True, "data": windows}
    row = store().get_worker(worker)
    if not row:
        return {"suc": False, "msg": f"没有这个工人: {worker}"}
    return {
        "suc": True,
        "worker": worker,
        "live": False,
        "data": _windows_from(row.get("windows")),
    }


@sqless.api
async def capture_pane(
    worker: str,
    window: str = "",
    lines: int = 50,
    escapes: bool = True,
    refresh: bool = True,
) -> dict:
    """读取工人某个窗口的屏幕内容（相当于 tmux capture-pane -p -S -N）。

    window 为空表示工人当前活动窗口；lines 是往回抓多少行。
    """
    worker = _need_worker(worker)
    window = safe_window(window)
    lines = max(1, min(to_int(lines, 50), 5000))
    if to_bool(refresh, True):
        row = await _request(
            worker,
            "capture",
            {"window": window, "lines": lines, "escapes": to_bool(escapes, True)},
            timeout=8,
        )
        if row and row.get("status") == "done":
            return {
                "suc": True,
                "worker": worker,
                "window": window or _active_window(worker),
                "live": True,
                "content": row.get("output") or "",
            }
    cached = store().get_pane(worker, window or _active_window(worker))
    if cached:
        return {
            "suc": True,
            "worker": worker,
            "window": cached.get("window"),
            "live": False,
            "content": cached.get("content") or "",
            "updated_at": cached.get("updated_at"),
        }
    return {
        "suc": False,
        "worker": worker,
        "window": window,
        "live": False,
        "content": "",
        "msg": f"工人 {worker} 没有响应（离线或窗口不存在）",
    }


@sqless.api
async def send_keys(
    worker: str,
    keys_json: str = "",
    window: str = "",
    literal: bool = False,
    wait: int = 0,
) -> dict:
    """把按键发送到工人的窗口（相当于 tmux send-keys）。

    keys_json 是 JSON 数组，如 ``["ls -l", "Enter"]``；也接受一整句文本。
    literal=True 时按字面发送，不做按键名解析。
    """
    worker = _need_worker(worker)
    window = safe_window(window)
    keys = _parse_keys(keys_json)
    if not keys:
        return {"suc": False, "msg": "没有要发送的按键"}
    args = {"window": window, "keys": keys, "literal": to_bool(literal)}
    if to_int(wait, 0) > 0:
        row = await _request(worker, "keys", args, timeout=max(1, to_int(wait, 0)))
        if not row:
            return {"suc": False, "msg": f"工人 {worker} 没有响应"}
        return {"suc": True, "cmd_id": row.get("key"), "output": row.get("output") or ""}
    cmd = _submit(worker, "keys", args)
    return {"suc": True, "cmd_id": cmd["id"], "keys": keys, "window": window}


@sqless.api
async def type_text(
    worker: str,
    text: str = "",
    window: str = "",
    enter: bool = True,
    wait: int = 0,
) -> dict:
    """往工人的窗口里"打字"（文本原样输入，可选回车）。AI Agent 最常用的接口。"""
    worker = _need_worker(worker)
    window = safe_window(window)
    keys: List[str] = []
    if text:
        keys.append(str(text))
    if to_bool(enter, True):
        keys.append("Enter")
    if not keys:
        return {"suc": False, "msg": "没有要发送的文本"}
    args = {"window": window, "keys": keys, "literal": False, "text_literal_first": True}
    if to_int(wait, 0) > 0:
        row = await _request(worker, "keys", args, timeout=max(1, to_int(wait, 0)))
        if not row:
            return {"suc": False, "msg": f"工人 {worker} 没有响应"}
        return {"suc": True, "cmd_id": row.get("key"), "output": row.get("output") or ""}
    cmd = _submit(worker, "keys", args)
    return {"suc": True, "cmd_id": cmd["id"]}


@sqless.api
async def run(worker: str, command: str, window: str = "", timeout: int = 30) -> dict:
    """在工人窗口执行一条 shell 命令并返回输出（相当于 ssh host "cmd"）。

    整个"输入命令 → 等哨兵 → 读屏幕"的过程都在工人本地完成（tmux 就在它
    身旁，没有网络往返），信标只发一次命令、收一次回执，延迟最低。
    """
    worker = _need_worker(worker)
    window = safe_window(window)
    command = str(command or "").strip()
    if not command:
        return {"suc": False, "msg": "命令为空"}
    timeout = max(1, min(to_int(timeout, 30), 600))
    row = await _request(
        worker,
        "run",
        {"window": window, "command": command, "timeout": timeout},
        timeout=timeout + 8,
    )
    if not row or row.get("status") not in ("done", "error"):
        return {
            "suc": False,
            "worker": worker,
            "window": window,
            "msg": f"工人 {worker} 没有响应（离线，或窗口「{window or '当前窗口'}」不存在）",
        }
    return {
        "suc": row.get("status") == "done",
        "worker": worker,
        "window": window or _active_window(worker),
        "cmd_id": row.get("key"),
        "exit_code": int(row.get("exit_code") or 0),
        "output": str(row.get("output") or ""),
    }


@sqless.api
async def new_window(worker: str, window: str, command: str = "") -> dict:
    """新建窗口（相当于 tmux new-window -n WINDOW）。"""
    worker = _need_worker(worker)
    window = safe_window(window)
    if not window:
        return {"suc": False, "msg": "需要窗口名"}
    row = await _request(
        worker, "newwin", {"window": window, "command": str(command or "")}, timeout=8
    )
    if not row or row.get("status") != "done":
        return {"suc": False, "msg": f"工人 {worker} 没有响应"}
    return await list_windows(worker, refresh=True, timeout=6)


@sqless.api
async def kill_window(worker: str, window: str) -> dict:
    """关闭窗口（相当于 tmux kill-window -t WORKER:WINDOW）。"""
    worker = _need_worker(worker)
    window = safe_window(window)
    if not window:
        return {"suc": False, "msg": "需要窗口名"}
    row = await _request(worker, "killwin", {"window": window}, timeout=8)
    if not row or row.get("status") != "done":
        return {"suc": False, "msg": f"工人 {worker} 没有响应"}
    await list_windows(worker, refresh=True, timeout=5)
    return {"suc": True, "worker": worker, "window": window}


@sqless.api
def kill_worker(worker: str) -> dict:
    """让工人退出（会顺带关闭它自己的 tmux 会话，工作目录保留）。"""
    worker = _need_worker(worker)
    store().set_flag(worker, kill=True)
    _wake(worker)
    return {"suc": True, "worker": worker, "msg": "已通知工人退出"}


@sqless.api
async def wait_cmd(cmd_id: str, timeout: int = 30) -> dict:
    """等待某条命令执行完成，返回它的输出。"""
    row = await _await_result(str(cmd_id or ""), timeout=max(1, to_int(timeout, 30)))
    if not row:
        return {"suc": False, "msg": "没有这条命令"}
    return {"suc": True, "data": row}


# 常见 shell 提示符的行尾：$ % # >（bash/zsh/ fish/powershell/mysql 等）
_PROMPT_TAIL = re.compile(r"[$%#>]\s*\Z")


def _pane_idle(content: str) -> bool:
    """屏幕最后一行是提示符（命令大概率已执行完）。"""
    if not content:
        return False
    lines = [ln for ln in content.splitlines() if ln.strip()]
    if not lines:
        return False
    return bool(_PROMPT_TAIL.search(lines[-1]))


@sqless.api
async def wait_idle(worker: str, window: str = "", timeout: int = 60) -> dict:
    """等待工人的窗口回到提示符（即命令执行完毕），返回最终屏幕内容。

    判定方式：连续两次（间隔约 0.5s）抓屏内容一致，且最后一行是提示符。
    capture 请求与 send-keys 走同一条 FIFO 队列，所以先发的命令必然
    先被执行，不会出现"命令还没打上去就误判空闲"的竞态。
    提示符识别是启发式的（``$ % # >`` 结尾），自定义提示符可能识别不了。
    """
    worker = _need_worker(worker)
    window = safe_window(window)
    timeout = max(1, min(to_int(timeout, 60), 600))
    deadline = time.monotonic() + timeout
    content = ""
    stable = 0
    while time.monotonic() < deadline:
        row = await _request(
            worker, "capture", {"window": window, "lines": 200, "escapes": False}, timeout=6
        )
        if row and row.get("status") == "done":
            new = row.get("output") or ""
            if new == content and new:
                stable += 1
                if stable >= 2 and _pane_idle(new):
                    return {
                        "suc": True,
                        "worker": worker,
                        "window": window or _active_window(worker),
                        "content": new,
                    }
            else:
                content, stable = new, 0
        await asyncio.sleep(0.5)
    return {
        "suc": False,
        "worker": worker,
        "window": window,
        "content": content,
        "msg": f"窗口在 {timeout}s 内没有回到提示符（可能在跑长命令或交互式程序）",
    }


@sqless.api
def command_history(worker: str = "", limit: int = 20) -> dict:
    """查看最近的命令历史（审计用）。"""
    rows = store().history(str(worker or ""), limit=max(1, min(to_int(limit, 20), 200)))
    out = []
    for row in rows:
        out.append(
            {
                "id": row.get("key"),
                "worker": row.get("worker"),
                "kind": row.get("kind"),
                "status": row.get("status"),
                "created_at": row.get("created_at"),
                "exit_code": row.get("exit_code"),
                "output": str(row.get("output") or "")[:2000],
            }
        )
    return {"suc": True, "data": out}


# --------------------------------------------------------------------- 对象存储


@sqless.api
async def storage_info() -> dict:
    """对象存储概况：是否装了 rclone、远端前缀、默认目录规则。"""
    return {
        "suc": True,
        "rclone": storage.available(),
        "remote": storage.remote_base(),
        "layout": "{remote}/{YYYY-MM-DD}/{WORKER_ID}/...",
    }


@sqless.api
async def storage_files(worker: str, date: str = "", path: str = "") -> dict:
    """列出工人在对象存储里的备份文件（默认今天的目录）。"""
    worker = _need_worker(worker)
    remote = storage.remote_path(worker, path, date)
    ret = await storage.lsjson(remote)
    ret["remote"] = remote
    ret["date"] = date or storage.today()
    ret["worker"] = worker
    return ret


@sqless.api
async def storage_link(worker: str, path: str, date: str = "") -> dict:
    """生成对象存储的预签名下载链接（浏览器直连，不吃信标带宽）。"""
    worker = _need_worker(worker)
    remote = storage.remote_path(worker, path, date)
    ret = await storage.link(remote)
    ret["remote"] = remote
    return ret


@sqless.api
async def storage_put(worker: str, path: str, b64: str, date: str = "") -> dict:
    """把 base64 内容写进对象存储（Web 界面上传文件走这里）。"""
    worker = _need_worker(worker)
    remote = storage.remote_path(worker, path, date)
    try:
        data = base64.b64decode(str(b64 or ""), validate=False)
    except Exception:
        return {"suc": False, "msg": "base64 解码失败"}
    ret = await storage.put_bytes(remote, data)
    ret["remote"] = remote
    ret["size"] = len(data)
    return ret


@sqless.api
async def storage_get(worker: str, path: str, date: str = "") -> dict:
    """读取对象存储里的文件（base64 返回，适合小文件）。"""
    worker = _need_worker(worker)
    remote = storage.remote_path(worker, path, date)
    ret = await storage.get_bytes(remote)
    if not ret.get("suc"):
        return ret
    data = ret.pop("data") or b""
    ret["b64"] = base64.b64encode(data).decode("ascii")
    ret["size"] = len(data)
    ret["remote"] = remote
    return ret


@sqless.api
async def storage_delete(worker: str, path: str, date: str = "") -> dict:
    """删除对象存储里的一个文件。"""
    worker = _need_worker(worker)
    remote = storage.remote_path(worker, path, date)
    return await storage.delete(remote)


@sqless.api
async def backup(worker: str, wait: int = 30) -> dict:
    """立刻让工人把工作目录同步到对象存储（不等 tmux 里的定时任务）。"""
    worker = _need_worker(worker)
    row = await _request(worker, "sync", {}, timeout=max(5, to_int(wait, 30)))
    if not row or row.get("status") != "done":
        return {"suc": False, "msg": f"工人 {worker} 没有响应"}
    return {"suc": True, "worker": worker, "output": row.get("output") or ""}


@sqless.api
async def restore(worker: str, date: str = "", path: str = "", wait: int = 60) -> dict:
    """把对象存储里的备份恢复到工人的工作目录。"""
    worker = _need_worker(worker)
    row = await _request(
        worker, "restore", {"date": date, "path": path}, timeout=max(5, to_int(wait, 60))
    )
    if not row or row.get("status") != "done":
        return {"suc": False, "msg": f"工人 {worker} 没有响应"}
    return {"suc": True, "worker": worker, "output": row.get("output") or ""}


# --------------------------------------------------------------------- 工人专用


@sqless.api
async def poll(worker: str, meta: str = "", wait: int = 20) -> dict:
    """[工人专用] 长轮询取命令。请求体里的 meta 会更新工人的状态记录。"""
    worker = _need_worker(worker)
    info = _json_loads(meta, {})
    if not isinstance(info, dict):
        info = {}
    _touch(worker, info)
    timeout = max(0, min(to_int(wait, 20), 55))
    deadline = time.monotonic() + timeout
    while True:
        ev = _wake_event(worker)
        ev.clear()
        if time.monotonic() - _REQUEUE_AT.get(worker, 0) > 10:
            _REQUEUE_AT[worker] = time.monotonic()
            store().requeue_stale()
        if _prune_due():
            store().prune()
        cmds = store().claim(worker, limit=16)
        if cmds:
            return {"suc": True, "data": cmds, "server": now_iso()}
        kill = bool(store().get_flag(worker, "kill"))
        if kill:
            return {"suc": True, "data": [], "kill": True, "server": now_iso()}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"suc": True, "data": [], "server": now_iso()}
        _touch(worker)  # 节流：至少每 TOUCH_SECONDS 保活一次
        try:
            await asyncio.wait_for(ev.wait(), min(remaining, 10))
        except asyncio.TimeoutError:
            pass


@sqless.api
def report(cmd_id: str, output: str = "", exit_code: int = 0, status: str = "done") -> dict:
    """[工人专用] 回报命令执行结果。"""
    cmd_id = str(cmd_id or "")
    if not cmd_id:
        return {"suc": False, "msg": "缺少 cmd_id"}
    status = str(status or "done")
    if status not in ("done", "error"):
        status = "done"
    store().finish(cmd_id, str(output or ""), to_int(exit_code, 0), status)
    ev = _RESULT.get(cmd_id)
    if ev is not None:
        ev.set()
    return {"suc": True}


def _decode_panes(raw: Any) -> dict:
    """解析屏幕快照负载：兼容压缩格式（"z:" + base64(zlib(JSON))）与纯 JSON。

    压缩是为了把请求体压小：sqless 会把整个请求体打到 stdout，未压缩时
    一次全屏推送能产生上百 KB 日志写入。
    """
    text = raw if isinstance(raw, str) else str(raw or "")
    if text.startswith("z:"):
        try:
            blob = base64.b64decode(text[2:])
            data = json.loads(zlib.decompress(blob).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}
    data = _json_loads(text, {})
    return data if isinstance(data, dict) else {}


@sqless.api
def panes(worker: str, panes_json: str = "") -> dict:
    """[工人专用] 批量上报各窗口的屏幕快照，供 capture-pane 秒回。"""
    worker = _need_worker(worker)
    data = _decode_panes(panes_json)
    if not data:
        return {"suc": False, "msg": "panes_json 需要是 JSON 对象"}
    count = store().put_panes(worker, {str(k): str(v) for k, v in data.items()})
    return {"suc": True, "count": count}


@sqless.api
def offline(worker: str, note: str = "") -> dict:
    """[工人专用] 工人退出前把自己标记为离线。"""
    worker = _need_worker(worker)
    store().set_offline(worker, str(note or ""))
    return {"suc": True}
