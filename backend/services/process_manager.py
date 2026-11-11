"""Process manager for running and controlling service servers.

Enables starting, stopping, crashing, and inspecting service server processes
both in standalone mode and for robustness testing.
"""
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Dict, Any, Optional

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_BACKEND, "..", ".."))
_SRC = os.path.join(_ROOT, "src")
_LOGS = os.path.join(_ROOT, "logs")

from services import event_bus

_procs: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


def get_managed_servers() -> list[dict]:
    """Return status of all managed server processes."""
    with _lock:
        result = []
        for name, info in list(_procs.items()):
            proc: subprocess.Popen = info["proc"]
            poll = proc.poll()
            is_running = poll is None
            result.append({
                "name": name,
                "service": info["service"],
                "port": info["port"],
                "pid": proc.pid,
                "running": is_running,
                "exit_code": poll,
                "no_advertise": info.get("no_advertise", False),
                "started_at": info.get("started_at"),
            })
        return result


def start_server(
    name: str,
    service: str,
    registry_ip: str = "127.0.0.1",
    port: int = 7000,
    interval: float = 5.0,
    ttl: float = 15.0,
    no_advertise: bool = False,
) -> dict:
    """Start a service server subprocess."""
    os.makedirs(_LOGS, exist_ok=True)
    logfile = os.path.join(_LOGS, f"{name}.log")

    with _lock:
        if name in _procs:
            old_proc = _procs[name]["proc"]
            try:
                old_proc.kill()
                old_proc.wait(timeout=1.0)
            except Exception:
                pass
            _procs.pop(name, None)

        # Ensure any stray process with this name is terminated
        try:
            subprocess.run(["pkill", "-9", "-f", f"--name {name}"], capture_output=True, timeout=1.0)
            time.sleep(0.2)
        except Exception:
            pass

        py = sys.executable or "python3"
        server_script = os.path.join(_SRC, "service_server.py")
        cmd = [
            py, "-u", server_script,
            "--name", name,
            "--service", service,
            "--registry", registry_ip,
            "--port", str(port),
            "--interval", str(interval),
            "--ttl", str(ttl),
            "--log", logfile,
        ]
        if no_advertise:
            cmd.append("--no-advertise")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            )
            _procs[name] = {
                "proc": proc,
                "name": name,
                "service": service,
                "port": port,
                "no_advertise": no_advertise,
                "started_at": time.time(),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    event_bus.emit("SERVER", "SERVER_STARTED", {
        "name": name,
        "service": service,
        "port": port,
        "pid": proc.pid,
        "no_advertise": no_advertise,
        "mode": "unadvertised (rogue)" if no_advertise else "advertising",
    })

    return {
        "ok": True,
        "name": name,
        "service": service,
        "port": port,
        "pid": proc.pid,
        "no_advertise": no_advertise,
    }


def stop_server(name: str, hard: bool = False) -> dict:
    """Stop or crash a service server subprocess."""
    with _lock:
        if name not in _procs:
            return {"ok": False, "error": f"Server '{name}' is not managed by backend"}

        info = _procs[name]
        proc: subprocess.Popen = info["proc"]
        if proc.poll() is not None:
            return {"ok": True, "message": f"Server '{name}' was already stopped"}

        pid = proc.pid
        try:
            if hard:
                # Force kill (SIGKILL) - simulates crash without clean DEREGISTER
                proc.kill()
                event_type = "SERVER_CRASHED"
                reason = "SIGKILL force-crash (simulated crash, adverts stopped)"
            else:
                # Graceful termination (SIGTERM) - triggers DEREGISTER in signal handler
                proc.terminate()
                event_type = "SERVER_STOPPED"
                reason = "SIGTERM graceful termination (sending DEREGISTER)"

            proc.wait(timeout=2.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    event_bus.emit("SERVER", event_type, {
        "name": name,
        "pid": pid,
        "hard": hard,
        "reason": reason,
    })

    return {"ok": True, "name": name, "hard": hard, "message": reason}


def stop_all() -> None:
    """Stop all managed server processes."""
    with _lock:
        names = list(_procs.keys())
    for name in names:
        stop_server(name, hard=False)
