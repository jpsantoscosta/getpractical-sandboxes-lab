"""Loopback-only process used to distinguish disk state from process memory."""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import uuid4


def main() -> None:
    process_nonce = uuid4().hex
    counter = 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            nonlocal counter
            if self.path != "/state":
                self.send_error(404)
                return
            counter += 1
            payload = json.dumps({
                "process_nonce": process_nonce,
                "counter": counter,
                "pid": os.getpid(),
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "disk_token": Path("/tmp/aca-lab/disk-token.txt").read_text().strip(),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    HTTPServer(("127.0.0.1", 8765), Handler).serve_forever()


if __name__ == "__main__":
    main()
