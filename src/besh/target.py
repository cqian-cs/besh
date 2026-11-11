# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""目标解析：把 tmux 风格的 `WORKER:WINDOW` 拆成 (worker, window)。"""

from __future__ import annotations

from typing import NamedTuple

from .util import BeshError, safe_id, safe_window


class Target(NamedTuple):
    worker: str
    window: str  # 空字符串表示"工人当前活动窗口"

    def __str__(self) -> str:
        return f"{self.worker}:{self.window}" if self.window else self.worker


def parse_target(value: str, default_window: str = "") -> Target:
    """解析 `-t` 参数。

    ``W1``            -> worker=W1, window=""
    ``W1:main``       -> worker=W1, window="main"
    ``W1:``           -> worker=W1, window=""
    """
    text = str(value or "").strip()
    if not text:
        raise BeshError("缺少 -t 参数，例如 -t WORKER_ID 或 -t WORKER_ID:WINDOW")
    worker, _, window = text.partition(":")
    worker = safe_id(worker, "工人ID")
    window = safe_window(window) or safe_window(default_window)
    return Target(worker, window)
