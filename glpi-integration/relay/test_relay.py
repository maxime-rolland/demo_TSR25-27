import hashlib
import hmac
import json
import threading
import unittest
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

import relay


class ComputeV2SignatureTests(unittest.TestCase):
    def test_known_vector(self):
        secret = b"test-secret"
        timestamp = "1700000000"
        body = b'{"hello":"world"}'
        expected = hmac.new(
            secret, timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        self.assertEqual(relay.compute_v2_signature(secret, timestamp, body), expected)

    def test_different_bodies_produce_different_signatures(self):
        secret = b"test-secret"
        timestamp = "1700000000"
        sig_a = relay.compute_v2_signature(secret, timestamp, b"a")
        sig_b = relay.compute_v2_signature(secret, timestamp, b"b")
        self.assertNotEqual(sig_a, sig_b)


class _CapturingTargetHandler(BaseHTTPRequestHandler):
    """Fake 'Hermes' endpoint that records the last request it received."""

    captured = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        _CapturingTargetHandler.captured = {
            "body": body,
            "headers": dict(self.headers.items()),
        }
        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


class RelayEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.target_server = HTTPServer(("127.0.0.1", 0), _CapturingTargetHandler)
        self.target_port = self.target_server.server_port
        threading.Thread(target=self.target_server.serve_forever, daemon=True).start()

        relay.TARGET_URL = f"http://127.0.0.1:{self.target_port}/webhooks/glpi-ticket"
        relay.SECRET = b"shared-secret"

        self.relay_server = HTTPServer(("127.0.0.1", 0), relay.RelayHandler)
        self.relay_port = self.relay_server.server_port
        threading.Thread(target=self.relay_server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.relay_server.shutdown()
        self.relay_server.server_close()
        self.target_server.shutdown()
        self.target_server.server_close()

    def test_forwards_with_correct_signature(self):
        payload = json.dumps({"item": {"id": 1}, "event": "new"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.relay_port}/relay",
            data=payload,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)

        captured = _CapturingTargetHandler.captured
        self.assertEqual(captured["body"], payload)
        timestamp = captured["headers"]["X-Webhook-Timestamp"]
        expected_sig = relay.compute_v2_signature(b"shared-secret", timestamp, payload)
        self.assertEqual(captured["headers"]["X-Webhook-Signature-V2"], expected_sig)


if __name__ == "__main__":
    unittest.main()
