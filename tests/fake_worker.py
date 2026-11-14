# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""假工人：不依赖 tmux，用内存模拟一个 shell 窗口，用于联调与集成测试。"""

from __future__ import annotations

import base64
import json
import re
import threading
import zlib
from typing import Dict, List, Optional

from besh.client import Beacon

# 命令末尾会被 `besh run` 追加哨兵：<cmd>; echo "__BESH_xxx__$?"
_SENTINEL = re.compile(r'^(?:.*?\$ )?(.*?); echo "(__BESH_[0-9a-f]+__)\$\?"?$')


class FakeShell:
    """一个极简 shell：记录屏幕行，能识别 run 的哨兵。"""

    def __init__(self) -> None:
        self.windows: Dict[str, dict] = {"main": self._new_window()}

    @staticmethod
    def _new_window() -> dict:
        return {"lines": ["besh fake shell"], "cur": "$ "}

    def pane(self, window: str) -> str:
        state = self.windows.setdefault(window, self._new_window())
        return "\n".join(state["lines"] + [state["cur"]])

    def _enter(self, window: str) -> None:
        state = self.windows.setdefault(window, self._new_window())
        typed = state["cur"]
        state["lines"].append(typed)
        body = typed[2:] if typed.startswith("$ ") else typed
        match = _SENTINEL.match(body.strip())
        if match:
            command, marker = match.group(1), match.group(2)
            state["lines"].append("ran: " + command)
            state["lines"].append(marker + "0")
        elif body.strip():
            state["lines"].append("ran: " + body.strip())
        state["cur"] = "$ "

    def handle(self, cmd: dict) -> tuple:
        kind = cmd.get("kind")
        args = cmd.get("args") or {}
        window = args.get("window") or "main"
        if kind == "keys":
            for key in args.get("keys") or []:
                if key == "Enter":
                    self._enter(window)
                else:
                    state = self.windows.setdefault(window, self._new_window())
                    state["cur"] += str(key)
            return "", 0, "done"
        if kind == "capture":
            return self.pane(window), 0, "done"
        if kind == "windows":
            rows = [
                {
                    "index": str(i),
                    "name": name,
                    "active": name == "main",
                    "path": "/work",
                    "command": "bash",
                    "size": "80x24",
                    "activity": "0",
                }
                for i, name in enumerate(self.windows)
            ]
            return json.dumps(rows), 0, "done"
        if kind == "newwin":
            self.windows.setdefault(args.get("window") or "main", self._new_window())
            return "", 0, "done"
        if kind == "killwin":
            self.windows.pop(args.get("window") or "", None)
            return "", 0, "done"
        if kind == "shell":
            return "fake", 0, "done"
        if kind == "run":
            command = str(args.get("command") or "").strip()
            if not command:
                return "命令为空", 1, "error"
            state = self.windows.setdefault(window, self._new_window())
            state["lines"].append("$ " + command)
            state["lines"].append("ran: " + command)
            state["cur"] = "$ "
            return "ran: " + command, 0, "done"
        return "unknown command", 1, "error"

    def meta(self) -> dict:
        return {
            "host": "fake-host",
            "cwd": "/work",
            "pid": 1,
            "version": "test",
            "windows": [
                {
                    "index": str(i),
                    "name": name,
                    "active": name == "main",
                    "path": "/work",
                    "command": "bash",
                    "size": "80x24",
                }
                for i, name in enumerate(self.windows)
            ],
            "active": "main",
            "sync": "on",
        }


class FakeWorker(threading.Thread):
    """后台线程：像真工人一样长轮询、执行、回报。"""

    def __init__(self, url: str, secret: str, worker: str = "w1", wait: int = 5):
        super().__init__(daemon=True)
        self.client = Beacon(url, secret)
        self.worker = worker
        self.wait = wait
        self.shell = FakeShell()
        self.stop_flag = threading.Event()
        self.seen: List[str] = []
        self.error: Optional[str] = None

    def run(self) -> None:  # pragma: no cover - 线程主体
        last_panes = {}
        try:
            while not self.stop_flag.is_set():
                ret = self.client.poll(self.worker, self.shell.meta(), wait=self.wait)
                if ret.get("kill"):
                    return
                for cmd in ret.get("data") or []:
                    self.seen.append(cmd.get("kind") or "")
                    output, code, status = self.shell.handle(cmd)
                    self.client.call(
                        "report",
                        cmd_id=cmd["id"],
                        output=output,
                        exit_code=code,
                        status=status,
                    )
                # 与真工人一致：只上报内容有变化的窗口，且用压缩负载
                panes = {w: self.shell.pane(w) for w in self.shell.windows}
                changed = {w: c for w, c in panes.items() if last_panes.get(w) != c}
                last_panes = panes
                if changed:
                    blob = zlib.compress(json.dumps(changed).encode("utf-8"), 6)
                    self.client.call(
                        "panes",
                        worker=self.worker,
                        panes_json="z:" + base64.b64encode(blob).decode("ascii"),
                    )
        except Exception as exc:  # pragma: no cover
            self.error = f"{type(exc).__name__}: {exc}"

    def stop(self) -> None:
        self.stop_flag.set()


def _main() -> None:  # pragma: no cover - 手动联调用
    import os
    import signal
    import sys
    import time

    url = os.environ.get("besh_url") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not url:
        print("用法：besh_url=http://host:port?secret=xxx python fake_worker.py [id]")
        raise SystemExit(2)
    worker_id = sys.argv[2] if len(sys.argv) > 2 else "w1"
    fw = FakeWorker(url, "", worker_id)
    fw.start()
    print(f"假工人 {worker_id} 已连接 {fw.client.base}，Ctrl+C 退出", flush=True)

    def _bye(signum, frame):  # noqa: ARG001
        fw.stop()
        fw.join(timeout=6)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _bye)
    signal.signal(signal.SIGTERM, _bye)
    while fw.is_alive():
        time.sleep(1)
    if fw.error:
        print("假工人出错：" + fw.error, file=sys.stderr, flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    _main()
