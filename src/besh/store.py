# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""信标侧的状态存储（sqless 关系表 + 内存队列）。

三张表：

* ``besh-workers``  key=工人ID，记录主机名、工作目录、窗口列表、心跳时间
* ``besh-cmds``     key=命令ID，记录待执行/已执行命令与输出（审计 + 断点续投）
* ``besh-panes``    key=``工人ID:窗口名``，缓存最近的屏幕内容，供 capture-pane 秒回

命令队列在内存中维护（信标单进程），启动时从 ``besh-cmds`` 恢复未完成的命令，
因此信标重启不会丢命令。
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Dict, List, Optional

from .util import now_iso

# sent 状态多久没收到回执就重新投递（工人崩溃时命令不丢）
LEASE_SECONDS = 120
# 命令历史保留秒数
HISTORY_SECONDS = 24 * 3600


def new_id(prefix: str = "") -> str:
    """可按时间排序的短 ID：20261111T175512-ab12cd34"""
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}{('-' + prefix) if prefix else ''}"


class Store:
    def __init__(self, dbs: Any):
        self.dbs = dbs
        self.t_workers = dbs["besh-workers"]
        self.t_cmds = dbs["besh-cmds"]
        self.t_panes = dbs["besh-panes"]
        self._queues: Dict[str, List[dict]] = {}
        # 屏幕快照缓存（只放内存，见 put_panes 的说明）
        self._panes: Dict[str, Dict[str, dict]] = {}
        self._lock = threading.RLock()
        self._restore()

    # ------------------------------------------------------------------ 队列

    def _restore(self) -> None:
        """信标重启后，把未完成的命令重新放回队列。

        ``sent`` 状态（已投递但无回执）也重新排队：工人侧用 ``_done``
        去重，所以最坏情况是重复执行一次（at-least-once），不会丢命令。
        """
        self._sent: Dict[str, float] = {}
        try:
            rows = list(self.t_cmds.iter(""))
        except Exception:  # pragma: no cover - 表损坏时不阻塞启动
            rows = []
        for row in rows:
            row = dict(row)
            worker = row.get("worker")
            status = row.get("status")
            if not worker or status not in ("pending", "sent"):
                continue
            cmd = {
                "id": row.get("key"),
                "worker": worker,
                "kind": row.get("kind") or "",
                "args": row.get("args") or {},
                "created_at": row.get("created_at") or now_iso(),
            }
            if not cmd["id"]:
                continue
            self._queues.setdefault(worker, []).append(cmd)
        for queue in self._queues.values():
            queue.sort(key=lambda c: c.get("created_at") or "")

    def add(self, worker: str, kind: str, args: Optional[dict] = None) -> dict:
        """把命令放进队列并落库，返回命令字典。"""
        cmd = {
            "id": new_id(worker.lower()[:8]),
            "worker": worker,
            "kind": kind,
            "args": args or {},
            "created_at": now_iso(),
        }
        with self._lock:
            self._queues.setdefault(worker, []).append(cmd)
        self.t_cmds.upsert(
            {
                cmd["id"]: {
                    "worker": worker,
                    "kind": kind,
                    "args": cmd["args"],
                    "status": "pending",
                    "created_at": cmd["created_at"],
                    "updated_at": cmd["created_at"],
                }
            }
        )
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
            self.t_cmds.upsert(
                {cmd["id"]: {"status": "sent", "sent_at": stamp, "updated_at": stamp}}
            )
        return taken

    def requeue_stale(self) -> int:
        """把投递后迟迟没有回执的命令重新排队（工人可能已经重启）。"""
        deadline = time.time() - LEASE_SECONDS
        stale = [cid for cid, ts in self._sent.items() if ts < deadline]
        moved = 0
        for cmd_id in stale:
            self._sent.pop(cmd_id, None)
            row = self.get_cmd(cmd_id)
            if not row:
                continue
            worker = str(row.get("worker") or "")
            if not worker:
                continue
            with self._lock:
                queued = {c["id"] for c in self._queues.get(worker, [])}
                if cmd_id in queued:
                    continue
                self._queues.setdefault(worker, []).insert(
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
            self.t_cmds.upsert(
                {cmd["id"]: {"status": "cancel", "updated_at": now_iso()}}
            )
        return len(queue)

    # ------------------------------------------------------------------ 命令

    def finish(self, cmd_id: str, output: str = "", exit_code: int = 0, status: str = "done") -> dict:
        self._sent.pop(cmd_id, None)
        self.t_cmds.upsert(
            {
                cmd_id: {
                    "status": status,
                    "output": output or "",
                    "exit_code": int(exit_code or 0),
                    "done_at": now_iso(),
                    "updated_at": now_iso(),
                }
            }
        )
        return self.get_cmd(cmd_id)

    def get_cmd(self, cmd_id: str) -> Optional[dict]:
        row = self.t_cmds.get_item(cmd_id)
        return dict(row) if row else None

    def history(self, worker: str = "", limit: int = 20) -> List[dict]:
        rows = [dict(r) for r in self.t_cmds.iter("")]
        if worker:
            rows = [r for r in rows if r.get("worker") == worker]
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows[: max(1, limit)]

    def prune(self) -> None:
        """清理过期历史，避免 sqlite 无限增长。"""
        deadline = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - HISTORY_SECONDS)
        )
        for row in list(self.t_cmds.iter("")):
            created = row.get("created_at") or ""
            if not created or created >= deadline:
                continue
            if row.get("status") in ("pending", "sent"):
                continue
            key = row.get("key")
            if key:
                self.t_cmds.delete(f'key = "{key}"')

    # ------------------------------------------------------------------ 工人

    def touch(self, worker: str, meta: Optional[dict] = None) -> None:
        meta = meta or {}
        data: Dict[str, Any] = {"seen_at": now_iso(), "online": True, "offline": False}
        for key in ("host", "cwd", "pid", "version", "active", "sync", "started_at", "note"):
            if key in meta and meta[key] not in (None, ""):
                data[key] = meta[key]
        if "windows" in meta and isinstance(meta["windows"], list):
            data["windows"] = meta["windows"]
        self.t_workers.upsert({worker: data})

    def get_worker(self, worker: str, online_seconds: int = 45) -> Optional[dict]:
        row = self.t_workers.get_item(worker)
        if not row:
            return None
        return self._decorate(dict(row), online_seconds)

    def list_workers(self, online_seconds: int = 45) -> List[dict]:
        rows = [self._decorate(dict(r), online_seconds) for r in self.t_workers.iter("")]
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

    def set_offline(self, worker: str, note: str = "") -> None:
        data: Dict[str, Any] = {"online": False, "offline": True, "seen_at": now_iso()}
        if note:
            data["note"] = note
        self.t_workers.upsert({worker: data})

    def set_flag(self, worker: str, **flags: Any) -> None:
        self.t_workers.upsert({worker: flags})

    def get_flag(self, worker: str, name: str) -> Any:
        row = self.t_workers.get_item(worker)
        return row.get(name) if row else None

    # ------------------------------------------------------------------ 屏幕

    def put_panes(self, worker: str, panes: Dict[str, str]) -> int:
        """屏幕快照只放内存：它是"秒回"用的缓存，不需要持久化。

        落盘会让磁盘 IO 随刷屏频率线性上升（每次全屏推送就是上百 KB 的
        sqlite 写入），信标重启后缓存自然失效，工人下一次推送就会补上。
        """
        stamp = now_iso()
        with self._lock:
            bucket = self._panes.setdefault(worker, {})
            for window, content in panes.items():
                if not isinstance(window, str) or not isinstance(content, str):
                    continue
                bucket[window] = {
                    "worker": worker,
                    "window": window,
                    "content": content,
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
