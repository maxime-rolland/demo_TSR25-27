#!/usr/bin/env python3
"""Relay GLPI's outbound webhooks to Hermes, re-signed in Hermes's
Generic V2 HMAC scheme.

GLPI signs its webhooks with X-GLPI-signature/X-GLPI-timestamp, a format
Hermes's webhook adapter does not recognize (it only matches GitHub,
GitLab, Svix, or its own X-Webhook-Signature[-V2] header names). This
relay sits between the two: it trusts the request because it is only
reachable from the `glpi` container on the private compose network (no
port is published to the host), and re-signs the untouched body so
Hermes accepts it.
"""

import hashlib
import hmac
import http.server
import os
import sys
import time
import urllib.error
import urllib.request

LISTEN_PORT = int(os.environ.get("RELAY_LISTEN_PORT", "8080"))
TARGET_URL = os.environ.get("RELAY_TARGET_URL", "")
SECRET = os.environ.get("RELAY_SECRET", "").encode()


def compute_v2_signature(secret: bytes, timestamp: str, body: bytes) -> str:
    """Hermes Generic V2 signature: hex HMAC-SHA256 of "<timestamp>.<body>"."""
    signed_content = timestamp.encode() + b"." + body
    return hmac.new(secret, signed_content, hashlib.sha256).hexdigest()


class RelayHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)

        timestamp = str(int(time.time()))
        signature = compute_v2_signature(SECRET, timestamp, body)

        req = urllib.request.Request(
            TARGET_URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature-V2": signature,
                "X-Webhook-Timestamp": timestamp,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except urllib.error.URLError as exc:
            print(f"[relay] forward failed: {exc}", file=sys.stderr)
            self.send_response(502)
            self.end_headers()
            return

        self.send_response(status)
        self.end_headers()

    def log_message(self, fmt, *args):
        print(f"[relay] {self.address_string()} - {fmt % args}", file=sys.stderr)


def run_server():
    if not TARGET_URL or not SECRET:
        raise SystemExit("RELAY_TARGET_URL and RELAY_SECRET must be set")
    server = http.server.ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), RelayHandler)
    print(f"[relay] listening on 0.0.0.0:{LISTEN_PORT}, forwarding to {TARGET_URL}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
