"""Own the local OpenViking + embedding processes for one workspace.

One instance per configured workspace state root. Listen on loopback only.
Reuse a live owned instance. Never kill a PID without command-line evidence
that it is this instance. Windows must not overwrite a database file that is
still open.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from vaws_knowledge.local.embedding import EMBEDDING_DIMENSION, EMBEDDING_MODEL

OPENVIKING_VERSION = "0.4.19"
LOOPBACK = "127.0.0.1"
_PROXY_KEYS = {
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "ftp_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "FTP_PROXY",
}


def venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    bindir = Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")
    candidates = [bindir / name]
    if os.name == "nt":
        candidates.insert(0, bindir / f"{name}.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def loopback_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for loopback services: never inherit a SOCKS/HTTP proxy."""

    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key in _PROXY_KEYS or key.lower() in {item.lower() for item in _PROXY_KEYS}:
            env.pop(key, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env.update(
        {
            "OMP_NUM_THREADS": "4",
            "OPENBLAS_NUM_THREADS": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


@contextmanager
def without_proxies() -> Iterator[None]:
    saved = {key: os.environ.pop(key) for key in list(os.environ) if key in _PROXY_KEYS}
    previous_no = {key: os.environ.get(key) for key in ("NO_PROXY", "no_proxy")}
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    try:
        yield
    finally:
        for key, value in saved.items():
            os.environ[key] = value
        for key, value in previous_no.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _popen_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind((LOOPBACK, 0))
        return int(sock.getsockname()[1])


def _health(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def pid_alive(pid: int) -> bool:
    """True when *pid* still refers to a running process. Never a kill."""

    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "state="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    state = (completed.stdout or "").strip()
    if state.upper().startswith("Z"):
        return False
    return True


def _windows_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0x102
    finally:
        kernel.CloseHandle(handle)


def process_command(pid: int) -> str:
    if not pid_alive(pid):
        return ""
    try:
        if os.name == "nt":
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            # Ownership markers can follow long executable/config paths.
            # Ignore terminal width so a live owned process is not missed.
            completed = subprocess.run(
                ["ps", "-ww", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=5,
            )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (completed.stdout or "").strip()


def owned_process(pid: int, marker: str) -> bool:
    """True only when *pid* is alive and its command line carries *marker*."""

    if not marker or not pid_alive(pid):
        return False
    command = process_command(pid)
    return bool(command) and marker in command


def stop_owned_pid(pid: int, marker: str) -> bool:
    """Stop *pid* only with command-line ownership. Stale PIDs are left alone."""

    if not owned_process(pid, marker):
        return False
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T"],
                capture_output=True,
                timeout=8,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.TimeoutExpired):
        return False
    def reap():
        if os.name != "nt":
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass

    reap()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.1)
        reap()
    if pid_alive(pid) and owned_process(pid, marker):
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    capture_output=True,
                    timeout=8,
                    check=False,
                )
            elif hasattr(signal, "SIGKILL"):
                os.kill(pid, signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired):
            return False
    deadline = time.monotonic() + 5
    while pid_alive(pid) and time.monotonic() < deadline:
        reap()
        time.sleep(0.1)
    reap()
    return not pid_alive(pid)


class InstanceLock:
    """Exclusive lock for one workspace knowledge instance."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        if os.name == "nt":
            import msvcrt

            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class LocalInstance:
    def __init__(self, state_root: Path):
        self.state_root = Path(state_root)
        self.pid_path = self.state_root / "pid.json"
        self.lock_path = self.state_root / "instance.lock"
        self.config_path = self.state_root / "ov.conf"
        self.credentials_path = self.state_root / "credentials.json"
        self.log_dir = self.state_root / "logs"
        self.data_dir = self.state_root / "ov-data"
        cache_env = os.environ.get("VAWS_KNOWLEDGE_EMBEDDING_CACHE")
        self.cache_dir = Path(cache_env) if cache_env else self.state_root / "embedding-cache"
        self.metrics_path = self.state_root / "embedding.jsonl"

    @property
    def openviking_marker(self) -> str:
        return str(self.config_path)

    @property
    def embedding_marker(self) -> str:
        return str(self.metrics_path)

    def describe(self) -> dict[str, Any]:
        record = self._read_pid() or {}
        ov_port = record.get("openviking_port")
        embed_port = record.get("embedding_port")
        ov_pid = record.get("openviking_pid")
        embed_pid = record.get("embedding_pid")
        ov_owned = isinstance(ov_pid, int) and owned_process(ov_pid, self.openviking_marker)
        embed_owned = isinstance(embed_pid, int) and owned_process(
            embed_pid, self.embedding_marker
        )
        ov_ok = bool(ov_port) and ov_owned and _health(f"http://{LOOPBACK}:{ov_port}/health")
        embed_ok = bool(embed_port) and embed_owned and _health(
            f"http://{LOOPBACK}:{embed_port}/health"
        )
        return {
            "state_root": str(self.state_root),
            "live": ov_ok and embed_ok,
            "openviking_ok": ov_ok,
            "embedding_ok": embed_ok,
            "openviking_owned": ov_owned,
            "embedding_owned": embed_owned,
            "openviking_url": f"http://{LOOPBACK}:{ov_port}" if ov_port else None,
            "embedding_url": f"http://{LOOPBACK}:{embed_port}/v1" if embed_port else None,
            "pid": record,
            "loopback": LOOPBACK,
            "openviking_version": OPENVIKING_VERSION,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_dimension": EMBEDDING_DIMENSION,
        }

    def ensure(self) -> dict[str, Any]:
        with InstanceLock(self.lock_path):
            current = self.describe()
            credentials = self._credentials()
            if current["live"] and credentials.get("data_key"):
                return current
            self._stop_owned(current.get("pid") or {})
            self.state_root.mkdir(parents=True, exist_ok=True)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            embed_port = _unused_port()
            ov_port = _unused_port()
            credentials.setdefault("root_key", secrets.token_urlsafe(32))
            self._save_credentials(credentials)
            config = {
                "storage": {
                    "workspace": str(self.data_dir),
                    "vectordb": {"name": "context", "backend": "local"},
                    "agfs": {"backend": "local"},
                },
                "embedding": {
                    "dense": {
                        "api_base": f"http://{LOOPBACK}:{embed_port}/v1",
                        "api_key": "local-loopback",
                        "provider": "openai",
                        "dimension": EMBEDDING_DIMENSION,
                        "model": EMBEDDING_MODEL,
                    }
                },
                "vlm": {
                    "api_base": f"http://{LOOPBACK}:{embed_port}/v1",
                    "api_key": "generation-disabled",
                    "provider": "openai",
                    "model": "generation-disabled",
                },
                "server": {
                    "host": LOOPBACK,
                    "port": ov_port,
                    "auth_mode": "api_key",
                    "root_api_key": credentials["root_key"],
                },
            }
            self.config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            self.config_path.chmod(0o600)
            env = loopback_env()
            env["VAWS_KNOWLEDGE_STATE"] = str(self.state_root)
            embed_cmd = [
                sys.executable,
                "-m",
                "vaws_knowledge.local.embedding",
                "--host",
                LOOPBACK,
                "--port",
                str(embed_port),
                "--cache-dir",
                str(self.cache_dir),
                "--metrics",
                str(self.metrics_path),
            ]
            ov_bin = which("openviking-server")
            if ov_bin is None:
                raise RuntimeError(
                    "openviking-server is not on PATH; install vaws-knowledge with "
                    f"OpenViking {OPENVIKING_VERSION}"
                )
            ov_cmd = [ov_bin, "--config", str(self.config_path)]
            with (self.log_dir / "embedding.log").open("ab") as embed_log:
                embed_proc = subprocess.Popen(
                    embed_cmd, env=env, stdout=embed_log, stderr=subprocess.STDOUT, **_popen_kwargs()
                )
            try:
                self._wait_proc_health(
                    embed_proc,
                    f"http://{LOOPBACK}:{embed_port}/health",
                    timeout=90,
                    log_path=self.log_dir / "embedding.log",
                    name="embedding server",
                )
                with (self.log_dir / "openviking.log").open("ab") as ov_log:
                    ov_proc = subprocess.Popen(
                        ov_cmd, env=env, stdout=ov_log, stderr=subprocess.STDOUT, **_popen_kwargs()
                    )
                try:
                    self._wait_proc_health(
                        ov_proc,
                        f"http://{LOOPBACK}:{ov_port}/health",
                        timeout=90,
                        log_path=self.log_dir / "openviking.log",
                        name="openviking-server",
                    )
                    if not credentials.get("data_key"):
                        from vaws_knowledge.distribution.client import provision_tenant_key

                        credentials["data_key"] = provision_tenant_key(
                            f"http://{LOOPBACK}:{ov_port}",
                            root_key=credentials["root_key"],
                        )
                        self._save_credentials(credentials)
                except Exception:
                    stop_owned_pid(ov_proc.pid, self.openviking_marker)
                    raise
            except Exception:
                stop_owned_pid(embed_proc.pid, self.embedding_marker)
                raise
            record = {
                "embedding_pid": embed_proc.pid,
                "openviking_pid": ov_proc.pid,
                "embedding_port": embed_port,
                "openviking_port": ov_port,
                "host": LOOPBACK,
                "started_at": time.time(),
                "openviking_version": OPENVIKING_VERSION,
                "embedding_model": EMBEDDING_MODEL,
                "openviking_marker": self.openviking_marker,
                "embedding_marker": self.embedding_marker,
            }
            self.pid_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            return self.describe()

    def _credentials(self) -> dict[str, str]:
        try:
            data = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_credentials(self, credentials: dict[str, str]) -> None:
        from vaws_knowledge.distribution.manifest import atomic_write_json

        atomic_write_json(self.credentials_path, credentials)
        self.credentials_path.chmod(0o600)

    def data_key(self) -> str:
        """Private client credential; never include it in status or MCP output."""
        key = self._credentials().get("data_key")
        if not key:
            raise RuntimeError("knowledge instance tenant is not initialized")
        return key

    def stop(self) -> None:
        with InstanceLock(self.lock_path):
            self._stop_owned(self._read_pid() or {})

    def _stop_owned(self, record: dict[str, Any]) -> None:
        ov_pid = record.get("openviking_pid")
        embed_pid = record.get("embedding_pid")
        pending = []
        for pid, marker in ((ov_pid, self.openviking_marker), (embed_pid, self.embedding_marker)):
            if isinstance(pid, int):
                stopped = stop_owned_pid(pid, marker)
                if not stopped and owned_process(pid, marker):
                    pending.append(pid)
        if pending:
            raise RuntimeError(f"knowledge processes did not stop: {pending}; ownership record preserved")
        if self.pid_path.exists():
            try:
                self.pid_path.unlink()
            except OSError:
                pass

    def _read_pid(self) -> dict[str, Any] | None:
        if not self.pid_path.is_file():
            return None
        try:
            payload = json.loads(self.pid_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _wait_proc_health(
        self,
        proc: subprocess.Popen[Any],
        url: str,
        *,
        timeout: float,
        log_path: Path,
        name: str,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = _log_tail(log_path)
                raise RuntimeError(
                    f"{name} exited {proc.returncode} before becoming ready on loopback"
                    + (f": {tail}" if tail else "")
                )
            if _health(url):
                return
            time.sleep(0.2)
        tail = _log_tail(log_path)
        raise RuntimeError(
            f"{name} did not become ready on loopback"
            + (f": {tail}" if tail else "")
        )


def _log_tail(path: Path, limit: int = 1200) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:].strip()


def instance_for_config(config: Any) -> LocalInstance:
    root = getattr(config, "state_root", None)
    if root is None:
        candidate = config.mount("candidate") if hasattr(config, "mount") else None
        roots = getattr(candidate, "roots", ()) if candidate is not None else ()
        if roots:
            root = Path(roots[0]).parent / "instance"
        else:
            state_home = os.environ.get("XDG_STATE_HOME")
            base = Path(state_home) if state_home else Path.home() / ".local" / "state"
            root = base / "vaws-knowledge" / "instance"
    return LocalInstance(Path(root))
