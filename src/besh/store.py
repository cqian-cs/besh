# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""信标侧状态：**全部在内存，零磁盘写入**，且各项都有硬上限。

信标是"转发 + 状态"服务，没有需要持久化的业务数据：工人重连会自动重新
注册，命令重发即可。把状态写进 sqlite 只会让磁盘被刷屏、心跳和 WAL 反复
写满（实测一个工人每 2.5 秒就能产生近 1MB 写入），因此这里完全不落盘。

内存占用是硬上限的，不会随运行时间增长：

* 命令记录 ``MAX_CMDS`` 条（超出淘汰最老的已完成项），单条输出截断到 64KB
* 屏幕快照每个窗口只留末尾 ``PANE_LINES_MAX`` 行，工人下线即释放
* 工人表最多 ``MAX_WORKERS`` 个，超时未心跳的会被清掉

典型 10 个工人时总占用约几 MB，不会成为服务器负担。
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from .util import now_iso

# sent 状态多久没收到回执就重新投递（工人崩溃时命令不丢）
LEASE_SECONDS = 120
# 命令历史保留秒数
HISTORY_SECONDS = 24 * 3600

# ---- 内存硬上限 ----
MAX_CMDS = 1000  # 命令记录条数（含历史）
CMD_OUTPUT_LIMIT = 64 * 1024  # 单条命令输出保留上限
OLD_OUTPUT_LIMIT = 2 * 1024  # 超过 10 分钟的历史输出只留开头这么多
MAX_WORKERS = 200  # 工人表上限
PANE_LINES_MAX = 60  # 每个窗口缓存末尾多少行屏幕


def new_id(prefix: str = "") -> str:
    """可按时间排序的短 ID：20261111T175512-ab12cd34"""
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}{('-' + prefix) if prefix else ''}"


