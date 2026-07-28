"""Launch the shipped uvicorn command, then run the HTTP-only deployment smoke test.

The process boundary belongs here, outside the test. `tests/test_smoke.py` receives only a
base URL, so importing the ASGI app or falling back to TestClient is structurally impossible.
The frontend must already be freshly built by the `just smoke` recipe.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from attest.config import settings

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "frontend" / "dist"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(proc: subprocess.Popen[str], base: str, timeout_s: float = 45) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(
                "the deployed Attest/UI did not start; uvicorn exited during startup:\n"
                f"{output[-2000:]}"
            )
        try:
            if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"the deployed Attest/UI did not start on {base} within {timeout_s}s")


def _stop(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def main() -> int:
    try:
        httpx.get(f"{settings.datahub_gms_url}/config", timeout=3).raise_for_status()
    except Exception:
        print(
            f"DataHub GMS is not reachable at {settings.datahub_gms_url}/config. "
            "Bring the stack up first: `just up`.",
            file=sys.stderr,
        )
        return 1

    if not (DIST / "index.html").is_file():
        print(
            "the built frontend is absent; `just smoke` must run the fresh UI build before "
            "launching uvicorn",
            file=sys.stderr,
        )
        return 1

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    app_module = os.environ.get("ATTEST_SMOKE_APP_MODULE", "attest.api.app:app")

    with tempfile.TemporaryDirectory(prefix="attest-smoke-") as tmp:
        state = Path(tmp)
        server_env = {
            **os.environ,
            "ATTEST_STORE_PATH": str(state / "attest.db"),
            "ATTEST_CHECKPOINT_PATH": str(state / "attest-checkpoints.db"),
        }
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                app_module,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=str(REPO),
            env=server_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for_server(proc, base)
            test_env = {**os.environ, "ATTEST_SMOKE_BASE_URL": base}
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/test_smoke.py",
                    "-m",
                    "live",
                    "-q",
                ],
                cwd=str(REPO),
                env=test_env,
            )
            return result.returncode
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            _stop(proc)
            # Windows can release the checkpoint SQLite handles just after process exit.
            # Wait for that release before TemporaryDirectory removes the files.
            time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
