"""Start and stop the sandbox csai: a second instance on its own port, sharing
the dev stack's core, bridge, LDAP, Postgres and Redis, with the stub model and
low limits so the limit paths are reachable in seconds."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]          # convert_search_ai/

#: Low limits: a scenario reaches each of them with a handful of requests.
SANDBOX_LIMITS = {
    "CSAI_HEADLESS_REQUESTS_PER_MINUTE": "40",
    "CSAI_HEADLESS_TOKENS_PER_DAY": "5000",
    "CSAI_HEADLESS_CONCURRENT_RUNS": "2",
    "CSAI_HEADLESS_FILES_PER_RUN": "8",
    "CSAI_HEADLESS_WARN_RATIO": "0.8",
    "CSAI_HEADLESS_MAX_INPUT_CHARS": "2000",
    "CSAI_HEADLESS_EXTRACT_WINDOW_CHARS": "4000",
}


def dev_service_env() -> dict:
    """The dev csai's own ``FILEENGINE_*`` settings (service token file, pepper,
    JWT secret, ...). The start script exports them instead of writing them to
    ``.env``, so a csai started from ``.env`` alone is refused by the core. Read
    from the running app's environment; the values are passed on, never shown."""
    raw = b""
    for pid in [os.environ.get("DESKTOP_SIM_CSAI_PID", ""), *_csai_app_pids()]:
        try:
            raw = Path(f"/proc/{pid}/environ").read_bytes() if pid else b""
        except OSError:
            continue
        if raw:
            break
    out = {}
    for entry in raw.split(b"\0"):
        k, sep, v = entry.decode("utf-8", "replace").partition("=")
        if sep and k.startswith("FILEENGINE_"):
            out[k] = v
    return out


def _csai_app_pids() -> list:
    """The dev csai app, found by its command line (its pidfile can be stale)."""
    pids = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or proc.name == str(os.getpid()):
            continue
        try:
            cmd = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"convert_search_ai.app" in cmd:
            pids.append(proc.name)
    return pids


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Sandbox:
    def __init__(self, model_url: str, *, log_dir: Path, name: str = "sandbox",
                 overrides: dict | None = None, redis_port: int | None = None,
                 consent_timeout_ms: int = 12000):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.name = name
        self.log_path = Path(log_dir) / f"csai-{name}.log"
        env = dict(os.environ)
        env.update(dev_service_env())
        env.update({
            "PYTHONPATH": str(ROOT / "src"),
            "CSAI_HTTP_HOST": "127.0.0.1",
            "CSAI_HTTP_PORT": str(self.port),
            "CSAI_CHAT_PROVIDER": "openai-compatible",
            "CSAI_CHAT_BASE_URL": model_url,
            "CSAI_CHAT_MODEL": "desktop-sim-stub",
            "CSAI_CHAT_API_KEY": "stub",
            "CSAI_WEB_SEARCH_ENABLED": "false",
            "CSAI_MCP_ENABLED": "true",
            "CSAI_MCP_CONSENT_TIMEOUT_MS": str(consent_timeout_ms),
            **SANDBOX_LIMITS,
            **(overrides or {}),
        })
        if redis_port is not None:
            env["FILEENGINE_REDIS_PORT"] = str(redis_port)
        self._env = env
        self._proc: subprocess.Popen | None = None

    def start(self, timeout: float = 60) -> "Sandbox":
        log = open(self.log_path, "ab")
        self._proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("sandbox_main.py"))],
            cwd=str(ROOT), env=self._env, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"sandbox csai exited ({self._proc.returncode}); "
                                   f"see {self.log_path}")
            try:
                if httpx.get(f"{self.url}/healthz", timeout=2).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        self.stop()
        raise RuntimeError(f"sandbox csai did not become healthy; see {self.log_path}")

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
