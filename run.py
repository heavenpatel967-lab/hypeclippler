"""Starts HypeClipper and opens the dashboard in your browser.

If an older HypeClipper is still running on the same port, it is stopped first, so the
browser never keeps talking to the old version."""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from app import VERSION
from app.main import app, settings


def port_busy(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def pids_on_port(port):
    if os.name != "nt":
        return []
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True).stdout
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and re.search(rf":{port}$", parts[1]):
            pids.add(int(parts[4]))
    return [p for p in pids if p != os.getpid()]


def free_port(port):
    if not port_busy(port):
        return True
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
            old = json.load(r).get("version", "?")
        print(f"  An older HypeClipper ({old}) is still running, stopping it...")
    except Exception:  # noqa: BLE001
        print(f"  Port {port} is in use, freeing it...")
    try:  # ask nicely first (stops recordings cleanly)
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", method="POST",
                                                      data=b""), timeout=5)
    except Exception:  # noqa: BLE001
        pass
    for _ in range(15):
        time.sleep(1)
        if not port_busy(port):
            return True
    for pid in pids_on_port(port):  # still there: force it
        print(f"  Force-stopping process {pid}")
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    for _ in range(10):
        time.sleep(1)
        if not port_busy(port):
            return True
    return False


if __name__ == "__main__":
    host, port = settings["host"], settings["port"]
    if not free_port(port):
        print(f"\n  Port {port} is still used by another program. Restart the PC and run start.bat again.\n")
        input("  Press Enter to close")
        sys.exit(1)
    url = f"http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}"
    print(f"\n  HypeClipper {VERSION} running at {url}  (Ctrl+C to stop)\n")
    threading.Timer(2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
