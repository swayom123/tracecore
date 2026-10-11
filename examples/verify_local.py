"""Start the local API, send a separately executed demo, and verify its stored tree."""

import os
import subprocess
import sys
import time

import httpx


def main() -> None:
    base_url = "http://127.0.0.1:8123"
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tracecore.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8123",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            if server.poll() is not None:
                raise RuntimeError("TraceCore API exited before becoming healthy")
            try:
                if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise RuntimeError("TraceCore API did not become healthy within 10 seconds")

        environment = dict(os.environ)
        environment["TRACECORE_DEMO_API_URL"] = base_url
        subprocess.run([sys.executable, "examples/local_demo.py"], check=True, env=environment)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


if __name__ == "__main__":
    main()
