# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""工人：向信标轮询命令，转交给本机的 tmux 执行。

工人不提供安全隔离（它本来就跑在容器/虚拟机里），只用两个 tmux 会话：

* ``besh_worker`` —— 所有窗口都在这里，命令在此执行
* ``besh_sync``   —— 定时把工作目录同步到对象存储（rclone）
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from .client import Beacon
from .util import (
    BeshError,
    DEFAULT_SYNC_INTERVAL,
    getenv,
    getenv_int,
    safe_id,
    safe_window,
    today,
    shorten,
)

SESSION_WORKER = "besh_worker"
SESSION_SYNC = "besh_sync"


# --------------------------------------------------------------------- tmux 基础


def tmux_bin() -> str:
    return getenv("besh_tmux", default="tmux") or "tmux"


def require_tmux() -> str:
    path = shutil.which(tmux_bin())
    if not path:
        raise BeshError(
            "没有找到 tmux。工人需要 tmux 来托管窗口，请先安装：\n"
            "  Debian/Ubuntu: sudo apt install -y tmux\n"
            "  Termux:        pkg install -y tmux\n"
            "  Alpine:        apk add tmux"
        )
    return path


def tmux(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [tmux_bin(), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        errors="replace",
    )


def tmux_ok(*args: str, timeout: int = 30) -> Tuple[bool, str]:
    try:
        ret = tmux(*args, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return False, str(exc)
    out = (ret.stdout or "") + (ret.stderr or "")
    return ret.returncode == 0, out.strip()


def session_exists(name: str) -> bool:
    return tmux_ok("has-session", "-t", f"={name}")[0]


def target_of(window: str = "", session: str = SESSION_WORKER) -> str:
    """构造 tmux 目标；= 前缀表示会话名精确匹配。"""
    window = safe_window(window)
    return f"={session}:{window}" if window else f"={session}"


def resolve_window(window: str = "", session: str = SESSION_WORKER) -> str:
    """把空窗口解析成会话当前的活动窗口名。

    send-keys / capture-pane 都是 pane 类命令，裸会话目标（``=besh_worker``）
    在部分 tmux 版本上解析不到 pane，导致"不带 :window 就超时"。
    统一解析成 ``SESSION:WINDOW`` 形式最稳。
    """
    name = safe_window(window)
    if name:
        return name
    windows = list_windows(session)
    for win in windows:
        if win.get("active"):
            return str(win.get("name") or "")
    return str(windows[0].get("name") or "") if windows else ""


def list_windows(session: str = SESSION_WORKER) -> List[dict]:
    fmt = "\t".join(
        [
            "#{window_index}",
            "#{window_name}",
            "#{window_active}",
            "#{pane_current_path}",
            "#{pane_current_command}",
            "#{window_width}x#{window_height}",
            "#{window_activity}",
        ]
    )
    ok, out = tmux_ok("list-windows", "-t", f"={session}", "-F", fmt)
    if not ok:
        return []
    windows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        windows.append(
            {
                "index": parts[0],
                "name": parts[1],
                "active": parts[2] == "1",
                "path": parts[3],
                "command": parts[4],
                "size": parts[5],
                "activity": parts[6],
            }
        )
    return windows


def window_exists(name: str, session: str = SESSION_WORKER) -> bool:
    return any(w["name"] == name for w in list_windows(session))


def ensure_window(name: str, workdir: str, session: str = SESSION_WORKER) -> None:
    """窗口不存在就创建（README：发送按键时窗口不存在则自动创建）。"""
    name = safe_window(name)
    if not name or window_exists(name, session):
        return
    tmux(
        "new-window",
        "-d",
        "-t",
        f"={session}",
        "-n",
        name,
        "-c",
        workdir,
    )


def capture(window: str, lines: int = 50, escapes: bool = True, session: str = SESSION_WORKER) -> str:
    window = resolve_window(window, session)
    args = ["capture-pane", "-p", "-t", target_of(window, session), "-S", f"-{max(1, lines)}"]
    if escapes:
        args.append("-e")
    ok, out = tmux_ok(*args)
    if not ok:
        return ""
    # 去掉屏幕底部的空行，命令行里看着舒服些
    lines_out = out.split("\n")
    while len(lines_out) > 1 and not lines_out[-1].strip():
        lines_out.pop()
    return "\n".join(lines_out)


def send_keys(window: str, keys: List[str], literal: bool = False, literal_first: bool = False,
              session: str = SESSION_WORKER, workdir: str = ".") -> str:
    """发送按键。

    与 tmux 一致：普通模式下参数会被当作按键名解析，不认识的按字面发送；
    literal=True 时全部按字面发送；literal_first=True 时只把前面的文本当字面，
    最后一个参数当按键（``type_text`` 用这个模式，避免文本恰好是 ``Enter``）。
    """
    if not keys:
        return ""
    # 空窗口 = 会话当前窗口：解析成窗口名，避免裸会话目标在 pane 类命令里失效
    window = resolve_window(window, session)
    if window:
        ensure_window(window, workdir, session)
    target = target_of(window, session)
    if literal:
        text = " ".join(keys)
        ok, out = tmux_ok("send-keys", "-l", "-t", target, text)
        return "" if ok else out
    if literal_first and len(keys) > 1:
        text = " ".join(keys[:-1])
        ok, out = tmux_ok("send-keys", "-l", "-t", target, text)
        if not ok:
            return out
        ok, out = tmux_ok("send-keys", "-t", target, keys[-1])
        return "" if ok else out
    ok, out = tmux_ok("send-keys", "-t", target, *keys)
    return "" if ok else out


def new_window(name: str, workdir: str, command: str = "", session: str = SESSION_WORKER) -> str:
    name = safe_window(name)
    args = ["new-window", "-d", "-t", f"={session}", "-n", name, "-c", workdir]
    if command:
        args.append(command)
    ok, out = tmux_ok(*args)
    return "" if ok else out


def kill_window(name: str, session: str = SESSION_WORKER) -> str:
    if not name:
        return "需要窗口名"
    ok, out = tmux_ok("kill-window", "-t", target_of(name, session))
    return "" if ok else out


def kill_sessions() -> None:
    for name in (SESSION_WORKER, SESSION_SYNC):
        if session_exists(name):
            tmux_ok("kill-session", "-t", f"={name}")


def ensure_worker_session(workdir: str, first_window: str = "main") -> None:
    """确保 besh_worker 会话存在，并且至少有一个窗口。"""
    if not session_exists(SESSION_WORKER):
        ok, out = tmux_ok(
            "new-session",
            "-d",
            "-s",
            SESSION_WORKER,
            "-n",
            first_window,
            "-c",
            workdir,
        )
        if not ok:
            raise BeshError(f"创建 tmux 会话失败：{out}")
    if not list_windows():
        ensure_window(first_window or "main", workdir)


# --------------------------------------------------------------------- 对象存储


def rclone_bin() -> str:
    return getenv("besh_rclone", default="rclone") or "rclone"


def remote_base() -> str:
    from .storage import remote_base as _remote_base

    return _remote_base()


def sync_once(workdir: str, worker: str, date: str = "") -> Tuple[int, str]:
    """把工作目录同步到 ``cf:besh/YYYY-MM-DD/WORKER_ID/``。"""
    if not shutil.which(rclone_bin()):
        return 127, "未安装 rclone，跳过同步（参考 README 安装并配置 RCLONE_CONFIG_CF_*）"
    date = date or today()
    dest = f"{remote_base()}/{date}/{worker}"
    args = [
        rclone_bin(),
        "copy",
        workdir,
        dest,
        "--transfers",
        "8",
        "--checkers",
        "8",
        "--stats-one-line",
        "--stats",
        "30s",
        "--log-level",
        "INFO",
    ]
    exclude = getenv("besh_exclude")
    if exclude:
        for pattern in exclude.split(","):
            pattern = pattern.strip()
            if pattern:
                args += ["--exclude", pattern]
    try:
        ret = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=3600)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return 1, f"rclone 执行失败：{exc}"
    out = ((ret.stdout or "") + (ret.stderr or "")).strip()
    return ret.returncode, out[-4000:]


def restore_once(workdir: str, worker: str, date: str = "", path: str = "") -> Tuple[int, str]:
    if not shutil.which(rclone_bin()):
        return 127, "未安装 rclone，无法恢复"
    from .storage import remote_path, safe_rel_path

    src = remote_path(worker, safe_rel_path(path), date)
    args = [rclone_bin(), "copy", src, workdir, "--transfers", "8", "--stats-one-line"]
    try:
        ret = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=3600)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return 1, f"rclone 执行失败：{exc}"
    out = ((ret.stdout or "") + (ret.stderr or "")).strip()
    return ret.returncode, out[-4000:]


