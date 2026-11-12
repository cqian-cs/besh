# SPDX-FileCopyrightText: 2026-present cqian-cs <cqian.cs@qq.com>
#
# SPDX-License-Identifier: MIT
"""工人：tmux 命令拼装、命令分发、rclone 备份。"""

import subprocess

import pytest

from besh import worker as worker_mod
from besh.client import Beacon
from besh.worker import SESSION_WORKER, Worker


@pytest.fixture()
def recorded(monkeypatch):
    calls = []

    def fake_tmux_ok(*args, timeout=30):
        calls.append(args)
        return True, ""

    monkeypatch.setattr(worker_mod, "tmux_ok", fake_tmux_ok)
    monkeypatch.setattr(worker_mod, "session_exists", lambda name: True)
    monkeypatch.setattr(worker_mod, "window_exists", lambda *a, **k: True)
    return calls


def test_target_of():
    assert worker_mod.target_of("main") == f"={SESSION_WORKER}:main"
    assert worker_mod.target_of("") == f"={SESSION_WORKER}"


def test_send_keys_normal(recorded):
    worker_mod.send_keys("main", ["ls -l", "Enter"])
    assert recorded[-1] == ("send-keys", "-t", "=besh_worker:main", "ls -l", "Enter")


def test_send_keys_literal(recorded):
    worker_mod.send_keys("main", ['echo "Hello $USER"'], literal=True)
    assert recorded[-1] == (
        "send-keys",
        "-l",
        "-t",
        "=besh_worker:main",
        'echo "Hello $USER"',
    )


def test_send_keys_literal_first(recorded):
    """type_text：文本按字面发，最后一个参数当按键。"""
    worker_mod.send_keys("main", ["Enter", "Enter"], literal_first=True)
    assert recorded[-2][:2] == ("send-keys", "-l")
    assert recorded[-2][-1] == "Enter"
    assert recorded[-1] == ("send-keys", "-t", "=besh_worker:main", "Enter")


def test_send_keys_creates_missing_window(monkeypatch, recorded):
    created = []
    monkeypatch.setattr(worker_mod, "window_exists", lambda *a, **k: False)
    monkeypatch.setattr(worker_mod, "ensure_window", lambda name, wd, s=None: created.append(name))
    worker_mod.send_keys("gpu", ["ls"], workdir="/work")
    assert created == ["gpu"]
    assert recorded[-1] == ("send-keys", "-t", "=besh_worker:gpu", "ls")


def test_capture_strips_trailing_blank_lines(monkeypatch):
    monkeypatch.setattr(worker_mod, "tmux_ok", lambda *a, **k: (True, "$ ls\nfile1\n\n\n"))
    assert worker_mod.capture("main") == "$ ls\nfile1"


def test_capture_returns_empty_on_failure(monkeypatch):
    monkeypatch.setattr(worker_mod, "tmux_ok", lambda *a, **k: (False, "no server"))
    assert worker_mod.capture("main") == ""


def test_list_windows_parses(monkeypatch):
    line = "0\tmain\t1\t/work\tbash\t80x24\t1700000000"
    monkeypatch.setattr(worker_mod, "tmux_ok", lambda *a, **k: (True, line))
    rows = worker_mod.list_windows()
    assert rows[0]["name"] == "main"
    assert rows[0]["active"] is True
    assert rows[0]["size"] == "80x24"


def test_list_windows_empty_when_session_missing(monkeypatch):
    monkeypatch.setattr(worker_mod, "tmux_ok", lambda *a, **k: (False, "can't find session"))
    assert worker_mod.list_windows() == []


@pytest.fixture()
def fake_worker():
    return Worker("w1", Beacon("http://127.0.0.1:1", "s"), "/tmp/besh-w1")


def test_handle_unknown_kind(fake_worker):
    out, code, status = fake_worker.handle("nope", {})
    assert status == "error"
    assert code == 1


def test_handle_capture(fake_worker, monkeypatch):
    monkeypatch.setattr(worker_mod, "capture", lambda *a, **k: "$ hi")
    out, code, status = fake_worker.handle("capture", {"window": "main", "lines": 20})
    assert (out, code, status) == ("$ hi", 0, "done")


def test_handle_keys_error_reported(fake_worker, monkeypatch):
    monkeypatch.setattr(worker_mod, "send_keys", lambda *a, **k: "boom")
    out, code, status = fake_worker.handle("keys", {"window": "main", "keys": ["ls"]})
    assert (out, code, status) == ("boom", 1, "error")


