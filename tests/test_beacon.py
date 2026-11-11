# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""信标 API：在进程内跑一个假工人，验证命令下发与屏幕回读。"""

import asyncio
import json

import pytest
import sqless

import besh.beacon as B
from tests.fake_worker import FakeShell


class FakeRemote:
    """进程内的假工人：长轮询 + 执行 + 回报。"""

    def __init__(self, worker: str = "w1"):
        self.worker = worker
        self.shell = FakeShell()
        self.task = None
        self.killed = False

    async def loop(self):
        while True:
            ret = await B.poll(self.worker, json.dumps(self.shell.meta()), 2)
            if ret.get("kill"):
                self.killed = True
                return
            for cmd in ret.get("data") or []:
                output, code, status = self.shell.handle(cmd)
                B.report(cmd["id"], output, code, status)


@pytest.fixture()
def bound(tmp_path):
    dbs = sqless.DBS(str(tmp_path / "db"))
    B.STATE.update({"cfg": {}, "dbs": dbs, "store": None, "app": ""})
    B._WAKE.clear()
    B._RESULT.clear()
    B._REQUEUE_AT.clear()
    B.bind({"path_base_db": str(tmp_path / "db")}, dbs)
    return dbs


@pytest.mark.asyncio
async def test_send_keys_then_capture(bound):
    remote = FakeRemote()
    task = asyncio.create_task(remote.loop())
    try:
        ret = await B.send_keys("w1", json.dumps(["echo hi", "Enter"]), window="main")
        assert ret["suc"] is True
        cap = await B.capture_pane("w1", "main", lines=50, escapes=False, refresh=True)
        assert cap["suc"] is True
        assert "echo hi" in cap["content"]
        assert cap["live"] is True
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_run_returns_output_and_exit_code(bound):
    remote = FakeRemote()
    task = asyncio.create_task(remote.loop())
    try:
        ret = await B.run("w1", "uname -a", window="main", timeout=10)
        assert ret["suc"] is True, ret
        assert ret["exit_code"] == 0
        assert "ran: uname -a" in ret["output"]
        # 哨兵与提示符都不能留在输出里
        assert "__BESH_" not in ret["output"]
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_type_text(bound):
    remote = FakeRemote()
    task = asyncio.create_task(remote.loop())
    try:
        await B.type_text("w1", "hello world", window="main")
        cap = await B.capture_pane("w1", "main", refresh=True)
        assert "hello world" in cap["content"]
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_windows_and_new_window(bound):
    remote = FakeRemote()
    task = asyncio.create_task(remote.loop())
    try:
        ret = await B.list_windows("w1", refresh=True, timeout=5)
        assert ret["live"] is True
        assert [w["name"] for w in ret["data"]] == ["main"]

        await B.new_window("w1", "gpu")
        ret = await B.list_windows("w1", refresh=True, timeout=5)
        assert "gpu" in [w["name"] for w in ret["data"]]

        await B.kill_window("w1", "gpu")
        ret = await B.list_windows("w1", refresh=True, timeout=5)
        assert "gpu" not in [w["name"] for w in ret["data"]]
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_panes_cache_and_offline_fallback(bound):
    B.panes("w1", json.dumps({"main": "$ cached"}))
    ret = await B.capture_pane("w1", "main", refresh=False)
    assert ret["content"] == "$ cached"
    assert ret["live"] is False


@pytest.mark.asyncio
async def test_list_workers_online(bound):
    remote = FakeRemote()
    task = asyncio.create_task(remote.loop())
    try:
        await asyncio.sleep(0.3)
        ret = B.list_workers()
        assert ret["data"][0]["worker"] == "w1"
        assert ret["data"][0]["online"] is True
        assert ret["data"][0]["windows"] == ["main"]
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_kill_worker_flag(bound):
    remote = FakeRemote()
    task = asyncio.create_task(remote.loop())
    try:
        await asyncio.sleep(0.2)
        B.kill_worker("w1")
        await asyncio.wait_for(task, timeout=5)
        assert remote.killed is True
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_capture_timeout_falls_back(bound):
    # 没有工人时不应该抛异常，而是给出 suc=False
    ret = await B.capture_pane("nobody", "main", refresh=True)
    assert ret["suc"] is False
    assert "没有响应" in ret["msg"]


def test_extract_run():
    content = "\n".join(
        [
            "$ ls",
            "file1",
            '$ echo hi; echo "__BESH_ab12__$?"',
            "hi",
            "__BESH_ab12__0",
            "$ ",
        ]
    )
    code, body = B._extract_run(content, "__BESH_ab12__")
    assert code == 0
    assert body == "hi"


def test_pane_idle_detects_prompt():
    assert B._pane_idle("$ ls\nfile1\n$ ") is True
    assert B._pane_idle("data %") is True
    assert B._pane_idle("PS C:\\Users> ") is True
    assert B._pane_idle("$ python train.py") is False
    assert B._pane_idle("running...") is False
    assert B._pane_idle("") is False
    assert B._pane_idle("\n\n") is False


@pytest.mark.asyncio
async def test_wait_idle_returns_when_prompt_back(bound):
    remote = FakeRemote()
    task = asyncio.create_task(remote.loop())
    try:
        await B.type_text("w1", "hello world", window="main")
        ret = await B.wait_idle("w1", "main", timeout=15)
        assert ret["suc"] is True, ret
        assert "ran: hello world" in ret["content"]
        assert ret["content"].splitlines()[-1].strip() == "$"
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_wait_idle_no_worker_fails(bound):
    ret = await B.wait_idle("nobody", "main", timeout=3)
    assert ret["suc"] is False


def test_extract_run_nonzero_and_multiline():
    content = "\n".join(
        [
            '$ make; echo "__BESH_ff__$?"',
            "line1",
            "line2",
            "__BESH_ff__2",
        ]
    )
    code, body = B._extract_run(content, "__BESH_ff__")
    assert code == 2
    assert body == "line1\nline2"


def test_extract_run_missing_marker():
    code, body = B._extract_run("$ ls\nfile\n$ ", "__BESH_none__")
    assert code is None
    assert body == ""


def test_hello(bound):
    ret = B.hello()
    assert ret["suc"] is True
    assert ret["name"] == "besh beacon"


def test_parse_keys():
    assert B._parse_keys('["ls -l", "Enter"]') == ["ls -l", "Enter"]
    assert B._parse_keys("ls -l") == ["ls -l"]
    assert B._parse_keys("") == []
