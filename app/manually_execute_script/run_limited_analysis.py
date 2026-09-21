"""Run one Linux analysis job with kernel resource limits and a host watchdog.

Usage: python -m app.manually_execute_script.run_limited_analysis -- COMMAND ...
Requires the current user's systemd manager; fails closed if it is unavailable.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import urllib.request


UNIT = "creator-app-analysis"
# User ceiling: 5 GB (decimal), not 5 GiB. Reserve remains for other services.
MEMORY_BYTES = 5_000_000_000
RESERVE_MIB = 2048


def available_memory_mib() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise RuntimeError("MemAvailable is unavailable; cannot safely start analysis")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument(
        "--memory-mb", type=int, default=5000,
        help="Analysis memory ceiling in decimal MB (128–5000; default: 5000)",
    )
    parser.add_argument("--health-url", default="http://127.0.0.1:8200/api/v1/health")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not 1 <= args.timeout <= 1800:
        parser.error("provide a command and a timeout between 1 and 1800 seconds")
    if not 128 <= args.memory_mb <= MEMORY_BYTES // 1_000_000:
        parser.error("--memory-mb must be between 128 and 5000")
    memory_bytes = args.memory_mb * 1_000_000
    memory_mib = (memory_bytes + 1024**2 - 1) // 1024**2
    runtime = Path(f"/run/user/{os.getuid()}")
    env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime),
               DBUS_SESSION_BUS_ADDRESS=f"unix:path={runtime}/bus")
    control = ["systemctl", "--user"]
    subprocess.run(control + ["show-environment"], env=env, check=True,
                   stdout=subprocess.DEVNULL)
    with (runtime / f"{UNIT}.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("An analysis job is already running")
        available = available_memory_mib()
        if available < memory_mib + RESERVE_MIB:
            raise SystemExit(
                f"Analysis not started: available memory {available} MiB < "
                f"{memory_mib + RESERVE_MIB} MiB"
            )
        if shutil.disk_usage(Path.cwd()).free < 5 * 1024**3:
            raise SystemExit("Analysis not started: less than 5 GiB of free disk")
        # This manager owns only the analysis unit, never the API service.
        properties = {
            "MemoryMax": str(memory_bytes), "MemoryHigh": str(memory_bytes * 4 // 5),
            "MemorySwapMax": "0", "CPUQuota": "1000%", "TasksMax": "512",
            "Nice": "15", "RuntimeMaxSec": str(args.timeout),
            "TimeoutStopSec": "10", "KillMode": "control-group", "OOMPolicy": "kill",
            "LimitCORE": "0",
            "LogRateLimitIntervalSec": "10s", "LogRateLimitBurst": "100",
        }
        run = ["systemd-run", "--user", f"--unit={UNIT}", "--wait", "--collect",
               f"--working-directory={Path.cwd()}"]
        for key, value in properties.items():
            run += ["--property", f"{key}={value}"]
        interrupted = False

        def interrupt(_signum, _frame):
            nonlocal interrupted
            interrupted = True

        signal.signal(signal.SIGTERM, interrupt)
        signal.signal(signal.SIGINT, interrupt)
        failures = 0
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        process = subprocess.Popen(run + ["--"] + command, env=env)
        try:
            while process.poll() is None:
                reason = ""
                if interrupted:
                    reason = "interrupted"
                elif available_memory_mib() < RESERVE_MIB:
                    reason = "host memory reserve reached"
                elif shutil.disk_usage(Path.cwd()).free < 5 * 1024**3:
                    reason = "host disk reserve reached"
                try:
                    with opener.open(args.health_url, timeout=2) as response:
                        healthy = json.load(response).get("status") == "ok"
                    failures = 0 if healthy else failures + 1
                except (OSError, ValueError):
                    failures += 1
                if failures >= 2:
                    reason = reason or "API health check failed twice"
                if reason:
                    print(f"Stopping analysis: {reason}", flush=True)
                    return 125
                time.sleep(2)
            return process.returncode
        finally:
            if process.poll() is None:
                subprocess.run(control + ["stop", UNIT], env=env, check=False,
                               timeout=20, stdout=subprocess.DEVNULL)
                process.wait(timeout=20)


if __name__ == "__main__":
    raise SystemExit(main())
