"""OBS WebSocket istemcisini, standart kütüphaneyle yazılmış küçük sahte bir OBS sunucusuna karşı test eder."""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import obs_stream_doctor as osd  # noqa: E402

PW = "gizli123"


def _recv_frame(c):
    def rx(n):
        b = b""
        while len(b) < n:
            ch = c.recv(n - len(b))
            if not ch:
                raise ConnectionError
            b += ch
        return b
    b0, b1 = rx(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", rx(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", rx(8))[0]
    mask = rx(4) if b1 & 0x80 else None
    pl = rx(n)
    if mask:
        pl = bytes(x ^ mask[i & 3] for i, x in enumerate(pl))
    return b0 & 0x0F, pl


def _send(c, obj=None, op=1, raw=None):
    pl = raw if raw is not None else json.dumps(obj).encode()
    hdr = bytes([0x80 | op])
    hdr += bytes([len(pl)]) if len(pl) < 126 else bytes([126]) + struct.pack(">H", len(pl))
    c.sendall(hdr + pl)


def fake_obs(srv):
    while True:
        try:
            c, _ = srv.accept()
        except OSError:
            return
        req = b""
        while b"\r\n\r\n" not in req:
            req += c.recv(4096)
        key = [l.split(":", 1)[1].strip() for l in req.decode().split("\r\n") if l.lower().startswith("sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   "Sec-WebSocket-Accept: %s\r\nSec-WebSocket-Protocol: obswebsocket.json\r\n\r\n" % acc).encode())
        salt, ch = "c2FsdA==", "Y2hhbA=="
        _send(c, {"op": 0, "d": {"rpcVersion": 1, "authentication": {"salt": salt, "challenge": ch}}})
        _, pl = _recv_frame(c)
        auth = json.loads(pl)["d"].get("authentication")
        secret = base64.b64encode(hashlib.sha256((PW + salt).encode()).digest())
        if auth != base64.b64encode(hashlib.sha256(secret + ch.encode()).digest()).decode():
            _send(c, op=8, raw=struct.pack(">H", 4009) + b"Authentication failed.")
            c.close()
            continue
        _send(c, {"op": 2, "d": {"negotiatedRpcVersion": 1}})
        _send(c, {"op": 5, "d": {"eventType": "X"}})
        _send(c, op=9, raw=b"ping")
        try:
            while True:
                op, pl = _recv_frame(c)
                if op != 1:
                    continue
                d = json.loads(pl)["d"]
                data = {"outputActive": True, "outputBytes": 12345, "outputSkippedFrames": 3, "outputTotalFrames": 600,
                        "padding": "x" * 300}
                _send(c, {"op": 7, "d": {"requestType": d["requestType"], "requestId": d["requestId"],
                                         "requestStatus": {"result": True, "code": 100}, "responseData": data}})
        except Exception:
            c.close()


class ObsClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = socket.socket()
        cls.srv.bind(("127.0.0.1", 0))
        cls.srv.listen(4)
        cls.port = cls.srv.getsockname()[1]
        threading.Thread(target=fake_obs, args=(cls.srv,), daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()

    def test_auth_and_request(self):
        c = osd.ObsClient("127.0.0.1", self.port, PW)
        c.connect()
        r = c.request("GetStreamStatus")
        self.assertEqual(r["outputSkippedFrames"], 3)
        self.assertEqual(c.request("GetStats")["outputTotalFrames"], 600)
        c.close()

    def test_wrong_password(self):
        c = osd.ObsClient("127.0.0.1", self.port, "yanlis")
        with self.assertRaises(osd.ObsAuthError):
            c.connect()
        c.close()


class PingTest(unittest.TestCase):
    def test_tcp_ping_closed_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertIsNone(osd.tcp_ping("127.0.0.1", port, 1.0))

    @unittest.skipUnless(os.name == "nt", "Windows ICMP API")
    def test_windows_icmp(self):
        self.assertIsNotNone(osd._win_icmp, "Windows ICMP API yüklenemedi")
        self.assertIsNotNone(osd.icmp_ping("127.0.0.1", 1.0))


if __name__ == "__main__":
    unittest.main()
