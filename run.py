"""Starts HypeClipper and opens the dashboard in your browser."""
import threading
import webbrowser

import uvicorn

from app.main import app, settings

if __name__ == "__main__":
    host, port = settings["host"], settings["port"]
    url = f"http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}"
    print(f"\n  HypeClipper running at {url}  (Ctrl+C to stop)\n")
    threading.Timer(2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
