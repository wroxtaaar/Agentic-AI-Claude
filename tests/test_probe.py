import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from vpsagents.probe import list_models, probe_model


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        self._send({"data": [{"id": "good"}, {"id": "notools"}, {"id": "broken"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        m = body["model"]
        if m == "broken":
            return self._send({"error": "boom"}, 500)
        if "tools" in body and m == "good":
            msg = {"tool_calls": [{"id": "1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "hello.txt", "content": "hi"})}}]}
        else:
            msg = {"content": "pong"}
        self._send({"choices": [{"message": msg}]})


class ProbeTests(unittest.TestCase):
    def test_probe(self):
        srv = HTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_port}/v1"
        try:
            self.assertEqual(list_models(base, "k"), ["good", "notools", "broken"])
            g = probe_model(base, "k", "good")
            self.assertTrue(g["chat"] and g["tool_call"] and g["args_valid"])
            n = probe_model(base, "k", "notools")
            self.assertTrue(n["chat"] and not n["tool_call"])
            b = probe_model(base, "k", "broken")
            self.assertFalse(b["chat"])
            self.assertIn("HTTPError", b["error"])
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