def test_handle_windows_json(fake_worker, monkeypatch):
    monkeypatch.setattr(worker_mod, "list_windows", lambda: [{"name": "main"}])
    out, code, status = fake_worker.handle("windows", {})
    assert '"main"' in out
    assert code == 0


def test_handle_exit(fake_worker):
    out, code, status = fake_worker.handle("exit", {})
    assert fake_worker.stop is True


def test_meta_contains_windows(fake_worker, monkeypatch):
    monkeypatch.setattr(worker_mod, "list_windows", lambda: [{"name": "main", "active": True}])
    meta = fake_worker._meta()
    assert meta["active"] == "main"
    assert meta["windows"]


def test_sync_once_builds_rclone(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "Transferred: 1 / 1", "")

    monkeypatch.setattr(worker_mod.shutil, "which", lambda name: "/usr/bin/rclone")
    monkeypatch.setattr(worker_mod.subprocess, "run", fake_run)
    code, out = worker_mod.sync_once(str(tmp_path), "w1", "2026-01-02")
    assert code == 0
    assert "rclone" in calls[0][0]
    assert "copy" in calls[0]
    assert calls[0][3] == "cf:besh/2026-01-02/w1"


def test_sync_once_without_rclone(monkeypatch, tmp_path):
    monkeypatch.setattr(worker_mod.shutil, "which", lambda name: None)
    code, out = worker_mod.sync_once(str(tmp_path), "w1")
    assert code == 127
    assert "rclone" in out


def test_restore_once_builds_rclone(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(worker_mod.shutil, "which", lambda name: "/usr/bin/rclone")
    monkeypatch.setattr(worker_mod.subprocess, "run", fake_run)
    code, _ = worker_mod.restore_once(str(tmp_path), "w1", "2026-01-02", "out/a.txt")
    assert code == 0
    assert calls[0][2] == "cf:besh/2026-01-02/w1/out/a.txt"
    assert calls[0][3] == str(tmp_path)


def test_exec_dedup(fake_worker, monkeypatch):
    handled = []
    monkeypatch.setattr(fake_worker, "handle", lambda kind, args: (handled.append(kind) or ("", 0, "done")))
    reported = []
    monkeypatch.setattr(
        fake_worker.beacon, "call", lambda *a, **k: reported.append(k) or {"suc": True}
    )
    fake_worker._exec({"id": "c1", "kind": "capture", "args": {}})
    fake_worker._exec({"id": "c1", "kind": "capture", "args": {}})
    assert handled == ["capture"]
    assert reported[0]["cmd_id"] == "c1"


def test_resolve_window_prefers_active(monkeypatch):
    monkeypatch.setattr(
        worker_mod,
        "list_windows",
        lambda session=SESSION_WORKER: [
            {"name": "gpu", "active": False},
            {"name": "main", "active": True},
        ],
    )
    assert worker_mod.resolve_window("") == "main"
    assert worker_mod.resolve_window("gpu") == "gpu"


def test_resolve_window_fallbacks(monkeypatch):
    monkeypatch.setattr(
        worker_mod, "list_windows", lambda session=SESSION_WORKER: [{"name": "only"}]
    )
    assert worker_mod.resolve_window("") == "only"  # 没有 active 标记时取第一个
    monkeypatch.setattr(worker_mod, "list_windows", lambda session=SESSION_WORKER: [])
    assert worker_mod.resolve_window("") == ""  # 没有窗口时保持空


def test_send_keys_empty_window_targets_active(recorded, monkeypatch):
    """不带 :window 时，按键必须发到 SESSION:WINDOW，而不是裸会话。"""
    monkeypatch.setattr(
        worker_mod,
        "list_windows",
        lambda session=SESSION_WORKER: [{"name": "main", "active": True}],
    )
    worker_mod.send_keys("", ["ls -l", "Enter"])
    assert recorded[-1] == ("send-keys", "-t", "=besh_worker:main", "ls -l", "Enter")


def test_capture_empty_window_targets_active(monkeypatch):
    calls = []

    def fake_tmux_ok(*args, **kwargs):
        calls.append(args)
        return True, "$ "

    monkeypatch.setattr(worker_mod, "tmux_ok", fake_tmux_ok)
    monkeypatch.setattr(
        worker_mod,
        "list_windows",
        lambda session=SESSION_WORKER: [{"name": "main", "active": True}],
    )
    assert worker_mod.capture("") == "$ "
    assert calls[-1][3] == "=besh_worker:main"  # capture-pane ... -t TARGET
