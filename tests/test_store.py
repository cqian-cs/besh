# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""信标侧状态存储：命令队列、线程安全的领取、屏幕缓存。"""

import time

import pytest
import sqless

from besh import store as store_mod
from besh.store import Store


@pytest.fixture()
def st(tmp_path):
    dbs = sqless.DBS(str(tmp_path / "db"))
    return Store(dbs)


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


def test_restart_recovers_pending(tmp_path):
    dbs = sqless.DBS(str(tmp_path / "db"))
    first = Store(dbs)
    pending = first.add("w1", "capture", {"window": "main"})
    sent = first.add("w1", "capture", {})
    first.claim("w1")  # 两条都变成 sent

    second = Store(sqless.DBS(str(tmp_path / "db")))
    # sent 状态的命令在重启后会被重新投递
    recovered = second.claim("w1")
    ids = {c["id"] for c in recovered}
    assert pending["id"] in ids or sent["id"] in ids


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