class Store:
    def __init__(self, dbs: Any = None):
        # dbs 保留参数是为了兼容既有调用；状态不再落库
        self.dbs = dbs
        self._cmds: "OrderedDict[str, dict]" = OrderedDict()
        self._queues: Dict[str, List[dict]] = {}
        self._sent: Dict[str, float] = {}
        self._workers: Dict[str, dict] = {}
        self._panes: Dict[str, Dict[str, dict]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 队列

    def add(self, worker: str, kind: str, args: Optional[dict] = None) -> dict:
        """把命令放进队列，返回命令字典。"""
        cmd = {
            "id": new_id(worker.lower()[:8]),
            "worker": worker,
            "kind": kind,
            "args": args or {},
            "created_at": now_iso(),
        }
        with self._lock:
            self._queues.setdefault(worker, []).append(cmd)
            self._cmds[cmd["id"]] = {
                "key": cmd["id"],
                "worker": worker,
                "kind": kind,
                "args": cmd["args"],
                "status": "pending",
                "created_at": cmd["created_at"],
                "updated_at": cmd["created_at"],
            }
        return cmd

    def claim(self, worker: str, limit: int = 16) -> List[dict]:
        """取走该工人待执行的命令（标记为 sent）。"""
        with self._lock:
            queue = self._queues.get(worker) or []
            taken = queue[:limit]
            del queue[:limit]
            stamp = now_iso()
            for cmd in taken:
                self._sent[cmd["id"]] = time.time()
                row = self._cmds.get(cmd["id"])
                if row is not None:
                    row.update({"status": "sent", "sent_at": stamp, "updated_at": stamp})
        return taken

    def requeue_stale(self) -> int:
        """把投递后迟迟没有回执的命令重新排队（工人可能已经重启）。"""
        deadline = time.time() - LEASE_SECONDS
        moved = 0
        with self._lock:
            stale = [cid for cid, ts in self._sent.items() if ts < deadline]
            for cmd_id in stale:
                self._sent.pop(cmd_id, None)
                row = self._cmds.get(cmd_id)
                if not row:
                    continue
                worker = str(row.get("worker") or "")
                if not worker:
                    continue
                queue = self._queues.setdefault(worker, [])
                if any(c["id"] == cmd_id for c in queue):
                    continue
                queue.insert(
                    0,
                    {
                        "id": cmd_id,
                        "worker": worker,
                        "kind": row.get("kind") or "",
                        "args": row.get("args") or {},
                        "created_at": row.get("created_at") or now_iso(),
                    },
                )
                moved += 1
        return moved

    def pending_count(self, worker: str) -> int:
        with self._lock:
            return len(self._queues.get(worker) or [])

    def cancel_pending(self, worker: str) -> int:
        """工人下线时清空它的队列（留下的命令已无意义）。"""
        with self._lock:
            queue = self._queues.pop(worker, [])
            for cmd in queue:
                row = self._cmds.get(cmd["id"])
                if row is not None:
                    row.update({"status": "cancel", "updated_at": now_iso()})
        return len(queue)

    # ------------------------------------------------------------------ 命令

    def finish(self, cmd_id: str, output: str = "", exit_code: int = 0, status: str = "done") -> dict:
        with self._lock:
            self._sent.pop(cmd_id, None)
            row = self._cmds.get(cmd_id)
            if row is not None:
                row.update(
                    {
                        "status": status,
                        "output": str(output or "")[:CMD_OUTPUT_LIMIT],
                        "exit_code": int(exit_code or 0),
                        "done_at": now_iso(),
                        "updated_at": now_iso(),
                    }
                )
            self._evict()
        return self.get_cmd(cmd_id)

    def get_cmd(self, cmd_id: str) -> Optional[dict]:
        with self._lock:
            row = self._cmds.get(cmd_id)
            return dict(row) if row else None

    def history(self, worker: str = "", limit: int = 20) -> List[dict]:
        with self._lock:
            rows = [dict(r) for r in self._cmds.values()]
        if worker:
            rows = [r for r in rows if r.get("worker") == worker]
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows[: max(1, limit)]

    def prune(self) -> None:
        """清理过期历史并裁掉老输出，保证内存占用有上限。"""
        deadline = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - HISTORY_SECONDS)
        )
        trim_before = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 600)
        )
        with self._lock:
            for cmd_id, row in list(self._cmds.items()):
                if row.get("status") in ("pending", "sent"):
                    continue
                created = row.get("created_at") or ""
                if created and created < deadline:
                    self._cmds.pop(cmd_id, None)
                    continue
                output = row.get("output") or ""
                if created and created < trim_before and len(output) > OLD_OUTPUT_LIMIT:
                    row["output"] = output[:OLD_OUTPUT_LIMIT]
            self._evict()
            self._drop_stale_workers()
            self._drop_orphan_queues(deadline)

    def _drop_orphan_queues(self, deadline: str) -> None:
        """工人长时间没出现过，它队列里排着的命令已经没有意义，清掉省内存。"""
        with self._lock:
            for worker in list(self._queues):
                row = self._workers.get(worker)
                seen = str((row or {}).get("seen_at") or "")
                if row is None or (seen and seen < deadline):
                    self._queues.pop(worker, None)
                    self._panes.pop(worker, None)

    def _evict(self) -> None:
        """超出上限时淘汰最老的已完成记录（未完成的命令不动）。"""
        with self._lock:
            if len(self._cmds) <= MAX_CMDS:
                return
            for cmd_id, row in list(self._cmds.items()):
                if len(self._cmds) <= MAX_CMDS:
                    break
                if row.get("status") in ("pending", "sent"):
                    continue
                self._cmds.pop(cmd_id, None)

    # ------------------------------------------------------------------ 工人

    def touch(self, worker: str, meta: Optional[dict] = None) -> None:
        meta = meta or {}
        data: Dict[str, Any] = {"key": worker, "seen_at": now_iso(), "online": True, "offline": False}
        with self._lock:
            row = self._workers.setdefault(worker, {"key": worker})
            row.update(data)
            for key in ("host", "cwd", "pid", "version", "active", "sync", "started_at", "note"):
                if key in meta and meta[key] not in (None, ""):
                    row[key] = meta[key]
            if "windows" in meta and isinstance(meta["windows"], list):
                row["windows"] = meta["windows"]
            self._drop_stale_workers()

    def get_worker(self, worker: str, online_seconds: int = 45) -> Optional[dict]:
        with self._lock:
            row = self._workers.get(worker)
            return self._decorate(dict(row), online_seconds) if row else None

    def list_workers(self, online_seconds: int = 45) -> List[dict]:
        with self._lock:
            rows = [self._decorate(dict(r), online_seconds) for r in self._workers.values()]
        rows.sort(key=lambda r: (not r["online"], str(r.get("key") or "").lower()))
        return rows

    @staticmethod
    def _decorate(row: dict, online_seconds: int) -> dict:
        seen = row.get("seen_at") or ""
        fresh = bool(seen) and _seconds_since(seen) <= online_seconds
        row["online"] = fresh and not row.get("offline")
        row["worker"] = row.get("key")
        windows = row.get("windows")
        row["windows"] = windows if isinstance(windows, list) else []
        return row

    def _drop_stale_workers(self) -> None:
        """工人下线超过 24 小时就清掉，避免内存里越积越多。"""
        with self._lock:
            if len(self._workers) <= MAX_WORKERS:
                return
            items = sorted(self._workers.items(), key=lambda kv: str(kv[1].get("seen_at") or ""))
            for worker, _row in items[: len(self._workers) - MAX_WORKERS]:
                self._workers.pop(worker, None)
                self._panes.pop(worker, None)
                self._queues.pop(worker, None)

    def set_offline(self, worker: str, note: str = "") -> None:
        with self._lock:
            row = self._workers.setdefault(worker, {"key": worker})
            row.update({"online": False, "offline": True, "seen_at": now_iso()})
            if note:
                row["note"] = note
            # 工人走了，缓存的屏幕没有意义，直接释放内存
            self._panes.pop(worker, None)

    def set_flag(self, worker: str, **flags: Any) -> None:
        with self._lock:
            row = self._workers.setdefault(worker, {"key": worker})
            row.update(flags)

    def get_flag(self, worker: str, name: str) -> Any:
        with self._lock:
            row = self._workers.get(worker)
            return row.get(name) if row else None

    # ------------------------------------------------------------------ 屏幕

    def put_panes(self, worker: str, panes: Dict[str, str]) -> int:
        """屏幕快照是"秒回"用的缓存：只留末尾若干行，不落盘。"""
        stamp = now_iso()
        with self._lock:
            bucket = self._panes.setdefault(worker, {})
            for window, content in panes.items():
                if not isinstance(window, str) or not isinstance(content, str):
                    continue
                lines = content.splitlines()[-PANE_LINES_MAX:]
                bucket[window] = {
                    "worker": worker,
                    "window": window,
                    "content": "\n".join(lines),
                    "updated_at": stamp,
                }
            return len(bucket)

    def get_panes(self, worker: str) -> Dict[str, dict]:
        with self._lock:
            return dict(self._panes.get(worker) or {})

    def get_pane(self, worker: str, window: str) -> Optional[dict]:
        with self._lock:
            row = (self._panes.get(worker) or {}).get(window)
        return dict(row) if row else None

    def prune_panes(self, worker: str, valid: List[str]) -> int:
        """删掉已经关掉的窗口的屏幕缓存，避免缓存无限增长。"""
        keep = set(valid)
        with self._lock:
            bucket = self._panes.get(worker) or {}
            gone = [name for name in bucket if name not in keep]
            for name in gone:
                bucket.pop(name, None)
        return len(gone)


def _seconds_since(stamp: str) -> float:
    try:
        return max(0.0, time.time() - time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S")))
    except (TypeError, ValueError):
        return 1e9
