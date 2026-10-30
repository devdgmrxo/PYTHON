"""Trava de execução (data/jobhunter.lock).

Princípio: na dúvida, bloquear. Só se remove uma trava com evidência clara de
que o processo dono não existe mais (ou de PID reutilizado).
"""

import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("jobhunter.lock")

CREATE_TIME_TOLERANCE = 2.0  # segundos


class LockError(Exception):
    pass


class LockHeldError(LockError):
    """Outra execução do JobHunter está ativa."""


class LockUncertainError(LockError):
    """Não foi possível decidir com segurança; conferir manualmente."""


class ProbeUnavailable(Exception):
    """Não há como verificar processos neste ambiente."""


@dataclass
class ProcessInfo:
    create_time: float
    cmdline: list[str] | None


def probe_process(pid: int) -> ProcessInfo | None:
    """Devolve ProcessInfo se o PID existe, None se não existe.

    Levanta ProbeUnavailable se a verificação não for possível.
    """
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            create_time = proc.create_time()
            try:
                cmdline = proc.cmdline()
            except (psutil.AccessDenied, psutil.ZombieProcess):
                cmdline = None
            return ProcessInfo(create_time, cmdline)
        except psutil.NoSuchProcess:
            return None
        except psutil.AccessDenied as exc:
            raise ProbeUnavailable(f"sem permissao para consultar PID {pid}") from exc
        except psutil.Error as exc:
            raise ProbeUnavailable(str(exc)) from exc
    if sys.platform.startswith("linux"):
        return _probe_proc_fs(pid)
    if sys.platform == "win32":
        return _probe_windows(pid)
    raise ProbeUnavailable("psutil ausente e sem metodo alternativo para esta plataforma")


def _probe_proc_fs(pid: int) -> ProcessInfo | None:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        stat = stat_path.read_text()
        fields = stat.rsplit(")", 1)[1].split()
        start_ticks = int(fields[19])
        boot_time = None
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime"):
                boot_time = int(line.split()[1])
        hz = os.sysconf("SC_CLK_TCK")
    except FileNotFoundError:
        return None
    except (OSError, ValueError, IndexError) as exc:
        raise ProbeUnavailable(str(exc)) from exc
    if boot_time is None:
        raise ProbeUnavailable("btime indisponivel")
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        cmdline = [p.decode(errors="replace") for p in raw.split(b"\0") if p]
    except OSError:
        cmdline = None
    return ProcessInfo(boot_time + start_ticks / hz, cmdline)


def _probe_windows(pid: int) -> ProcessInfo | None:  # pragma: no cover - só no Windows
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_INVALID_PARAMETER = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == ERROR_INVALID_PARAMETER:
            return None
        raise ProbeUnavailable(f"OpenProcess falhou (erro {err})")
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            raise ProbeUnavailable("GetProcessTimes falhou")
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ProcessInfo(ticks / 1e7 - 11644473600, None)
    finally:
        kernel32.CloseHandle(handle)


