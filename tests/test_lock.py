import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import app.lock as lock_module
from app.lock import (
    LockHeldError,
    LockUncertainError,
    ProbeUnavailable,
    ProcessInfo,
    RunLock,
    probe_process,
)

ROOT = Path(__file__).resolve().parent.parent


def write_lock(path, **overrides):
    data = {"pid": os.getpid(), "create_time": probe_process(os.getpid()).create_time,
            "cmdline": ["python"], "token": "outro-token"}
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def stale_files(path):
    return list(path.parent.glob(path.name + ".*.stale"))


def test_acquire_and_release(tmp_path):
    path = tmp_path / "jobhunter.lock"
    lock = RunLock(path)
    lock.acquire()
    data = json.loads(path.read_text())
    assert data["pid"] == os.getpid() and data["token"] and data["cmdline"] and data["create_time"]
    lock.release()
    assert not path.exists()


def test_second_acquire_is_blocked_while_active(tmp_path):
    path = tmp_path / "jobhunter.lock"
    first = RunLock(path)
    first.acquire()
    with pytest.raises(LockHeldError):
        RunLock(path).acquire()
    assert path.exists()
    first.release()


def test_pid_reuse_is_recovered_and_renamed_to_stale(tmp_path):
    path = tmp_path / "jobhunter.lock"
    write_lock(path, create_time=1.0)  # PID existe, mas começou em outro momento
    lock = RunLock(path)
    lock.acquire()
    stale = stale_files(path)
    assert len(stale) == 1
    assert json.loads(stale[0].read_text())["token"] == "outro-token"
    assert json.loads(path.read_text())["token"] == lock.token
    lock.release()


def test_dead_pid_is_recovered(tmp_path):
    path = tmp_path / "jobhunter.lock"
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    write_lock(path, pid=proc.pid, create_time=time.time() - 100)
    lock = RunLock(path)
    lock.acquire()
    assert len(stale_files(path)) == 1
    lock.release()


def test_corrupted_file_aborts_and_is_preserved(tmp_path):
    path = tmp_path / "jobhunter.lock"
    path.write_text("lixo{{{", encoding="utf-8")
    with pytest.raises(LockUncertainError):
        RunLock(path).acquire()
    assert path.read_text() == "lixo{{{"
    assert stale_files(path) == []


def test_permission_error_aborts(tmp_path, monkeypatch):
    path = tmp_path / "jobhunter.lock"

    def deny(*args, **kwargs):
        raise PermissionError("negado")

    monkeypatch.setattr(lock_module.os, "open", deny)
    with pytest.raises(LockUncertainError):
        RunLock(path).acquire()


def test_probe_unavailable_aborts_and_preserves(tmp_path):
    path = tmp_path / "jobhunter.lock"
    write_lock(path, pid=999999)
    me = os.getpid()

    def probe(pid):
        if pid == me:
            return ProcessInfo(time.time(), None)
        raise ProbeUnavailable("sem acesso")

    with pytest.raises(LockUncertainError):
        RunLock(path, probe=probe).acquire()
    assert path.exists() and stale_files(path) == []


def test_exception_during_run_releases_lock(tmp_path):
    path = tmp_path / "jobhunter.lock"
    with pytest.raises(RuntimeError):
        with RunLock(path):
            assert path.exists()
            raise RuntimeError("falha")
    assert not path.exists()


def test_release_never_removes_lock_of_another_token(tmp_path):
    path = tmp_path / "jobhunter.lock"
    lock = RunLock(path)
    lock.acquire()
    path.write_text(json.dumps({"pid": 1, "create_time": 1, "token": "de-outro", "cmdline": []}))
    lock.release()
    assert path.exists() and json.loads(path.read_text())["token"] == "de-outro"


def test_release_without_acquire_does_nothing(tmp_path):
    path = tmp_path / "jobhunter.lock"
    write_lock(path)
    RunLock(path).release()
    assert path.exists()


def test_rename_failure_preserves_files_and_aborts(tmp_path, monkeypatch):
    path = tmp_path / "jobhunter.lock"
    write_lock(path, create_time=1.0)

    def fail(*args, **kwargs):
        raise OSError("rename falhou")

    monkeypatch.setattr(lock_module.os, "rename", fail)
    with pytest.raises(LockUncertainError):
        RunLock(path).acquire()
    assert path.exists() and stale_files(path) == []


def test_failure_to_acquire_after_recovery(tmp_path, monkeypatch):
    path = tmp_path / "jobhunter.lock"
    write_lock(path, create_time=1.0)
    lock = RunLock(path)
    calls = {"n": 0}
    original = RunLock._try_create

    def racing(self, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return original(self, payload)  # arquivo existe: False
        return False  # outro processo ganhou a corrida

    monkeypatch.setattr(RunLock, "_try_create", racing)
    with pytest.raises(LockHeldError):
        lock.acquire()
    assert len(stale_files(path)) == 1  # a trava velha foi preservada como .stale


def test_two_concurrent_processes_only_one_wins(tmp_path):
    path = tmp_path / "jobhunter.lock"
    script = (
        "import sys, time\n"
        "from app.lock import RunLock, LockError\n"
        "lock = RunLock(sys.argv[1])\n"
        "try:\n"
        "    lock.acquire()\n"
        "except LockError:\n"
        "    print('LOCKED', flush=True); sys.exit(0)\n"
        "print('ACQUIRED', flush=True)\n"
        "time.sleep(2.5)\n"
        "lock.release()\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(path)], cwd=ROOT,
                         stdout=subprocess.PIPE, text=True)
        for _ in range(4)
    ]
    outputs = [p.communicate(timeout=30)[0].strip() for p in procs]
    assert outputs.count("ACQUIRED") == 1
    assert outputs.count("LOCKED") == 3
    assert not path.exists()
