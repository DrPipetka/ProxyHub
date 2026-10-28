"""Reproducible local benchmark; no proxy network traffic is generated."""
import argparse
import json
import tempfile
import threading
import time
from pathlib import Path

import psutil
from fastapi.testclient import TestClient

from proxyhub.main import create_app
from proxyhub.parsers import import_configs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=100000)
    args = parser.parse_args()
    root = Path(".tmp").resolve()
    root.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="benchmark-", dir=root))
    peak, stop = [0], threading.Event()
    def sample():
        while not stop.wait(0.02):
            peak[0] = max(peak[0], psutil.Process().memory_info().rss)
    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    try:
        with TestClient(create_app(directory, background=False), base_url='http://127.0.0.1') as client:
            data = "\n".join(f"vless://11111111-1111-4111-8111-111111111111@server-{i}.example:443?security=tls#node-{i}" for i in range(args.count))
            start = time.perf_counter()
            imported = import_configs(client.app.state.service.db, data, "benchmark")
            import_seconds = time.perf_counter() - start
            client.post('/auth/login', json={})
            timings = {}
            for name, query in [("first_page", "/proxies"), ("last_page", f"/proxies?page={max(1,args.count//50)}"), ("filtered", "/proxies?state=new&source=benchmark"), ("search", "/proxies?q=server-999")]:
                start = time.perf_counter()
                response = client.get(query)
                assert response.status_code == 200
                timings[name + "_ms"] = round((time.perf_counter() - start) * 1000, 2)
            report = {"count": args.count, "accepted": imported["accepted"], "import_seconds": round(import_seconds, 3), **timings,
                      "peak_process_rss_mb": round(peak[0] / 1024**2, 2), "note": "Local storage/API benchmark; no 50-proxy live workload"}
            output = Path(f"docs/benchmark-{args.count}.json")
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps(report, indent=2), encoding="utf-8")
            if args.count == 100000:
                Path("docs/benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(report, indent=2))
    finally:
        stop.set()
        thread.join()


if __name__ == "__main__":
    main()
