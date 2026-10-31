"""Test-only HTTPS MAAS endpoint; fake credentials, loopback binding, no logging."""

import http.server
import json
import ssl
from pathlib import Path
from typing import override

DIRECTORY = Path("/tmp/maas-bird-test")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/MAAS/api/2.0/machines/":
            self.send_error(404)
            return
        payload = (DIRECTORY / "machines.json").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        counts = DIRECTORY / "requests.json"
        previous = json.loads(counts.read_text()) if counts.exists() else 0
        counts.write_text(json.dumps(previous + 1))

    @override
    def log_message(self, format, *args):
        pass  # Never record OAuth headers, even with this test-only key.


if __name__ == "__main__":
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(DIRECTORY / "ca.crt", DIRECTORY / "key.pem")
    server = http.server.HTTPServer(("127.0.0.1", 8443), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