class RunLock:
    def __init__(self, path: Path, probe=probe_process):
        self.path = Path(path)
        self._probe = probe
        self.token: str | None = None

    # ---- aquisição -------------------------------------------------
    def _my_info(self) -> dict:
        pid = os.getpid()
        try:
            info = self._probe(pid)
        except ProbeUnavailable as exc:
            raise LockUncertainError(f"nao foi possivel obter dados do proprio processo: {exc}") from exc
        if info is None:
            raise LockUncertainError("proprio processo nao encontrado na verificacao")
        return {
            "pid": pid,
            "create_time": info.create_time,
            "cmdline": [sys.executable] + sys.argv,
            "token": uuid.uuid4().hex,
        }

    def _try_create(self, payload: dict) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except OSError as exc:
            raise LockUncertainError(f"sem permissao/erro ao criar {self.path}: {exc}") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise LockUncertainError(f"erro ao gravar {self.path}: {exc}") from exc
        return True

    def _read_existing(self) -> dict:
        """Lê a trava existente; repete algumas vezes porque o dono pode estar gravando."""
        last_error = "arquivo vazio"
        for _ in range(10):
            try:
                text = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise LockUncertainError(f"nao foi possivel ler {self.path}: {exc}") from exc
            try:
                data = json.loads(text)
                if (
                    isinstance(data, dict)
                    and isinstance(data.get("pid"), int)
                    and isinstance(data.get("create_time"), (int, float))
                    and isinstance(data.get("token"), str)
                    and data["token"]
                ):
                    return data
                last_error = "campos obrigatorios ausentes"
            except ValueError as exc:
                last_error = f"JSON invalido ({exc})"
            time.sleep(0.05)
        raise LockUncertainError(
            f"trava {self.path} corrompida ({last_error}); confira manualmente antes de remover"
        )

    def _is_stale(self, data: dict) -> bool:
        """True = evidência de processo morto/PID reutilizado; False = ativa."""
        try:
            info = self._probe(data["pid"])
        except ProbeUnavailable as exc:
            raise LockUncertainError(f"nao foi possivel verificar o PID {data['pid']}: {exc}") from exc
        if info is None:
            return True
        if abs(info.create_time - float(data["create_time"])) > CREATE_TIME_TOLERANCE:
            return True  # PID reutilizado por outro processo
        # PID e horário de início conferem: considera ativa. A linha de comando é
        # só informativa (varia entre 'python -m app.main' e 'python app/main.py').
        return False

    def _recover_stale(self, data: dict) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stale_path = self.path.with_name(f"{self.path.name}.{stamp}.{uuid.uuid4().hex}.stale")
        try:
            os.rename(self.path, stale_path)
        except FileNotFoundError:
            log.info("trava velha ja removida por outra execucao")
            return
        except OSError as exc:
            raise LockUncertainError(f"nao foi possivel renomear a trava velha: {exc}") from exc
        # Confere se o arquivo movido é mesmo o que foi avaliado como velho.
        try:
            moved = json.loads(stale_path.read_text(encoding="utf-8"))
            same = moved.get("token") == data["token"]
        except (OSError, ValueError):
            same = False
        if not same:
            try:
                os.link(stale_path, self.path)  # devolve sem sobrescrever
                os.unlink(stale_path)
            except OSError:
                pass
            raise LockUncertainError(
                f"a trava mudou durante a recuperacao; arquivo preservado em {stale_path}"
            )
        log.warning("trava velha (pid %s) renomeada para %s", data["pid"], stale_path.name)

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._my_info()
        if self._try_create(payload):
            self.token = payload["token"]
            log.info("trava adquirida (pid %s)", payload["pid"])
            return
        try:
            existing = self._read_existing()
            if not self._is_stale(existing):
                raise LockHeldError(
                    f"outra execucao do JobHunter esta ativa (pid {existing['pid']})"
                )
            self._recover_stale(existing)
        except FileNotFoundError:
            pass  # a trava sumiu entre a criação e a leitura; tenta adquirir de novo
        except LockUncertainError as exc:
            log.error("trava incerta: %s", exc)
            raise
        if self._try_create(payload):
            self.token = payload["token"]
            log.info("trava adquirida apos recuperacao (pid %s)", payload["pid"])
            return
        log.error("falha ao adquirir a trava apos recuperacao")
        raise LockHeldError("nao foi possivel adquirir a trava: outra execucao a obteve primeiro")

    # ---- liberação -------------------------------------------------
    def release(self) -> None:
        """Remove a trava somente se o token for desta execução."""
        if self.token is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.warning("trava ja nao existe na liberacao")
            self.token = None
            return
        except (OSError, ValueError) as exc:
            log.error("nao foi possivel confirmar a trava ao liberar (%s); mantida", exc)
            self.token = None
            return
        if data.get("token") == self.token:
            try:
                os.unlink(self.path)
                log.info("trava liberada")
            except OSError as exc:
                log.error("erro ao remover a trava: %s", exc)
        else:
            log.error("trava pertence a outra execucao; nao sera removida")
        self.token = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False