def sync_loop(workdir: str, worker: str, interval: int = DEFAULT_SYNC_INTERVAL) -> None:
    """在 besh_sync 会话里跑的循环：定期备份，日期跨天自动换目录。"""
    interval = max(10, int(interval or DEFAULT_SYNC_INTERVAL))
    print(f"[besh_sync] worker={worker} dir={workdir} remote={remote_base()} interval={interval}s", flush=True)
    last_date = ""
    while True:
        date = today()
        if date != last_date:
            print(f"[besh_sync] {time.strftime('%H:%M:%S')} 备份到 {remote_base()}/{date}/{worker}", flush=True)
            last_date = date
        code, out = sync_once(workdir, worker, date)
        stamp = time.strftime("%H:%M:%S")
        if code == 0:
            print(f"[besh_sync] {stamp} ok {shorten(out or 'synced', 120)}", flush=True)
            last_date = date
        else:
            print(f"[besh_sync] {stamp} failed({code}) {shorten(out, 300)}", flush=True)
            last_date = ""  # 失败后下一轮重试
        time.sleep(interval)


# --------------------------------------------------------------------- 工人主循环


class Worker:
    def __init__(
        self,
        worker_id: str,
        beacon: Beacon,
        workdir: str,
        sync: bool = True,
        interval: float = 2.0,
        pane_interval: float = 2.5,
        first_window: str = "main",
        poll_wait: int = 20,
    ):
        self.id = safe_id(worker_id, "工人ID")
        self.beacon = beacon
        self.workdir = os.path.abspath(workdir)
        self.sync_enabled = sync
        self.interval = max(0.0, float(interval))
        self.pane_interval = max(1.0, float(pane_interval))
        self.first_window = safe_window(first_window) or "main"
        self.poll_wait = max(0, min(int(poll_wait), 55))
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.stop = False
        self._done: "OrderedDict[str, float]" = OrderedDict()
        self._sync_note = "off"
        self._threads: List[threading.Thread] = []

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        require_tmux()
        os.makedirs(self.workdir, exist_ok=True)
        os.chdir(self.workdir)
        ensure_worker_session(self.workdir, self.first_window)
        if self.sync_enabled:
            self._start_sync_session()
        print(f"besh worker {self.id}")
        print(f"  beacon : {self.beacon}")
        print(f"  workdir: {self.workdir}")
        print(f"  tmux   : {SESSION_WORKER} / {SESSION_SYNC if self.sync_enabled else '(sync off)'}")
        print("  提示   : Ctrl-C 只停轮询，tmux 窗口里的任务会继续跑")
        self._spawn(self._pane_loop, "besh-panes")
        try:
            self._loop()
        except KeyboardInterrupt:
            print("\n收到 Ctrl-C，工人退出（tmux 窗口保留）")
        finally:
            self.stop = True
            try:
                self.beacon.call("offline", worker=self.id, note="worker stopped", _timeout=10)
            except Exception:
                pass

    def _spawn(self, fn, name: str) -> None:
        thread = threading.Thread(target=fn, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _start_sync_session(self) -> None:
        if not shutil.which(rclone_bin()):
            self._sync_note = "rclone missing"
            print("! 未安装 rclone：本次不启动 besh_sync（工作目录不会备份到对象存储）")
            return
        if session_exists(SESSION_SYNC):
            print(f"  {SESSION_SYNC} 会话已存在，复用")
            self._sync_note = "on"
            return
        interval = getenv_int("besh_sync_interval", default=DEFAULT_SYNC_INTERVAL)
        cmd = (
            f"{_quote(sys.executable)} -m besh sync-loop "
            f"--worker {_quote(self.id)} --dir {_quote(self.workdir)} --interval {interval}"
        )
        ok, out = tmux_ok(
            "new-session", "-d", "-s", SESSION_SYNC, "-c", self.workdir, cmd
        )
        if ok:
            self._sync_note = "on"
        else:
            self._sync_note = f"error: {shorten(out, 80)}"
            print(f"! 启动 {SESSION_SYNC} 失败：{out}")

    def _mark_done(self, cmd_id: str) -> None:
        self._done[cmd_id] = time.time()
        while len(self._done) > 512:
            self._done.popitem(last=False)

    # ---------------------------------------------------------------- 状态采集

    def _meta(self) -> dict:
        windows = list_windows()
        active = next((w["name"] for w in windows if w.get("active")), "")
        return {
            "host": socket.gethostname(),
            "cwd": self.workdir,
            "pid": os.getpid(),
            "version": _version(),
            "started_at": self.started_at,
            "windows": windows,
            "active": active,
            "sync": self._sync_note,
        }

    def _pane_loop(self) -> None:
        """定期把所有窗口的屏幕推给信标：Web 界面/遥控器 capture 才能秒回。"""
        while not self.stop:
            try:
                windows = list_windows()
                panes: Dict[str, str] = {}
                for win in windows[:12]:
                    panes[win["name"]] = capture(win["name"], lines=200, escapes=True)
                if panes:
                    self.beacon.call(
                        "panes", worker=self.id, panes_json=json.dumps(panes), _timeout=15
                    )
            except Exception:
                pass
            for _ in range(int(self.pane_interval * 10)):
                if self.stop:
                    return
                time.sleep(0.1)

    # ---------------------------------------------------------------- 命令执行

    def _loop(self) -> None:
        errors = 0
        while not self.stop:
            try:
                ret = self.beacon.poll(self.id, self._meta(), wait=self.poll_wait)
                errors = 0
            except Exception as exc:
                errors += 1
                print(f"! 轮询失败（{shorten(str(exc), 160)}），{min(errors * 2, 20)}s 后重试")
                for _ in range(min(errors * 2, 20) * 10):
                    if self.stop:
                        return
                    time.sleep(0.1)
                continue
            if ret.get("kill"):
                print("信标要求退出，正在关闭 tmux 会话…")
                kill_sessions()
                self.stop = True
                break
            for cmd in ret.get("data") or []:
                self._exec(cmd)
            if self.interval:
                time.sleep(self.interval)

    def _exec(self, cmd: dict) -> None:
        cmd_id = str(cmd.get("id") or "")
        if not cmd_id or cmd_id in self._done:
            return
        kind = cmd.get("kind") or ""
        args = cmd.get("args") or {}
        output, code, status = "", 0, "done"
        try:
            output, code, status = self.handle(kind, args)
        except Exception as exc:  # 单条命令失败不影响工人
            output, code, status = f"{type(exc).__name__}: {exc}", 1, "error"
        self._mark_done(cmd_id)
        try:
            self.beacon.call(
                "report", cmd_id=cmd_id, output=str(output or ""), exit_code=int(code), status=status
            )
        except Exception as exc:
            print(f"! 上报结果失败：{shorten(str(exc), 160)}")

    def handle(self, kind: str, args: dict) -> Tuple[str, int, str]:
        if kind == "keys":
            err = send_keys(
                str(args.get("window") or ""),
                [str(k) for k in (args.get("keys") or [])],
                literal=bool(args.get("literal")),
                literal_first=bool(args.get("text_literal_first")),
                workdir=self.workdir,
            )
            return err, (1 if err else 0), ("error" if err else "done")
        if kind == "capture":
            content = capture(
                str(args.get("window") or ""),
                lines=int(args.get("lines") or 50),
                escapes=bool(args.get("escapes", True)),
            )
            return content, 0, "done"
        if kind == "windows":
            return json.dumps(list_windows(), ensure_ascii=False), 0, "done"
        if kind == "newwin":
            err = new_window(
                str(args.get("window") or ""), self.workdir, str(args.get("command") or "")
            )
            return err, (1 if err else 0), ("error" if err else "done")
        if kind == "killwin":
            err = kill_window(str(args.get("window") or ""))
            return err, (1 if err else 0), ("error" if err else "done")
        if kind == "sync":
            code, out = sync_once(self.workdir, self.id, str(args.get("date") or ""))
            return out, code, ("done" if code == 0 else "error")
        if kind == "restore":
            code, out = restore_once(
                self.workdir, self.id, str(args.get("date") or ""), str(args.get("path") or "")
            )
            return out, code, ("done" if code == 0 else "error")
        if kind == "shell":
            return self._shell(str(args.get("command") or ""))
        if kind == "exit":
            self.stop = True
            return "bye", 0, "done"
        return f"未知命令类型: {kind}", 1, "error"

    def _shell(self, command: str) -> Tuple[str, int, str]:
        if not command:
            return "命令为空", 1, "error"
        ret = subprocess.run(
            command,
            shell=True,
            cwd=self.workdir,
            capture_output=True,
            text=True,
            errors="replace",
        )
        out = ((ret.stdout or "") + (ret.stderr or "")).strip()
        return out[-8000:], ret.returncode, ("done" if ret.returncode == 0 else "error")


def _quote(text: str) -> str:
    return shlex.quote(str(text))


def _version() -> str:
    from .__about__ import __version__

    return __version__


def run_worker(
    worker_id: str,
    beacon: Beacon,
    workdir: Optional[str] = None,
    sync: bool = True,
    interval: float = 2.0,
    pane_interval: float = 2.5,
    first_window: str = "main",
    poll_wait: int = 20,
) -> int:
    """``besh worker WORKER_ID`` 的入口。

    工作目录默认是 ``./WORKER_ID``（README 约定），不存在则创建。
    """
    worker_id = safe_id(worker_id, "工人ID")
    path = os.path.abspath(workdir or worker_id)
    worker = Worker(
        worker_id,
        beacon,
        path,
        sync=sync,
        interval=interval,
        pane_interval=pane_interval,
        first_window=first_window,
        poll_wait=poll_wait,
    )
    worker.start()
    return 0
