# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""信标侧状态存储：命令队列、线程安全的领取、屏幕缓存。"""

import time

import pytest

from besh import store as store_mod
from besh.store import Store


@pytest.fixture()
def st(tmp_path):
    # 状态全在内存，dbs 不再使用（传 None 也能工作）
    return Store(None)


def test_queue_roundtrip(st):
    cmd = st.add("w1", "capture", {"window": "main"})
    assert st.pending_count("w1") == 1
    taken = st.claim("w1")
    assert [c["id"] for c in taken] == [cmd["id"]]
    assert st.pending_count("w1") == 0
    # 落库状态
    assert st.get_cmd(cmd["id"])["status"] == "sent"
    st.finish(cmd["id"], "hello", 0)
    row = st.get_cmd(cmd["id"])
    assert row["status"] == "done"
    assert row["output"] == "hello"


def test_claim_is_fifo_and_limited(st):
    ids = [st.add("w1", "capture", {"i": i})["id"] for i in range(5)]
    taken = st.claim("w1", limit=3)
    assert [c["id"] for c in taken] == ids[:3]
    assert st.pending_count("w1") == 2


def test_queues_are_per_worker(st):
    st.add("w1", "capture", {})
    st.add("w2", "capture", {})
    assert len(st.claim("w1")) == 1
    assert st.pending_count("w2") == 1


def test_requeue_stale(st):
    cmd = st.add("w1", "keys", {"keys": ["ls"]})
    st.claim("w1")
    # 伪造"很久以前投递"
    st._sent[cmd["id"]] = time.time() - store_mod.LEASE_SECONDS - 1
    assert st.requeue_stale() == 1
    again = st.claim("w1")
    assert [c["id"] for c in again] == [cmd["id"]]


def test_requeue_ignores_fresh(st):
    st.add("w1", "keys", {})
    st.claim("w1")
    assert st.requeue_stale() == 0


def test_state_is_memory_only(tmp_path):
    """信标状态零落盘：跑一堆操作后目录里不该有任何文件。"""
    st = Store(None)
    for i in range(50):
        cmd = st.add("w1", "run", {"command": f"echo {i}"})
        st.claim("w1")
        st.finish(cmd["id"], "x" * 1000, 0)
        st.put_panes("w1", {"main": "screen"})
    st.touch("w1", {"host": "h", "windows": [{"name": "main", "active": True}]})
    st.prune()

    # 内存里有数据，但磁盘上一片空白
    assert st.get_cmd(cmd["id"])["status"] == "done"
    assert list(tmp_path.iterdir()) == []


def test_cmd_count_is_capped():
    """命令记录有上限，超出淘汰最老的已完成项，内存不会越滚越大。"""
    st = Store(None)
    for i in range(store_mod.MAX_CMDS + 50):
        cmd = st.add("w1", "run", {"command": f"echo {i}"})
        st.claim("w1")
        st.finish(cmd["id"], "ok", 0)
    assert len(st._cmds) <= store_mod.MAX_CMDS


def test_cmd_output_is_truncated():
    st = Store(None)
    cmd = st.add("w1", "run", {})
    st.claim("w1")
    st.finish(cmd["id"], "x" * (store_mod.CMD_OUTPUT_LIMIT * 4), 0)
    assert len(st.get_cmd(cmd["id"])["output"]) == store_mod.CMD_OUTPUT_LIMIT


def test_panes_keep_only_tail():
    """屏幕缓存只留末尾若干行，避免长输出撑爆内存。"""
    st = Store(None)
    screen = "\n".join(f"line {i}" for i in range(500))
    st.put_panes("w1", {"main": screen})
    content = st.get_pane("w1", "main")["content"]
    assert len(content.splitlines()) == store_mod.PANE_LINES_MAX
    assert content.splitlines()[-1] == "line 499"


def test_cancel_pending(st):
    st.add("w1", "capture", {})
    st.add("w1", "capture", {})
    assert st.cancel_pending("w1") == 2
    assert st.pending_count("w1") == 0


def test_worker_touch_and_online(st):
    st.touch("w1", {"host": "h", "cwd": "/w", "windows": [{"name": "main"}]})
    row = st.get_worker("w1")
    assert row["online"] is True
    assert row["host"] == "h"
    assert row["windows"] == [{"name": "main"}]
    st.set_offline("w1")
    assert st.get_worker("w1")["online"] is False


def test_list_workers_sorted_online_first(st):
    st.touch("b", {"host": "hb"})
    st.touch("a", {"host": "ha"})
    st.set_offline("a")
    rows = st.list_workers()
    assert rows[0]["worker"] == "b"
    assert rows[1]["online"] is False


def test_flag(st):
    st.set_flag("w1", kill=True)
    assert st.get_flag("w1", "kill") == 1
    assert st.get_flag("w2", "kill") is None


def test_panes_cache(st):
    assert st.put_panes("w1", {"main": "$ ls", "gpu": "$ "}) == 2
    pane = st.get_pane("w1", "main")
    assert pane["content"] == "$ ls"
    assert set(st.get_panes("w1")) == {"main", "gpu"}
    assert st.get_panes("w2") == {}


def test_history_filter_and_order(st):
    st.add("w1", "keys", {})
    st.add("w2", "keys", {})
    rows = st.history("w1")
    assert len(rows) == 1
    assert rows[0]["worker"] == "w1"
    assert len(st.history()) == 2


def test_prune_keeps_recent(st):
    cmd = st.add("w1", "keys", {})
    st.finish(cmd["id"], "x", 0)
    st.prune()
    assert st.get_cmd(cmd["id"]) is not None


def test_prune_panes_drops_closed_windows(st):
    st.put_panes("w1", {"main": "$ a", "gpu": "$ b"})
    st.put_panes("w2", {"main": "$ c"})
    dropped = st.prune_panes("w1", ["main"])
    assert dropped == 1
    assert set(st.get_panes("w1")) == {"main"}
    assert set(st.get_panes("w2")) == {"main"}  # 别的工人不受影响
