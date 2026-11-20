#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OBS Stream Doctor
=================
OBS ile Kick'e yayın yaparken oluşan donma / kare düşmelerinin SEBEBİNİ bulmak için.

Her saniye aynı anda ölçer:
  * OBS (WebSocket): ağ yüzünden düşen kareler, bitrate, tıkanıklık, encoder/render gecikmesi
  * Ping: Modem (ağ geçidi), Operatörün ilk sunucusu, İnternet (1.1.1.1 / 8.8.8.8),
          Kick yayın sunucusu (Amazon IVS) ICMP + TCP 443 bağlantı süresi
  * Bu PC'nin toplam upload / download trafiği
  * Wi-Fi sinyal gücü ve erişim noktası (BSSID) değişimleri
  * Kare düştüğü anda Kick sunucusuna traceroute

Kare düştüğünde bu ölçümleri karşılaştırıp sorunun nerede olduğunu söyler:
  ev içi ağ / upload hattı doluyor / operatör hattı / operatör->Kick rotası / PC

Yayın sırasında http://127.0.0.1:8765 adresinde canlı panel açılır.

Gereksinim: Python 3.8+ (isteğe bağlı: pip install psutil -> upload ölçümü)
OBS WebSocket şifresi ve Kick sunucu adresi OBS'in ayarlarından otomatik okunur.

Kullanım:
  python obs_stream_doctor.py
  python obs_stream_doctor.py --obs-password XYZ --ingest rtmps://xxx.global-contribute.live-video.net/app
  python obs_stream_doctor.py --analyze "<rapor klasörü>/olcumler.csv"
Durdurmak için Ctrl+C.

Lisans: MIT
"""

import argparse
import base64
import csv
import hashlib
import ipaddress
import json
import math
import os
import platform
import re
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

VERSION = "1.1.0"
DEFAULT_INGEST = "fa723fc1b171.global-contribute.live-video.net"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
LOSS = -1.0          # CSV'de paket kaybı
ROW_LAG = 3          # ölçümler geç gelebilir: satır 3 sn sonra kesinleşir

try:
    import psutil  # type: ignore
except Exception:
    psutil = None

# ----------------------------------------------------------------------------
# Konsol
# ----------------------------------------------------------------------------
if IS_WIN:
    os.system("")  # ANSI renkleri aç
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

C_RED, C_YEL, C_GRN, C_DIM, C_BOLD, C_RST = "\033[91m", "\033[93m", "\033[92m", "\033[2m", "\033[1m", "\033[0m"


def log(msg=""):
    print(msg, flush=True)


def run_cmd(args, timeout=30):
    kw = {}
    if IS_WIN:
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, **kw)
        out = p.stdout
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
    except Exception as e:
        return "HATA: %s" % e
    for enc in ("utf-8", "cp857", "cp1254", "latin-1"):
        try:
            return out.decode(enc)
        except Exception:
            continue
    return out.decode("utf-8", "replace")


# ----------------------------------------------------------------------------
# Ping (ICMP) ve TCP ölçümü
# ----------------------------------------------------------------------------
_win_icmp = None
if IS_WIN:
    try:
        import ctypes
        from ctypes import wintypes

        class _IPOPT(ctypes.Structure):
            _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                        ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]

        class _ECHOREPLY(ctypes.Structure):
            _fields_ = [("Address", ctypes.c_ulong), ("Status", ctypes.c_ulong),
                        ("RoundTripTime", ctypes.c_ulong), ("DataSize", ctypes.c_ushort),
                        ("Reserved", ctypes.c_ushort), ("Data", ctypes.c_void_p), ("Options", _IPOPT)]

        _iph = ctypes.windll.iphlpapi
        _iph.IcmpCreateFile.restype = wintypes.HANDLE
        _iph.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
        _iph.IcmpSendEcho.argtypes = [wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ushort,
                                      ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
        _iph.IcmpSendEcho.restype = ctypes.c_ulong

        def _win_ping(ip, timeout_s):
            h = _iph.IcmpCreateFile()
            if not h or h == wintypes.HANDLE(-1).value:
                raise OSError("IcmpCreateFile")
            try:
                addr = int.from_bytes(socket.inet_aton(ip), "little")
                data = ctypes.create_string_buffer(b"kickmonitor" * 3)
                size = ctypes.sizeof(_ECHOREPLY) + len(data) + 64
                buf = ctypes.create_string_buffer(size)
                t0 = time.perf_counter()
                n = _iph.IcmpSendEcho(h, addr, data, len(data), None, buf, size, int(timeout_s * 1000))
                dt = (time.perf_counter() - t0) * 1000
                if n == 0:
                    return None
                rep = _ECHOREPLY.from_buffer_copy(buf)
                if rep.Status != 0:
                    return None
                return max(0.1, min(dt, float(rep.RoundTripTime) + 1.0)) if rep.RoundTripTime else max(0.1, min(dt, 1.0))
            finally:
                _iph.IcmpCloseHandle(h)

        _win_ping("127.0.0.1", 1.0)
        _win_icmp = _win_ping
    except Exception:
        _win_icmp = None

_PING_RE = re.compile(r"[=<]\s*([\d]+(?:[.,]\d+)?)\s*ms", re.I)


def icmp_ping(ip, timeout_s=2.0):
    """RTT (ms) ya da None (kayıp)."""
    if _win_icmp:
        try:
            return _win_icmp(ip, timeout_s)
        except Exception:
            pass
    if IS_WIN:
        args = ["ping", "-n", "1", "-w", str(int(timeout_s * 1000)), ip]
    elif IS_MAC:
        args = ["ping", "-n", "-c", "1", "-W", str(int(timeout_s * 1000)), ip]
    else:
        args = ["ping", "-n", "-c", "1", "-W", str(max(1, int(round(timeout_s)))), ip]
    out = run_cmd(args, timeout=timeout_s + 2)
    for line in out.splitlines():
        if "ttl" in line.lower():
            m = _PING_RE.search(line)
            if m:
                return max(0.1, float(m.group(1).replace(",", ".")))
    return None


def tcp_ping(ip, port=443, timeout_s=2.0):
    t0 = time.perf_counter()
    try:
        s = socket.create_connection((ip, port), timeout=timeout_s)
        dt = (time.perf_counter() - t0) * 1000
        s.close()
        return max(0.1, dt)
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Ağ keşfi: ağ geçidi, traceroute, operatör ilk hop
# ----------------------------------------------------------------------------
_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def is_private(ip):
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private and not ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10")
    except Exception:
        return False


def default_gateway():
    try:
        if IS_WIN:
            out = run_cmd(["route", "print", "-4", "0.0.0.0"])
            best = None
            for line in out.splitlines():
                p = line.split()
                if len(p) >= 5 and p[0] == "0.0.0.0" and p[1] == "0.0.0.0" and _IP_RE.match(p[2]):
                    metric = int(p[4]) if p[4].isdigit() else 9999
                    if best is None or metric < best[0]:
                        best = (metric, p[2])
            if best:
                return best[1]
        elif IS_MAC:
            out = run_cmd(["route", "-n", "get", "default"])
            m = re.search(r"gateway:\s*(\S+)", out)
            if m and _IP_RE.match(m.group(1)):
                return m.group(1)
        else:
            out = run_cmd(["ip", "route", "show", "default"])
            m = re.search(r"default via (\S+)", out)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def traceroute(ip, max_hops=12):
    """[(hop, ip veya None, rtt veya None)], ham çıktı"""
    if IS_WIN:
        args = ["tracert", "-d", "-h", str(max_hops), "-w", "800", ip]
    else:
        args = ["traceroute", "-n", "-m", str(max_hops), "-w", "1", "-q", "2", ip]
    raw = run_cmd(args, timeout=90)
    hops = []
    for line in raw.splitlines():
        m = re.match(r"^\s*(\d+)\s+(.*)$", line)
        if not m:
            continue
        rest = m.group(2)
        ips = _IP_RE.findall(rest)
        rtts = [float(x.replace(",", ".")) for x in re.findall(r"([\d]+(?:[.,]\d+)?)\s*ms", rest)]
        if "<1" in rest and not rtts:
            rtts = [0.5]
        hops.append((int(m.group(1)), ips[-1] if ips else None, min(rtts) if rtts else None))
    return hops, raw


def resolve(host):
    try:
        return socket.gethostbyname(host)
    except Exception:
        return None


# ----------------------------------------------------------------------------
# OBS WebSocket v5 istemcisi (ek kütüphane gerektirmez)
# ----------------------------------------------------------------------------
class ObsAuthError(Exception):
    pass


class ObsClient:
    def __init__(self, host, port, password):
        self.host, self.port, self.password = host, port, password or ""
        self.sock = None
        self.buf = b""
        self.lock = threading.Lock()

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("OBS bağlantısı kapandı")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, payload, opcode=1):
        hdr = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self.sock.sendall(bytes(hdr) + mask + masked)

    def _recv_msg(self):
        data = b""
        while True:
            b0, b1 = self._recv_exact(2)
            fin, op = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b1 & 0x80 else None
            pl = self._recv_exact(n)
            if mask:
                pl = bytes(b ^ mask[i & 3] for i, b in enumerate(pl))
            if op == 8:
                code = struct.unpack(">H", pl[:2])[0] if len(pl) >= 2 else 0
                reason = pl[2:].decode("utf-8", "replace")
                if code == 4009:
                    raise ObsAuthError("OBS WebSocket şifresi yanlış")
                raise ConnectionError("OBS bağlantıyı kapattı (%s %s)" % (code, reason))
            if op == 9:
                self._send_frame(pl, 10)
                continue
            if op == 10:
                continue
            data += pl
            if fin:
                return json.loads(data.decode("utf-8"))

    def connect(self):
        self.close()
        self.buf = b""
        s = socket.create_connection((self.host, self.port), timeout=3)
        s.settimeout(5)
        self.sock = s
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET / HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n"
               "Sec-WebSocket-Protocol: obswebsocket.json\r\n\r\n") % (self.host, self.port, key)
        s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket el sıkışması başarısız")
            resp += chunk
        head, self.buf = resp.split(b"\r\n\r\n", 1)
        if b" 101" not in head.split(b"\r\n")[0]:
            raise ConnectionError("WebSocket el sıkışması reddedildi: %r" % head[:80])
        hello = self._recv_msg()
        if hello.get("op") != 0:
            raise ConnectionError("Beklenmeyen OBS mesajı")
        d = hello.get("d", {})
        ident = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = d.get("authentication")
        if auth:
            if not self.password:
                raise ObsAuthError("OBS WebSocket şifre istiyor (--obs-password)")
            secret = base64.b64encode(hashlib.sha256((self.password + auth["salt"]).encode()).digest())
            ident["authentication"] = base64.b64encode(
                hashlib.sha256(secret + auth["challenge"].encode()).digest()).decode()
        self._send_frame(json.dumps({"op": 1, "d": ident}).encode())
        msg = self._recv_msg()
        if msg.get("op") != 2:
            raise ConnectionError("OBS kimlik doğrulama başarısız")

    def request(self, rtype, data=None):
        with self.lock:
            rid = uuid.uuid4().hex
            d = {"requestType": rtype, "requestId": rid}
            if data:
                d["requestData"] = data
            self._send_frame(json.dumps({"op": 6, "d": d}).encode())
            deadline = time.time() + 5
            while time.time() < deadline:
                msg = self._recv_msg()
                if msg.get("op") == 7 and msg["d"].get("requestId") == rid:
                    st = msg["d"].get("requestStatus", {})
                    if not st.get("result"):
                        raise RuntimeError("OBS isteği başarısız: %s" % st)
                    return msg["d"].get("responseData", {}) or {}
            raise TimeoutError("OBS yanıt vermedi")


# ----------------------------------------------------------------------------
# Wi-Fi ve trafik sayaçları
# ----------------------------------------------------------------------------
def wifi_info():
    """{'signal': %, 'bssid': str, 'ssid': str} ya da None"""
    try:
        if IS_WIN:
            out = run_cmd(["netsh", "wlan", "show", "interfaces"], timeout=5)
            info = {}
            for line in out.splitlines():
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k, v = k.strip().lower(), v.strip()
                if "bssid" in k:
                    info["bssid"] = v.lower()
                elif k.startswith("ssid"):
                    info["ssid"] = v
                elif ("signal" in k or "sinyal" in k) and "%" in v:
                    m = re.search(r"(\d+)\s*%", v)
                    if m:
                        info["signal"] = int(m.group(1))
                elif k in ("channel", "kanal"):
                    info["channel"] = v
            return info if "signal" in info else None
        if IS_MAC:
            out = run_cmd(["system_profiler", "SPAirPortDataType"], timeout=15)
            m = re.search(r"Current Network Information:\s*\n\s*(.+?):\s*\n(.*?)(?:\n\s*Other Local|\Z)", out, re.S)
            if not m:
                return None
            block = m.group(2)
            sn = re.search(r"Signal / Noise:\s*(-?\d+)\s*dBm", block)
            ch = re.search(r"Channel:\s*(.+)", block)
            if not sn:
                return None
            dbm = int(sn.group(1))
            pct = max(0, min(100, 2 * (dbm + 100)))  # -50 dBm=100%, -100 dBm=0%
            return {"signal": pct, "ssid": m.group(1).strip(), "bssid": "", "channel": ch.group(1).strip() if ch else ""}
    except Exception:
        return None
    return None


def net_bytes():
    """(gönderilen, alınan) toplam byte, loopback hariç"""
    if psutil:
        try:
            s = r = 0
            for name, c in psutil.net_io_counters(pernic=True).items():
                ln = name.lower()
                if ln.startswith("lo") or "loopback" in ln:
                    continue
                s += c.bytes_sent
                r += c.bytes_recv
            return s, r
        except Exception:
            pass
    if IS_WIN:
        out = run_cmd(["netstat", "-e"], timeout=5)
        for line in out.splitlines():
            nums = re.findall(r"\d+", line)
            if len(nums) == 2 and re.match(r"^\s*\D", line):
                return int(nums[1]), int(nums[0])
    elif not IS_MAC and os.path.exists("/proc/net/dev"):
        s = r = 0
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                name, data = line.split(":", 1)
                if name.strip() == "lo":
                    continue
                v = data.split()
                r += int(v[0])
                s += int(v[8])
        return s, r
    return None


# ----------------------------------------------------------------------------
# Kayıt alanları
# ----------------------------------------------------------------------------
TARGETS = [  # anahtar, görünen ad
    ("gw", "Modem"),
    ("isp", "Operatör ilk hop"),
    ("net", "İnternet (1.1.1.1/8.8.8.8)"),
    ("kick_tcp", "Kick sunucusu (TCP)"),
    ("kick_icmp", "Kick sunucusu (ping)"),
]
FIELDS = (["ts", "saat"] + ["%s_ms" % k for k, _ in TARGETS] +
          ["obs_aktif", "obs_yeniden_baglaniyor", "obs_tikaniklik", "obs_kbps", "obs_ag_dusen_kare",
           "obs_toplam_kare", "obs_encoder_atlanan", "obs_render_atlanan", "obs_cpu", "obs_fps",
           "obs_render_ms", "pc_upload_kbps", "pc_download_kbps", "wifi_sinyal", "wifi_bssid"])


# ----------------------------------------------------------------------------
# Analiz / teşhis
# ----------------------------------------------------------------------------
CATS = {
    "LOCAL": {
        "ad": "Ev içi ağ (modem / Wi-Fi / kablo)",
        "neden": "Kare düştüğü anda bilgisayar ile modem arasındaki ping'de bile kayıp veya ani gecikme var. "
                 "Sorun daha internete çıkmadan, evin içinde başlıyor. Farklı PC'lerle denediğinizde de aynı modem "
                 "ve aynı ağ kullanıldığı için bu ihtimal hâlâ geçerli.",
        "oneri": ["Wi-Fi kullanıyorsanız kabloyla bağlanıp tekrar ölçün (en kesin test).",
                  "Ethernet kablosunu ve modemdeki portu değiştirin; kablo Cat5e/Cat6 olsun.",
                  "Modemi yeniden başlatın, ısınıyor mu kontrol edin; arada switch/ikinci modem varsa çıkarıp deneyin.",
                  "Wi-Fi şartsa 5 GHz kullanın, modeme yaklaşın, kanal değiştirin."],
    },
    "UPLOAD": {
        "ad": "Upload hattı doluyor (bufferbloat)",
        "neden": "Modeme ping normal, ama dışarıdaki tüm hedeflerde (operatör, internet, Kick) gecikme aynı anda "
                 "fırlıyor. Bu, upload hattının dolup modemde kuyruk oluştuğunu gösterir: ya yayın bitrate'i hattın "
                 "gerçek upload kapasitesine çok yakın ya da evdeki başka cihazlar (oyun konsolu, telefon yedeklemesi, "
                 "torrent, bulut senkronizasyonu) aynı anda upload yapıyor.",
        "oneri": ["OBS bitrate'ini düşürün (ölçülen upload hızınızın en fazla %60-70'i).",
                  "Yayın sırasında evdeki diğer cihazların upload yapmadığından emin olun.",
                  "Modemde QoS / SQM (Smart Queue) varsa açın ve yayın PC'sine öncelik verin.",
                  "OBS > Ayarlar > Gelişmiş > Ağ: 'Dinamik olarak bit hızını değiştir' seçeneğini açın."],
    },
    "ISP": {
        "ad": "Operatör hattında paket kaybı",
        "neden": "Modeme ping temiz, ama operatörün ilk sunucusundan itibaren her yerde paket kaybı var. Hat 'kopmuyor' "
                 "olabilir; yine de kısa süreli paket kayıpları yayını dondurmaya yeter. Bu, operatörün hattı/santrali "
                 "tarafında bir sorun.",
        "oneri": ["Bu raporu ve traceroute çıktısını operatöre gönderin: 'bağlantı kopmuyor ama X saatlerinde paket kaybı var'.",
                  "Modemin hat değerlerini (SNR, CRC/FEC hata sayıları) kontrol ettirin.",
                  "Sorun hep aynı saatlerde oluyorsa (ör. akşam 20-24) bunu özellikle belirtin."],
    },
    "KICK_ROUTE": {
        "ad": "Operatör ile Kick (Amazon IVS) arasındaki rota",
        "neden": "Modem, operatör ve genel internet (1.1.1.1 / 8.8.8.8) temiz, ama yalnızca Kick'in yayın sunucusuna "
                 "giden yolda kayıp/gecikme var. İnternetiniz sağlam; sorun operatörünüzün Amazon/Kick'e giden "
                 "rotasında (peering). Operatör 'hattınızda sorun yok' derken haklı çıkabilir, çünkü sorun hatta değil, "
                 "dışarıya giden yolda.",
        "oneri": ["Bir VPN ile (farklı bir rota üzerinden) yayın açıp test edin; VPN ile düzeliyorsa teşhis kesinleşir.",
                  "Operatöre traceroute çıktısıyla 'Amazon IVS (live-video.net) yönünde paket kaybı' şikayeti açın.",
                  "Kick destek ekibine de aynı bilgiyi iletin."],
    },
    "OTHER_UPLOAD": {
        "ad": "Bu PC'de başka upload trafiği",
        "neden": "Kare düşmeleri sırasında bu bilgisayar yayından ayrı olarak kayda değer upload yapıyor "
                 "(ör. OneDrive / Google Drive senkronizasyonu, Steam, Discord ekran paylaşımı, torrent).",
        "oneri": ["Görev Yöneticisi > Performans > Kaynak İzleyicisi > Ağ sekmesinden upload yapan programı bulun.",
                  "Yayın sırasında bulut senkronizasyonunu ve indirme/yükleme programlarını durdurun."],
    },
    "PC": {
        "ad": "Bilgisayar (encoder / render gecikmesi)",
        "neden": "Ağ ölçümleri temiz; OBS'in kendisi encoder veya render gecikmesi yüzünden kare atlıyor.",
        "oneri": ["OBS'te encoder önayarını hafifletin veya donanım encoder (NVENC/AMF/QuickSync) kullanın.",
                  "Oyun FPS'ini sınırlayın, OBS'i yönetici olarak çalıştırın."],
    },
    "UNKNOWN": {
        "ad": "Ağ ölçümlerinde iz yok",
        "neden": "OBS ağ yüzünden kare düşürdü ama ping ölçümlerinin hiçbirinde aynı anda bozulma görülmedi. "
                 "Bu genelde yalnızca yayın bağlantısının (TCP) yavaşladığı durumlarda olur: Kick sunucusu tarafı, "
                 "operatörün yayın trafiğini kısması ya da bitrate'in hattın sürdürebileceğinden yüksek olması.",
        "oneri": ["Bitrate'i 1500-2000 kbps düşürüp aynı saatte tekrar ölçün.",
                  "VPN ile deneyin; düzelirse sorun operatör->Kick yolundadır.",
                  "speedtest ile upload hızınızı yayın sırasında değil, yayından hemen önce ölçün."],
    },
}


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def _median(xs):
    return statistics.median(xs) if xs else None


def _pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo = int(math.floor(k))
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


class Analyzer:
    def __init__(self, rows):
        self.rows = rows
        self.n = len(rows)
        self.base = {}
        self.thr = {}
        for k, _ in TARGETS:
            vals = [_f(r.get("%s_ms" % k)) for r in rows]
            ok = [v for v in vals if v is not None and v >= 0]
            med = _median(ok)
            self.base[k] = med
            if med is not None:
                p25, p75 = _pct(ok, .25), _pct(ok, .75)
                iqr = (p75 - p25) if p25 is not None else 0
                self.thr[k] = med + max(30.0, 2.0 * med, 4 * iqr)
        kb = [_f(r.get("obs_kbps")) for r in rows if _f(r.get("obs_aktif"))]
        kb = [v for v in kb if v and v > 0]
        self.kbps_med = _median(kb)

    def state(self, r, k):
        """'ok' | 'loss' | 'spike' | None (ölçüm yok)"""
        v = _f(r.get("%s_ms" % k))
        if v is None:
            return None
        if v < 0:
            return "loss"
        t = self.thr.get(k)
        if t is not None and v > t:
            return "spike"
        return "ok"

    def is_event(self, r):
        if not _f(r.get("obs_aktif")):
            return False
        if (_f(r.get("obs_ag_dusen_kare")) or 0) > 0:
            return True
        if _f(r.get("obs_yeniden_baglaniyor")):
            return True
        kb = _f(r.get("obs_kbps"))
        if self.kbps_med and kb is not None and kb < 0.35 * self.kbps_med:
            return True
        return False

    def incidents(self):
        inc = []
        cur = None
        for i, r in enumerate(self.rows):
            if self.is_event(r):
                if cur and i - cur[1] <= 3:
                    cur[1] = i
                else:
                    cur = [i, i]
                    inc.append(cur)
        return [tuple(x) for x in inc]

    def target_noise(self, incs):
        """Olay dışı zamanlarda her hedefin 'bozuk' oranı (ICMP rate-limit vb. gürültüyü ayıklamak için)"""
        mask = set()
        for a, b in incs:
            mask.update(range(max(0, a - 8), min(self.n, b + 4)))
        noise = {}
        for k, _ in TARGETS:
            bad = tot = 0
            for i, r in enumerate(self.rows):
                if i in mask:
                    continue
                s = self.state(r, k)
                if s is None:
                    continue
                tot += 1
                bad += s != "ok"
            noise[k] = (bad / tot) if tot else None
        return noise

    def classify(self, a, b, noise):
        rows = self.rows[max(0, a - 6): min(self.n, b + 3)]
        ev = {}
        for k, _ in TARGETS:
            st = [self.state(r, k) for r in rows]
            st = [s for s in st if s is not None]
            loss, spike = st.count("loss"), st.count("spike")
            nz = noise.get(k)
            reliable = st and (nz is None or nz < 0.5)
            rate = (loss + spike) / len(st) if st else 0
            bad = bool(reliable and (loss + spike) >= 2 and rate >= max(0.15, 3 * (nz or 0)))
            if k == "gw" and reliable and loss >= 1 and (nz or 0) < 0.05:
                bad = True
            vals = [_f(r.get("%s_ms" % k)) for r in rows]
            vals = [v for v in vals if v is not None and v >= 0]
            ev[k] = {"bad": bad, "loss": loss, "spike": spike, "n": len(st), "reliable": bool(reliable),
                     "max": max(vals) if vals else None, "base": self.base.get(k)}
        dropped = sum((_f(r.get("obs_ag_dusen_kare")) or 0) for r in self.rows[a:b + 1])
        enc = sum((_f(r.get("obs_encoder_atlanan")) or 0) + (_f(r.get("obs_render_atlanan")) or 0)
                  for r in self.rows[a:b + 1])
        reconn = any(_f(r.get("obs_yeniden_baglaniyor")) for r in self.rows[a:b + 1])
        sigs = [_f(r.get("wifi_sinyal")) for r in rows]
        sigs = [s for s in sigs if s is not None]
        bss = {r.get("wifi_bssid") for r in rows if r.get("wifi_bssid")}
        wifi_bad = (sigs and min(sigs) < 40) or len(bss) > 1
        other = []
        for r in self.rows[a:b + 1]:
            up, ob = _f(r.get("pc_upload_kbps")), _f(r.get("obs_kbps")) or 0
            if up is not None:
                other.append(max(0.0, up - ob * 1.05))
        other_up = _median(other) if other else None
        other_bad = other_up is not None and other_up > max(1000.0, 0.3 * (self.kbps_med or 3000))

        ext = [k for k in ("isp", "net", "kick_tcp", "kick_icmp") if ev[k]["bad"]]
        ext_spike = sum(ev[k]["spike"] for k in ext)
        ext_loss = sum(ev[k]["loss"] for k in ext)
        kick_bad = ev["kick_tcp"]["bad"] or ev["kick_icmp"]["bad"]
        if ev["gw"]["bad"] or wifi_bad:
            cat = "LOCAL"
        elif ev["net"]["bad"] and (kick_bad or ev["isp"]["bad"]):
            cat = "UPLOAD" if ext_spike >= ext_loss else "ISP"
        elif ev["isp"]["bad"] and kick_bad:
            cat = "UPLOAD" if ext_spike >= ext_loss else "ISP"
        elif kick_bad and not ev["net"]["bad"]:
            cat = "KICK_ROUTE"
        elif other_bad:
            cat = "OTHER_UPLOAD"
        elif enc > 0 and dropped == 0:
            cat = "PC"
        else:
            cat = "UNKNOWN"
        names = dict(TARGETS)
        kanit = []
        for k, _ in TARGETS:
            e = ev[k]
            if e["n"] == 0 or not e["reliable"]:
                continue
            if e["bad"] or e["loss"] or e["spike"]:
                s = "%s: %d kayıp, %d ani gecikme" % (names[k], e["loss"], e["spike"])
                if e["max"] is not None and e["base"] is not None:
                    s += " (normal %.0f ms, en yüksek %.0f ms)" % (e["base"], e["max"])
                kanit.append(s)
        if wifi_bad:
            kanit.append("Wi-Fi: sinyal en düşük %s%%%s" % (int(min(sigs)) if sigs else "?",
                                                          ", erişim noktası değişti" if len(bss) > 1 else ""))
        if other_bad:
            kanit.append("Bu PC'de yayın dışı upload ≈ %.1f Mbps" % (other_up / 1000))
        if enc:
            kanit.append("OBS encoder/render gecikmesiyle %d kare atladı" % enc)
        if not kanit:
            kanit.append("Hiçbir ping hedefinde eşzamanlı bozulma yok")
        return {"a": a, "b": b, "cat": cat, "dropped": int(dropped), "reconn": reconn,
                "start": self.rows[a].get("saat"), "dur": b - a + 1, "kanit": kanit}

    def summary(self):
        incs = self.incidents()
        noise = self.target_noise(incs)
        res = [self.classify(a, b, noise) for a, b in incs]
        tot_drop = sum((_f(r.get("obs_ag_dusen_kare")) or 0) for r in self.rows)
        tot_frames = sum((_f(r.get("obs_toplam_kare")) or 0) for r in self.rows)
        by = defaultdict(lambda: {"n": 0, "dropped": 0})
        for x in res:
            by[x["cat"]]["n"] += 1
            by[x["cat"]]["dropped"] += x["dropped"]
        ranking = sorted(by.items(), key=lambda kv: (kv[1]["dropped"], kv[1]["n"]), reverse=True)
        tstats = {}
        for k, name in TARGETS:
            vals = [_f(r.get("%s_ms" % k)) for r in self.rows]
            vals = [v for v in vals if v is not None]
            ok = [v for v in vals if v >= 0]
            tstats[k] = {"ad": name, "n": len(vals),
                         "kayip_yuzde": (100.0 * (len(vals) - len(ok)) / len(vals)) if vals else None,
                         "medyan": _median(ok), "p95": _pct(ok, .95), "max": max(ok) if ok else None,
                         "gurultu": noise.get(k)}
        return {"incidents": res, "ranking": ranking, "tot_drop": int(tot_drop),
                "tot_frames": int(tot_frames), "tstats": tstats, "kbps_med": self.kbps_med}


# ----------------------------------------------------------------------------
# HTML rapor (dosya) ve canlı panel (yerel web sunucusu)
# ----------------------------------------------------------------------------
def _downsample(rows, maxpts=2400):
    n = len(rows)
    step = max(1, int(math.ceil(n / float(maxpts))))
    out = {"t": [], "kbps": [], "drop": [], "up": []}
    for k, _ in TARGETS:
        out[k] = []
        out[k + "_l"] = []
    for i in range(0, n, step):
        chunk = rows[i:i + step]
        out["t"].append(int(_f(chunk[0].get("ts")) or 0))
        kb = [_f(r.get("obs_kbps")) for r in chunk if _f(r.get("obs_aktif"))]
        kb = [v for v in kb if v is not None]
        out["kbps"].append(round(sum(kb) / len(kb)) if kb else None)
        out["drop"].append(int(sum((_f(r.get("obs_ag_dusen_kare")) or 0) for r in chunk)))
        up = [_f(r.get("pc_upload_kbps")) for r in chunk]
        up = [v for v in up if v is not None]
        out["up"].append(round(sum(up) / len(up)) if up else None)
        for k, _ in TARGETS:
            vals = [_f(r.get("%s_ms" % k)) for r in chunk]
            vals = [v for v in vals if v is not None]
            ok = [v for v in vals if v >= 0]
            out[k].append(round(max(ok), 1) if ok else None)
            out[k + "_l"].append(len(vals) - len(ok))
    return out, step


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _now_tiles(rows, an):
    """Canlı panelin 'Şu an' kutucukları: son 5 saniyenin durumu."""
    if not rows:
        return []
    last = rows[-5:]
    tiles = []
    r = rows[-1]
    if _f(r.get("obs_aktif")):
        kb = _f(r.get("obs_kbps"))
        st = "good"
        if an.kbps_med and kb is not None and kb < 0.6 * an.kbps_med:
            st = "crit"
        tiles.append({"l": "Yayın bitrate", "v": "–" if kb is None else "%d kbps" % kb, "s": st})
    else:
        tiles.append({"l": "Yayın", "v": "Yayında değil" if r.get("obs_aktif") not in (None, "") else "OBS bağlı değil", "s": "none"})
    d60 = int(sum((_f(x.get("obs_ag_dusen_kare")) or 0) for x in rows[-60:]))
    tiles.append({"l": "Düşen kare (son 60 sn)", "v": str(d60), "s": "crit" if d60 else "good"})
    for k, name in TARGETS:
        if k == "kick_icmp":
            continue
        vals = [_f(x.get("%s_ms" % k)) for x in last]
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        loss = sum(1 for v in vals if v < 0)
        ok = [v for v in vals if v >= 0]
        cur = ok[-1] if ok else None
        thr = an.thr.get(k)
        st = "good"
        if loss:
            st = "crit"
        elif cur is not None and thr is not None and cur > thr:
            st = "warn"
        v = ("%.0f ms" % cur) if cur is not None else "yanıt yok"
        if loss:
            v += " · %d kayıp" % loss
        short = {"gw": "Modem", "isp": "Operatör", "net": "İnternet", "kick_tcp": "Kick sunucusu"}.get(k, name)
        tiles.append({"l": short, "v": v, "s": st})
    up = _f(r.get("pc_upload_kbps"))
    if up is not None:
        tiles.append({"l": "Bu PC upload", "v": "%.1f Mbps" % (up / 1000.0), "s": "none"})
    sg = _f(r.get("wifi_sinyal"))
    if sg is not None:
        tiles.append({"l": "Wi-Fi sinyali", "v": "%%%d" % sg, "s": "crit" if sg < 40 else "warn" if sg < 60 else "good"})
    return tiles


def build_report(rows, meta, traces="", window=0, live=False):
    """Raporun tüm dinamik parçaları. window>0 ise grafikler sadece son 'window' saniyeyi gösterir."""
    an = Analyzer(rows)
    s = an.summary()
    view = rows[-window:] if window and len(rows) > window else rows
    data, step = _downsample(view)
    names = dict(TARGETS)
    shown = [k for k, _ in TARGETS if any(v is not None for v in data[k])]
    incs = s["incidents"]
    for x in incs:
        x["t0"] = int(_f(rows[x["a"]].get("ts")) or 0)
        x["t1"] = int(_f(rows[x["b"]].get("ts")) or 0) + 1

    dur = len(rows)
    drop_pct = (100.0 * s["tot_drop"] / s["tot_frames"]) if s["tot_frames"] else 0.0
    if not incs:
        verdict_html = ("<div class='verdict ok'><span class='ico'>✓</span><div><b>%s</b><br>%s</div></div>" % (
            "Şimdiye kadar ağ kaynaklı kare düşmesi yok." if live else "Bu kayıtta ağ kaynaklı kare düşmesi yakalanmadı.",
            "Donma olduğunda burada sebebi görünecek." if live else
            "Sorun oluştuğu bir yayında aracı çalıştırıp tekrar bakın."))
    else:
        top, tv = s["ranking"][0]
        share = 100.0 * tv["dropped"] / max(1, s["tot_drop"]) if s["tot_drop"] else 100.0 * tv["n"] / len(incs)
        c = CATS[top]
        verdict_html = ("<div class='verdict bad'><span class='ico'>!</span><div><div class='vk'>En olası sebep</div>"
                        "<div class='vt'>%s</div><div class='vs'>%d olaydan %d tanesi, düşen karelerin %%%.0f'i</div>"
                        "<p>%s</p><ul>%s</ul></div></div>") % (
            _esc(c["ad"]), len(incs), tv["n"], share, _esc(c["neden"]),
            "".join("<li>%s</li>" % _esc(o) for o in c["oneri"]))

    rank_rows = "".join(
        "<tr><td>%s</td><td class='num'>%d</td><td class='num'>%d</td></tr>" % (_esc(CATS[k]["ad"]), v["n"], v["dropped"])
        for k, v in s["ranking"]) or "<tr><td colspan='3' class='muted'>–</td></tr>"
    stat_rows = ""
    for k, _ in TARGETS:
        t = s["tstats"][k]
        if not t["n"]:
            continue

        def fm(v):
            return "–" if v is None else "%.0f" % v
        note = ""
        if t["gurultu"] is not None and t["gurultu"] >= 0.5:
            note = " <span class='muted'>(ping'e güvenilir yanıt vermiyor, analizde yok sayıldı)</span>"
        stat_rows += ("<tr><td>%s%s</td><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td>"
                      "<td class='num'>%s</td></tr>") % (
            _esc(t["ad"]), note, fm(t["medyan"]), fm(t["p95"]), fm(t["max"]),
            "–" if t["kayip_yuzde"] is None else "%.2f" % t["kayip_yuzde"])
    inc_rows = "".join(
        "<tr data-t0='%d' data-t1='%d'><td>%s</td><td class='num'>%d sn</td><td class='num'>%d%s</td><td>%s</td><td>%s</td></tr>" % (
            x["t0"], x["t1"], _esc(x["start"]), x["dur"], x["dropped"], " · yeniden bağlandı" if x["reconn"] else "",
            _esc(CATS[x["cat"]]["ad"]), "<br>".join(_esc(k) for k in x["kanit"]))
        for x in reversed(incs[-300:])) or "<tr><td colspan='5' class='muted'>Olay yok</td></tr>"

    return {
        "title": "OBS Stream Doctor – %s" % meta.get("baslangic", ""),
        "meta": " · ".join(x for x in (
            meta.get("baslangic", ""), "%d dk %d sn ölçüm" % (dur // 60, dur % 60), meta.get("sistem", ""),
            ("Kick sunucusu: " + meta["ingest"]) if meta.get("ingest") else "") if x),
        "verdict": verdict_html,
        "k_drop": "%d" % s["tot_drop"],
        "k_pct": "%.2f%%" % drop_pct,
        "k_inc": "%d" % len(incs),
        "k_kbps": "%.0f" % s["kbps_med"] if s["kbps_med"] else "–",
        "t_rank": rank_rows,
        "t_stats": stat_rows,
        "t_inc": inc_rows,
        "notes": "".join("<li>%s</li>" % _esc(n) for n in meta.get("notlar", [])) or "<li>–</li>",
        "traces": _esc(traces or "Traceroute çıktısı yok."),
        "now": _now_tiles(rows, an) if live else [],
        "payload": {"d": data, "step": step, "shown": shown, "names": names,
                    "inc": [[x["t0"], x["t1"]] for x in incs]},
        "_summary": s,
    }


def render_html(parts, live=False, window=0):
    html = REPORT_TEMPLATE
    boot = {k: v for k, v in parts.items() if not k.startswith("_")}
    rep = {
        "%%TITLE%%": _esc(parts["title"]),
        "%%META%%": _esc(parts["meta"]),
        "%%VERDICT%%": parts["verdict"],
        "%%KPI_DROP%%": parts["k_drop"],
        "%%KPI_DROPPCT%%": parts["k_pct"],
        "%%KPI_INC%%": parts["k_inc"],
        "%%KPI_KBPS%%": parts["k_kbps"],
        "%%RANK%%": parts["t_rank"],
        "%%STATS%%": parts["t_stats"],
        "%%INCIDENTS%%": parts["t_inc"],
        "%%TRACES%%": parts["traces"],
        "%%NOTES%%": parts["notes"],
        "%%BOOT%%": json.dumps({"live": live, "window": window, "parts": boot},
                               ensure_ascii=False).replace("</", "<\\/"),
    }
    for k, v in rep.items():
        html = html.replace(k, v)
    return html


def write_report(rows, path, meta, traces=""):
    parts = build_report(rows, meta, traces)
    html = render_html(parts, live=False)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, path)
    return parts["_summary"]


class LiveServer:
    """Tarayıcıdan canlı izleme: http://127.0.0.1:8765"""

    def __init__(self, mon, port=8765, lan=False):
        from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
        from urllib.parse import urlparse, parse_qs
        self.mon = mon
        self.cache = {}
        self.cache_lock = threading.Lock()
        srv_self = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype):
                b = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                try:
                    u = urlparse(self.path)
                    q = parse_qs(u.query)
                    w = int((q.get("w") or ["600"])[0])
                    if u.path == "/":
                        parts = srv_self.parts(w)
                        self._send(200, render_html(parts, live=True, window=w), "text/html; charset=utf-8")
                    elif u.path == "/live.json":
                        parts = srv_self.parts(w)
                        self._send(200, json.dumps({k: v for k, v in parts.items() if not k.startswith("_")},
                                                   ensure_ascii=False), "application/json; charset=utf-8")
                    else:
                        self._send(404, "bulunamadı", "text/plain; charset=utf-8")
                except Exception as e:
                    try:
                        self._send(500, "hata: %s" % e, "text/plain; charset=utf-8")
                    except Exception:
                        pass

        host = "0.0.0.0" if lan else "127.0.0.1"
        self.httpd = None
        for p in range(port, port + 15):
            try:
                self.httpd = ThreadingHTTPServer((host, p), H)
                self.port = p
                break
            except OSError:
                continue
        if not self.httpd:
            raise OSError("Boş port bulunamadı")
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/" % self.port
        self.lan_url = None
        if lan:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                self.lan_url = "http://%s:%d/" % (s.getsockname()[0], self.port)
                s.close()
            except Exception:
                pass

    def parts(self, w):
        now = time.time()
        with self.cache_lock:
            c = self.cache.get(w)
            if c and now - c[0] < 1.5:
                return c[1]
        rows = list(self.mon.rows)
        p = build_report(rows, self.mon.meta, self.mon.traces, window=w, live=True)
        with self.cache_lock:
            self.cache[w] = (now, p)
        return p

    def close(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass


REPORT_TEMPLATE = r"""<!DOCTYPE html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%%TITLE%%</title>
<style>
.viz-root{color-scheme:light;--surface-0:#f4f3ef;--surface-1:#fcfcfb;--text-primary:#0b0b0b;--text-secondary:#52514e;--text-muted:#7a7974;
--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--band:rgba(208,59,59,.10);
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--crit:#d03b3b;--good:#0ca30c;--warn:#fab219}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;--surface-0:#111110;--surface-1:#1a1a19;
--text-primary:#f0efec;--text-secondary:#c3c2b7;--text-muted:#8f8e88;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--band:rgba(208,59,59,.18);
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181}}
*{box-sizing:border-box}body{margin:0}
.viz-root{background:var(--surface-0);color:var(--text-primary);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;min-height:100vh;padding:28px 20px 60px}
.wrap{max-width:1180px;margin:0 auto}
.head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
h1{font-size:22px;margin:0}h2{font-size:15px;margin:0 0 12px}
.livebadge{display:none;align-items:center;gap:6px;font-size:12px;font-weight:600;color:var(--text-secondary);border:1px solid var(--border);border-radius:999px;padding:2px 10px;background:var(--surface-1)}
.livebadge i{width:8px;height:8px;border-radius:50%;background:var(--good)}
.livebadge.stale i{background:var(--crit)}
.meta{color:var(--text-secondary);font-size:13px;margin:4px 0 20px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin-bottom:16px}
.verdict{display:flex;gap:16px;align-items:flex-start;border-radius:12px;padding:18px 20px;margin-bottom:16px;background:var(--surface-1);border:1px solid var(--border)}
.verdict .ico{flex:none;width:30px;height:30px;border-radius:50%;display:grid;place-items:center;font-weight:700;color:#fff}
.verdict.bad .ico{background:var(--crit)}.verdict.ok .ico{background:var(--good)}
.verdict.bad{border-left:4px solid var(--crit)}.verdict.ok{border-left:4px solid var(--good)}
.vk{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-secondary)}
.vt{font-size:20px;font-weight:650;margin:2px 0}.vs{color:var(--text-secondary);font-size:13px;margin-bottom:6px}
.verdict p{margin:8px 0}.verdict ul{margin:6px 0 0;padding-left:20px}
.now{display:none;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:16px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:10px 12px}
.tile .l{font-size:12px;color:var(--text-secondary);display:flex;align-items:center;gap:6px}
.tile .l i{width:8px;height:8px;border-radius:50%;background:var(--axis);flex:none}
.tile.good .l i{background:var(--good)}.tile.warn .l i{background:var(--warn)}.tile.crit .l i{background:var(--crit)}
.tile .v{font-size:18px;font-weight:650;font-variant-numeric:tabular-nums}
.tile .st{font-size:11px;color:var(--text-muted)}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.kpi{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.kpi .l{font-size:12px;color:var(--text-secondary)}.kpi .v{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--text-secondary);font-weight:600}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.muted{color:var(--text-muted)}
.chart{position:relative;margin-bottom:6px}.chart svg{display:block;width:100%;overflow:visible}
.ctitle{font-size:13px;font-weight:600;margin:10px 0 2px}.csub{font-size:12px;color:var(--text-secondary);margin-bottom:4px}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--text-secondary);margin:2px 0 8px}
.legend span{display:inline-flex;align-items:center;gap:6px}.legend i{display:inline-block;width:14px;height:2px;border-radius:1px}
.tip{position:fixed;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:12px;box-shadow:0 4px 18px rgba(0,0,0,.14);display:none;z-index:9;min-width:180px}
.tip .h{color:var(--text-secondary);margin-bottom:4px}.tip .r{display:flex;align-items:center;gap:8px;justify-content:space-between}
.tip .r span{display:inline-flex;align-items:center;gap:6px;color:var(--text-secondary)}.tip .r b{font-variant-numeric:tabular-nums;color:var(--text-primary)}
.tip i{display:inline-block;width:12px;height:2px}
tr[data-t0]{cursor:pointer}tr[data-t0]:hover{background:var(--band)}
.zoombar{display:flex;gap:6px;align-items:center;flex-wrap:wrap;font-size:12px;color:var(--text-secondary);margin-bottom:6px}
.zoombar .sp{flex:1}
.zoombar button{font:inherit;background:var(--surface-0);color:var(--text-primary);border:1px solid var(--border);border-radius:6px;padding:3px 10px;cursor:pointer}
.zoombar button.on{background:var(--text-primary);color:var(--surface-1)}
.zoombar button.lv{display:none}
pre{white-space:pre-wrap;font-size:12px;background:var(--surface-0);padding:12px;border-radius:8px;max-height:420px;overflow:auto}
details summary{cursor:pointer;font-weight:600}
@media (max-width:800px){.kpis{grid-template-columns:repeat(2,1fr)}.grid2{grid-template-columns:1fr}}
</style></head>
<body><div class="viz-root"><div class="wrap">
<div class="head"><h1>Yayın donma teşhis raporu</h1><span class="livebadge" id="lb"><i></i><span id="lbt">CANLI</span></span></div>
<div class="meta" id="meta">%%META%%</div>
<div class="now" id="now"></div>
<div id="verdict">%%VERDICT%%</div>
<div class="kpis">
<div class="kpi"><div class="l">Ağ yüzünden düşen kare</div><div class="v" id="k_drop">%%KPI_DROP%%</div></div>
<div class="kpi"><div class="l">Düşen kare oranı</div><div class="v" id="k_pct">%%KPI_DROPPCT%%</div></div>
<div class="kpi"><div class="l">Donma olayı</div><div class="v" id="k_inc">%%KPI_INC%%</div></div>
<div class="kpi"><div class="l">Ortalama yayın bitrate (kbps)</div><div class="v" id="k_kbps">%%KPI_KBPS%%</div></div>
</div>
<div class="card">
<h2>Zaman çizelgesi</h2>
<div class="zoombar"><span id="zinfo">Grafik üzerinde sürükleyerek yakınlaştırın · olay satırına tıklayınca o ana gider</span><span class="sp"></span>
<button class="lv" data-w="600">Son 10 dk</button><button class="lv" data-w="3600">Son 1 saat</button><button id="zreset" data-w="0">Tümü</button></div>
<div class="ctitle">Yayın bitrate (kbps)</div><div class="csub">Kırmızı bantlar donma olaylarını gösterir</div>
<div class="chart" id="c_kbps"></div>
<div class="ctitle">Ağ yüzünden düşen kareler</div>
<div class="chart" id="c_drop"></div>
<div class="ctitle">Gecikme (ms, logaritmik)</div><div class="csub">Yukarıdaki kısa çizgiler o hedefte paket kaybı olduğunu gösterir</div>
<div class="legend" id="lg"></div>
<div class="chart" id="c_rtt"></div>
</div>
<div class="grid2">
<div class="card"><h2>Sebeplere göre olaylar</h2><table><thead><tr><th>Sebep</th><th class="num">Olay</th><th class="num">Düşen kare</th></tr></thead><tbody id="t_rank">%%RANK%%</tbody></table></div>
<div class="card"><h2>Ping hedefleri (tüm ölçüm)</h2><table><thead><tr><th>Hedef</th><th class="num">Medyan ms</th><th class="num">p95 ms</th><th class="num">En yüksek</th><th class="num">Kayıp %</th></tr></thead><tbody id="t_stats">%%STATS%%</tbody></table></div>
</div>
<div class="card"><h2>Donma olayları <span class="muted" style="font-weight:400">(en yeni üstte)</span></h2><table><thead><tr><th>Saat</th><th class="num">Süre</th><th class="num">Düşen kare</th><th>Sebep</th><th>Kanıt</th></tr></thead><tbody id="t_inc">%%INCIDENTS%%</tbody></table></div>
<div class="card"><h2>Notlar</h2><ul id="notes">%%NOTES%%</ul>
<details><summary>Kick sunucusuna traceroute çıktıları</summary><pre id="traces">%%TRACES%%</pre></details></div>
</div><div class="tip" id="tip"></div></div>
<script>
const BOOT=%%BOOT%%;const LIVE=BOOT.live;let WIN=BOOT.window||0;
let P=BOOT.parts.payload,D=P.d,N=D.t.length;
const COL={gw:'var(--s1)',isp:'var(--s2)',net:'var(--s3)',kick_tcp:'var(--s4)',kick_icmp:'var(--s5)'};
const NS='http://www.w3.org/2000/svg';
let x0=0,x1=Math.max(1,N-1),custom=false,pendingZoom=null;
function el(t,a,p){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);if(p)p.appendChild(e);return e}
function fmtT(ts){const d=new Date(ts*1000);return d.toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit',second:'2-digit'})}
function nice(m){if(m<=0)return 1;const p=Math.pow(10,Math.floor(Math.log10(m)));const f=m/p;return (f<=1?1:f<=2?2:f<=5?5:10)*p}
const charts=[];
function chart(id,cfg){
 const host=document.getElementById(id);const H=cfg.h;const M={l:48,r:12,t:cfg.mt||10,b:22};
 const svg=el('svg',{height:H},host);const c={host,svg,cfg,H,M};charts.push(c);
 svg.addEventListener('pointerdown',e=>{c.drag=pos(c,e);c.dragX=e.clientX});
 svg.addEventListener('pointermove',e=>hover(c,e));
 svg.addEventListener('pointerleave',()=>{document.getElementById('tip').style.display='none';charts.forEach(k=>k.ch&&k.ch.setAttribute('opacity',0))});
 window.addEventListener('pointerup',e=>{if(c.drag==null)return;const a=c.drag;c.drag=null;if(Math.abs(e.clientX-c.dragX)<6)return;const b=pos(c,e);setRange(Math.min(a,b),Math.max(a,b))});
 return c}
function W(c){return c.host.clientWidth}
function sx(c,i){return c.M.l+(i-x0)/Math.max(1,x1-x0)*(W(c)-c.M.l-c.M.r)}
function pos(c,e){const r=c.svg.getBoundingClientRect();const f=(e.clientX-r.left-c.M.l)/(r.width-c.M.l-c.M.r);return Math.max(0,Math.min(N-1,Math.round(x0+f*(x1-x0))))}
function setRange(a,b){if(b-a<5){a=Math.max(0,a-3);b=Math.min(N-1,b+3)}x0=a;x1=b;custom=true;markBtns();draw()}
function idxOf(ts){let lo=0,hi=N-1;while(lo<hi){const m=(lo+hi)>>1;if(D.t[m]<ts)lo=m+1;else hi=m}return lo}
function zoomTs(t0,t1){const a=idxOf(t0),b=idxOf(t1);const pad=Math.max(30,(b-a)*3);setRange(Math.max(0,a-pad),Math.min(N-1,b+pad));document.getElementById('c_kbps').scrollIntoView({behavior:'smooth',block:'center'})}
function bindRows(){document.querySelectorAll('tr[data-t0]').forEach(tr=>tr.onclick=()=>{const t0=+tr.dataset.t0,t1=+tr.dataset.t1;
 if(LIVE&&WIN&&(N===0||t0<D.t[0])){WIN=0;pendingZoom=[t0,t1];poll();return}zoomTs(t0,t1)})}
function markBtns(){document.querySelectorAll('.zoombar button').forEach(b=>b.classList.toggle('on',!custom&&+b.dataset.w===WIN))}
document.querySelectorAll('.zoombar button').forEach(b=>b.onclick=()=>{custom=false;const w=+b.dataset.w;
 if(LIVE&&w!==WIN){WIN=w;markBtns();poll();return}x0=0;x1=Math.max(1,N-1);markBtns();draw()});
function render(c){
 const svg=c.svg;while(svg.firstChild)svg.removeChild(svg.firstChild);const w=W(c),H=c.H,M=c.M;svg.setAttribute('viewBox','0 0 '+w+' '+H);
 if(N===0){const t=el('text',{x:w/2,y:H/2,'text-anchor':'middle','font-size':12,fill:'var(--text-muted)'},svg);t.textContent='Veri bekleniyor…';return}
 const ser=c.cfg.series();let mx=0;
 for(const s of ser)for(let i=x0;i<=x1;i++){const v=s.v[i];if(v!=null&&v>mx)mx=v}
 let top,y,ticks;const ph=H-M.t-M.b;
 if(c.cfg.log){if(!P.shown.length){const t=el('text',{x:w/2,y:H/2,'text-anchor':'middle','font-size':12,fill:'var(--text-muted)'},svg);t.textContent='Ping verisi yok';return}const T=[0,5,10,25,50,100,250,500,1000,2000,5000];top=Math.max(10,T.find(t=>t>=mx*1.05)||mx);const L=v=>Math.log10(1+Math.max(0,v));y=v=>M.t+(1-L(Math.min(v,top))/L(top))*ph;ticks=T.filter(t=>t<=top)}
 else{top=nice(mx*1.08||1);let n=4;if(top/4%1)n=5;if(top/n%1)n=top<=5?top:2;y=v=>M.t+(1-Math.min(v,top)/top)*ph;ticks=[];for(let k=0;k<=n;k++)ticks.push(top*k/n)}
 for(const [a,b] of P.inc){if(b<D.t[x0]||a>D.t[x1])continue;const ia=idxOf(a),ib=idxOf(b);const xa=sx(c,Math.max(ia,x0)),xb=Math.max(xa+2,sx(c,Math.min(ib,x1)));el('rect',{x:xa,y:M.t,width:xb-xa,height:H-M.t-M.b,fill:'var(--band)'},svg)}
 for(const v of ticks){const yy=y(v);el('line',{x1:M.l,x2:w-M.r,y1:yy,y2:yy,stroke:v?'var(--grid)':'var(--axis)','stroke-width':1},svg);const t=el('text',{x:M.l-6,y:yy+4,'text-anchor':'end','font-size':11,fill:'var(--text-muted)'},svg);t.textContent=v>=1000?(v/1000)+'k':Math.round(v)}
 const nt=Math.max(1,Math.min(x1-x0,Math.floor((w-M.l-M.r)/110)));for(let k=0;k<=nt;k++){const i=Math.round(x0+(x1-x0)*k/nt);const t=el('text',{x:sx(c,i),y:H-5,'text-anchor':k==0?'start':k==nt?'end':'middle','font-size':11,fill:'var(--text-muted)'},svg);t.textContent=fmtT(D.t[i])}
 const bw=Math.max(2,(w-M.l-M.r)/Math.max(1,x1-x0+1)-1);
 for(const s of ser){
  if(c.cfg.type==='bar'){for(let i=x0;i<=x1;i++){const v=s.v[i];if(!v)continue;const h=Math.max(2,H-M.b-y(v));el('rect',{x:sx(c,i)-bw/2,y:H-M.b-h,width:bw,height:h,rx:Math.min(2,bw/2),fill:s.c},svg)}continue}
  let d='',last=-99;for(let i=x0;i<=x1;i++){const v=s.v[i];if(v==null)continue;d+=(i-last<=3?'L':'M')+sx(c,i).toFixed(1)+','+y(v).toFixed(1);last=i}
  el('path',{d,fill:'none',stroke:s.c,'stroke-width':2,'stroke-linejoin':'round','stroke-linecap':'round'},svg);
  if(s.l){for(let i=x0;i<=x1;i++){if(s.l[i]>0){const xx=sx(c,i);el('line',{x1:xx,x2:xx,y1:1+s.o*4,y2:4+s.o*4,stroke:s.c,'stroke-width':2},svg)}}}
 }
 c.ch=el('line',{x1:0,x2:0,y1:M.t,y2:H-M.b,stroke:'var(--text-muted)','stroke-width':1,opacity:0},svg)}
function hover(c,e){
 if(N===0)return;const i=pos(c,e);charts.forEach(k=>{if(!k.ch)return;const xx=sx(k,i);k.ch.setAttribute('x1',xx);k.ch.setAttribute('x2',xx);k.ch.setAttribute('opacity',1)});
 const tip=document.getElementById('tip');tip.replaceChildren();const h=document.createElement('div');h.className='h';h.textContent=fmtT(D.t[i])+(P.step>1?' ('+P.step+' sn özet)':'');tip.appendChild(h);
 const rows=[['Bitrate',D.kbps[i]==null?'–':D.kbps[i]+' kbps',null],['Düşen kare',String(D.drop[i]),null]];
 if(D.up[i]!=null)rows.push(['PC upload',(D.up[i]/1000).toFixed(1)+' Mbps',null]);
 for(const k of P.shown){const v=D[k][i],l=D[k+'_l'][i];rows.push([P.names[k],(v==null?'–':v.toFixed(0)+' ms')+(l?' · '+l+' kayıp':''),COL[k]])}
 for(const [n,v,col] of rows){const r=document.createElement('div');r.className='r';const s=document.createElement('span');if(col){const ii=document.createElement('i');ii.style.background=col;s.appendChild(ii)}s.appendChild(document.createTextNode(n));const b=document.createElement('b');b.textContent=v;r.append(s,b);tip.appendChild(r)}
 tip.style.display='block';const tw=tip.offsetWidth,th=tip.offsetHeight;let lx=e.clientX+14,ly=e.clientY+14;if(lx+tw>innerWidth-8)lx=e.clientX-tw-14;if(ly+th>innerHeight-8)ly=e.clientY-th-14;tip.style.left=lx+'px';tip.style.top=ly+'px'}
chart('c_kbps',{h:150,series:()=>[{v:D.kbps,c:'var(--s1)'}]});
chart('c_drop',{h:90,type:'bar',series:()=>[{v:D.drop,c:'var(--crit)'}]});
chart('c_rtt',{h:260,mt:26,log:true,series:()=>P.shown.map((k,j)=>({v:D[k],l:D[k+'_l'],c:COL[k],o:j}))});
function legend(){const lg=document.getElementById('lg');lg.replaceChildren();for(const k of P.shown){const s=document.createElement('span');const i=document.createElement('i');i.style.background=COL[k];s.append(i,document.createTextNode(P.names[k]));lg.appendChild(s)}}
function nowTiles(list){const box=document.getElementById('now');box.replaceChildren();const lab={good:'normal',warn:'yüksek',crit:'sorun',none:''};
 for(const t of list){const d=document.createElement('div');d.className='tile '+t.s;const l=document.createElement('div');l.className='l';l.append(document.createElement('i'),document.createTextNode(t.l));
 const v=document.createElement('div');v.className='v';v.textContent=t.v;const st=document.createElement('div');st.className='st';st.textContent=lab[t.s]||'';d.append(l,v,st);box.appendChild(d)}}
function draw(){charts.forEach(render)}
function apply(p){
 for(const id of ['verdict','k_drop','k_pct','k_inc','k_kbps','t_rank','t_stats','t_inc','notes'])document.getElementById(id).innerHTML=p[id];
 document.getElementById('meta').textContent=p.meta;const tr=document.getElementById('traces');if(tr.innerHTML!==p.traces)tr.innerHTML=p.traces;
 nowTiles(p.now||[]);
 const oldT0=N?D.t[x0]:null,oldT1=N?D.t[x1]:null;
 P=p.payload;D=P.d;N=D.t.length;legend();
 if(custom&&oldT0!=null&&N){x0=idxOf(oldT0);x1=Math.max(x0+1,idxOf(oldT1));if(x1>N-1)x1=N-1}else{x0=0;x1=Math.max(1,N-1)}
 bindRows();draw();
 if(pendingZoom){const z=pendingZoom;pendingZoom=null;markBtns();zoomTs(z[0],z[1])}}
let lastOk=Date.now();
async function poll(){try{const r=await fetch('live.json?w='+WIN,{cache:'no-store'});if(!r.ok)throw 0;apply(await r.json());lastOk=Date.now()}catch(e){}
 const lb=document.getElementById('lb');const stale=Date.now()-lastOk>10000;lb.classList.toggle('stale',stale);document.getElementById('lbt').textContent=stale?'BAĞLANTI YOK (ölçüm durdu mu?)':'CANLI · 3 sn\'de bir güncellenir'}
legend();bindRows();
if(LIVE){document.getElementById('lb').style.display='inline-flex';document.getElementById('now').style.display='grid';
 document.querySelectorAll('.zoombar .lv').forEach(b=>b.style.display='inline-block');nowTiles(BOOT.parts.now||[]);setInterval(poll,3000)}
markBtns();draw();addEventListener('resize',draw);
</script></body></html>
"""


# ----------------------------------------------------------------------------
# Canlı izleme
# ----------------------------------------------------------------------------
class Monitor:
    def __init__(self, args):
        self.args = args
        self.store = defaultdict(dict)
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=40)
        self.ip = {}
        self.rows = []
        self.notes = []
        self.traces = ""
        self.trace_lock = threading.Lock()
        self.last_trace = 0
        self.obs = None if args.no_obs else ObsClient(args.obs_host, args.obs_port, args.obs_password)
        self.obs_ok = False
        self.obs_err = ""
        self.obs_next_try = 0
        self.prev_obs = None
        self.prev_net = None
        self.wifi = None
        self.stop = threading.Event()
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.dir = os.path.join(args.out, stamp)
        os.makedirs(self.dir, exist_ok=True)
        self.csv_path = os.path.join(self.dir, "olcumler.csv")
        self.report_path = os.path.join(self.dir, "rapor.html")
        self.trace_path = os.path.join(self.dir, "traceroute.txt")
        self.meta = {"baslangic": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                     "sistem": "%s %s" % (platform.system(), platform.release()), "ingest": args.ingest,
                     "notlar": self.notes}
        self.csv_f = open(self.csv_path, "w", newline="", encoding="utf-8")
        self.csv_w = csv.DictWriter(self.csv_f, fieldnames=FIELDS)
        self.csv_w.writeheader()
        self.live_inc = None
        self.analyzer_cache = None
        self.last_final = 0

    # --- keşif ---
    def discover(self):
        log(C_BOLD + "OBS Stream Doctor v%s" % VERSION + C_RST)
        log("Kayıt klasörü: %s" % os.path.abspath(self.dir))
        gw = default_gateway()
        kick = resolve(self.args.ingest)
        self.ip["gw"] = gw
        self.ip["kick"] = kick
        log("Modem (ağ geçidi): %s" % (gw or "bulunamadı"))
        log("Kick yayın sunucusu: %s -> %s" % (self.args.ingest, kick or "ÇÖZÜLEMEDİ"))
        if not kick:
            self.notes.append("Kick sunucu adı çözülemedi: DNS sorunu olabilir.")
        if _win_icmp:
            log(C_DIM + "Ping yöntemi: Windows ICMP API" + C_RST)
        if psutil is None and IS_MAC:
            self.notes.append("psutil kurulu değil, upload ölçülemedi (pip install psutil).")
            log(C_YEL + "Uyarı: psutil yok, upload trafiği ölçülmeyecek (pip install psutil)" + C_RST)
        threading.Thread(target=self.initial_trace, daemon=True).start()
        threading.Thread(target=self.wifi_loop, daemon=True).start()
        threading.Thread(target=self.dns_loop, daemon=True).start()

    def initial_trace(self):
        if not self.ip.get("kick"):
            return
        log(C_DIM + "Operatörün ilk sunucusunu bulmak için traceroute çalışıyor (20-40 sn)..." + C_RST)
        hops, raw = traceroute(self.ip["kick"])
        self._save_trace("Başlangıç", raw)
        gw = self.ip.get("gw")
        hop_ips = [h[1] for h in hops if h[1]]
        if not gw and hop_ips and is_private(hop_ips[0]):
            self.ip["gw"] = gw = hop_ips[0]
        privs = [ip for ip in hop_ips if is_private(ip)]
        if len(set(privs)) >= 2:
            self.notes.append("Yolda birden fazla yerel cihaz var (%s): çift modem / çift NAT. Gereksizse tek cihaza indirin."
                              % ", ".join(dict.fromkeys(privs)))
        for h, ip, rtt in hops:
            if ip and not is_private(ip) and ip != self.ip.get("kick"):
                self.ip["isp"] = ip
                log("Operatör ilk hop: %s (hop %d)" % (ip, h))
                break
        if "isp" not in self.ip:
            self.notes.append("Operatörün ilk sunucusu traceroute'ta tespit edilemedi.")

    def _save_trace(self, why, raw):
        with self.trace_lock:
            block = "=== %s  (%s) ===\n%s\n" % (datetime.now().strftime("%H:%M:%S"), why, raw.strip())
            self.traces += block + "\n"
            with open(self.trace_path, "a", encoding="utf-8") as f:
                f.write(block + "\n")

    def trigger_trace(self, why):
        now = time.time()
        if now - self.last_trace < 90 or not self.ip.get("kick"):
            return
        self.last_trace = now

        def job():
            _, raw = traceroute(self.ip["kick"])
            self._save_trace(why, raw)
        threading.Thread(target=job, daemon=True).start()

    def wifi_loop(self):
        while not self.stop.is_set():
            self.wifi = wifi_info()
            self.stop.wait(5 if IS_WIN else 15)

    def dns_loop(self):
        while not self.stop.wait(300):
            ip = resolve(self.args.ingest)
            if ip and ip != self.ip.get("kick"):
                self.notes.append("%s Kick sunucu IP'si değişti: %s -> %s" % (datetime.now().strftime("%H:%M"), self.ip.get("kick"), ip))
                self.ip["kick"] = ip

    # --- ölçüm ---
    def _put(self, sec, key, val):
        with self.lock:
            if sec > self.last_final:
                self.store[sec][key] = val

    def probe(self, sec):
        def p_icmp(key, ip):
            v = icmp_ping(ip, 2.0)
            self._put(sec, key, LOSS if v is None else v)

        def p_net():
            res = [icmp_ping("1.1.1.1", 2.0), icmp_ping("8.8.8.8", 2.0)]
            ok = [v for v in res if v is not None]
            self._put(sec, "net", min(ok) if ok else LOSS)

        def p_tcp(ip):
            v = tcp_ping(ip, 443, 2.0)
            self._put(sec, "kick_tcp", LOSS if v is None else v)

        if self.ip.get("gw"):
            self.pool.submit(p_icmp, "gw", self.ip["gw"])
        if self.ip.get("isp"):
            self.pool.submit(p_icmp, "isp", self.ip["isp"])
        self.pool.submit(p_net)
        if self.ip.get("kick"):
            self.pool.submit(p_icmp, "kick_icmp", self.ip["kick"])
            if sec % 2 == 0:
                self.pool.submit(p_tcp, self.ip["kick"])

    def sample_obs(self, sec):
        if not self.obs:
            return
        now = time.time()
        if not self.obs_ok:
            if now < self.obs_next_try:
                return
            try:
                self.obs.connect()
                self.obs_ok = True
                self.obs_err = ""
                self.prev_obs = None
                log(C_GRN + "OBS'e bağlanıldı." + C_RST)
            except ObsAuthError as e:
                cfg = obs_websocket_config()
                if cfg and cfg.get("password") and cfg["password"] != self.obs.password:
                    self.obs.password = cfg["password"]
                    self.obs_next_try = now + 1
                    return
                self.obs_err = str(e)
                self.obs_next_try = now + 30
                log(C_RED + str(e) + C_RST)
                return
            except Exception as e:
                if self.obs_err != str(e):
                    log(C_YEL + "OBS'e bağlanılamadı (%s). OBS açık ve WebSocket etkin mi? 5 sn'de bir denenecek." % e + C_RST)
                self.obs_err = str(e)
                self.obs_next_try = now + 5
                return
        try:
            ss = self.obs.request("GetStreamStatus")
            st = self.obs.request("GetStats")
        except Exception as e:
            self.obs_ok = False
            self.obs.close()
            self.obs_err = str(e)
            log(C_YEL + "OBS bağlantısı koptu: %s" % e + C_RST)
            return
        cur = {"t": time.perf_counter(), "bytes": ss.get("outputBytes", 0), "skip": ss.get("outputSkippedFrames", 0),
               "tot": ss.get("outputTotalFrames", 0), "enc": st.get("outputSkippedFrames", 0),
               "ren": st.get("renderSkippedFrames", 0)}
        row = {"obs_aktif": 1 if ss.get("outputActive") else 0,
               "obs_yeniden_baglaniyor": 1 if ss.get("outputReconnecting") else 0,
               "obs_tikaniklik": round(float(ss.get("outputCongestion") or 0), 3),
               "obs_cpu": round(float(st.get("cpuUsage") or 0), 1),
               "obs_fps": round(float(st.get("activeFps") or 0), 1),
               "obs_render_ms": round(float(st.get("averageFrameRenderTime") or 0), 2)}
        p = self.prev_obs
        if p:
            dt = cur["t"] - p["t"]

            def d(k):
                v = cur[k] - p[k]
                return v if v >= 0 else None
            db = d("bytes")
            if db is not None and dt > 0.2 and row["obs_aktif"]:
                row["obs_kbps"] = round(db * 8 / dt / 1000)
            for k, f in (("skip", "obs_ag_dusen_kare"), ("tot", "obs_toplam_kare"),
                         ("enc", "obs_encoder_atlanan"), ("ren", "obs_render_atlanan")):
                v = d(k)
                if v is not None:
                    row[f] = v
        self.prev_obs = cur
        with self.lock:
            if sec > self.last_final:
                self.store[sec].update(row)

    def sample_net(self, sec):
        nb = net_bytes()
        t = time.perf_counter()
        if nb and self.prev_net:
            dt = t - self.prev_net[0]
            if dt > 0.2:
                up = (nb[0] - self.prev_net[1][0]) * 8 / dt / 1000
                dn = (nb[1] - self.prev_net[1][1]) * 8 / dt / 1000
                if up >= 0 and dn >= 0:
                    with self.lock:
                        self.store[sec]["pc_upload_kbps"] = round(up)
                        self.store[sec]["pc_download_kbps"] = round(dn)
        if nb:
            self.prev_net = (t, nb)
        w = self.wifi
        if w:
            with self.lock:
                self.store[sec]["wifi_sinyal"] = w.get("signal")
                self.store[sec]["wifi_bssid"] = w.get("bssid", "")

    # --- satır yazma ---
    def finalize(self, sec):
        with self.lock:
            d = self.store.pop(sec, {})
            self.last_final = max(self.last_final, sec)
        row = {"ts": sec, "saat": datetime.fromtimestamp(sec).strftime("%H:%M:%S")}
        for k, _ in TARGETS:
            if k in d:
                row["%s_ms" % k] = round(d[k], 1)
        for k in FIELDS:
            if k not in row and k in d:
                row[k] = d[k]
        self.rows.append(row)
        self.csv_w.writerow(row)
        self.csv_f.flush()
        self.print_status(row)
        self.live_detect()

    def print_status(self, r):
        def tgt(k, label):
            v = _f(r.get("%s_ms" % k))
            if v is None:
                return C_DIM + "%s –" % label + C_RST
            if v < 0:
                return C_RED + "%s KAYIP" % label + C_RST
            col = C_GRN if v < 60 else (C_YEL if v < 150 else C_RED)
            if k == "gw" and v >= 20:
                col = C_YEL if v < 60 else C_RED
            return col + "%s %.0fms" % (label, v) + C_RST

        parts = [C_DIM + r["saat"] + C_RST]
        if self.obs:
            if not self.obs_ok:
                parts.append(C_YEL + "OBS bağlı değil" + C_RST)
            elif not _f(r.get("obs_aktif")):
                parts.append(C_DIM + "OBS yayında değil" + C_RST)
            else:
                drop = int(_f(r.get("obs_ag_dusen_kare")) or 0)
                kb = r.get("obs_kbps")
                s = "OBS %s kbps" % (kb if kb is not None else "–")
                if drop:
                    s = C_RED + C_BOLD + s + "  DÜŞEN KARE +%d" % drop + C_RST
                if _f(r.get("obs_yeniden_baglaniyor")):
                    s += C_RED + " YENİDEN BAĞLANIYOR" + C_RST
                parts.append(s)
        parts += [tgt("gw", "Modem"), tgt("isp", "Operatör"), tgt("net", "İnternet"), tgt("kick_tcp", "Kick")]
        up = r.get("pc_upload_kbps")
        if up is not None:
            parts.append("Upload %.1f Mbps" % (up / 1000.0))
        if r.get("wifi_sinyal") is not None:
            sg = r["wifi_sinyal"]
            parts.append((C_RED if sg < 40 else C_YEL if sg < 60 else C_GRN) + "Wi-Fi %%%s" % sg + C_RST)
        log(" | ".join(parts))

    def live_detect(self):
        n = len(self.rows)
        if n < 10:
            return
        if self.analyzer_cache is None or n % 60 == 0:
            self.analyzer_cache = Analyzer(self.rows)
        an = self.analyzer_cache
        an.rows, an.n = self.rows, n
        i = n - 1
        if an.is_event(self.rows[i]):
            if self.live_inc is None:
                self.live_inc = [i, i]
                self.trigger_trace("Kare düşmesi %s" % self.rows[i]["saat"])
            else:
                self.live_inc[1] = i
        elif self.live_inc and i - self.live_inc[1] >= 3:
            a, b = self.live_inc
            self.live_inc = None
            res = an.classify(a, b, an.target_noise([(a, b)]))
            msg = "\n%s>>> DONMA %s (%d sn, %d kare düştü) -> Muhtemel sebep: %s%s" % (
                C_RED + C_BOLD, res["start"], res["dur"], res["dropped"], CATS[res["cat"]]["ad"], C_RST)
            log(msg)
            for k in res["kanit"]:
                log("    - " + k)
            log("")
            with open(os.path.join(self.dir, "olaylar.txt"), "a", encoding="utf-8") as f:
                f.write("%s  %d sn  %d kare  %s\n    %s\n" % (res["start"], res["dur"], res["dropped"],
                                                         CATS[res["cat"]]["ad"], "\n    ".join(res["kanit"])))

    def emergency_save(self):
        try:
            self.csv_f.flush()
        except Exception:
            pass
        rows = list(self.rows)
        if rows:
            write_report(rows, self.report_path, self.meta, self.traces)

    def report(self):
        if self.rows:
            return write_report(self.rows, self.report_path, self.meta, self.traces)

    def run(self):
        install_close_handler(self)
        self.discover()
        self.live = None
        if not self.args.no_live:
            try:
                self.live = LiveServer(self, self.args.port, self.args.lan)
                log(C_BOLD + "Canlı panel: " + self.live.url + C_RST)
                if self.live.lan_url:
                    log("Telefondan (aynı Wi-Fi): " + self.live.lan_url)
                if not self.args.no_open:
                    threading.Timer(4.0, open_file, args=(self.live.url,)).start()
            except Exception as e:
                log(C_YEL + "Canlı panel açılamadı: %s" % e + C_RST)
        log(C_BOLD + "Ölçüm başladı. Yayını şimdi açabilirsiniz." + C_RST)
        log("Yayın bitince bu pencerede " + C_BOLD + "Ctrl+C" + C_RST + " tuşlarına basın, rapor açılacak.\n")
        next_t = math.floor(time.time()) + 1
        last_report = time.time()
        try:
            while True:
                now = time.time()
                if next_t > now:
                    time.sleep(next_t - now)
                sec = int(next_t)
                self.probe(sec)
                self.sample_obs(sec)
                self.sample_net(sec)
                fin = sec - ROW_LAG
                with self.lock:
                    pending = sorted(k for k in self.store if k <= fin)
                for s in pending:
                    self.finalize(s)
                if time.time() - last_report > 60:
                    last_report = time.time()
                    try:
                        self.report()
                    except Exception as e:
                        log(C_YEL + "Rapor yazılamadı: %s" % e + C_RST)
                next_t += 1
                if time.time() - next_t > 5:  # uyku/donma sonrası
                    next_t = math.floor(time.time()) + 1
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()
            time.sleep(min(ROW_LAG, 2.2))
            with self.lock:
                pending = sorted(self.store)
            for s in pending:
                self.finalize(s)
            self.csv_f.close()
            s = self.report()
            log("\n" + C_BOLD + "Ölçüm bitti." + C_RST)
            if s:
                print_summary(s)
            log("Rapor: %s" % os.path.abspath(self.report_path))
            if not self.args.no_open and self.rows:
                open_file(os.path.abspath(self.report_path))
            self.pool.shutdown(wait=False)
            if self.live:
                self.live.close()


def print_summary(s):
    log("Ağ yüzünden düşen kare: %d   Donma olayı: %d" % (s["tot_drop"], len(s["incidents"])))
    for k, v in s["ranking"]:
        log("  %-45s %3d olay  %6d kare" % (CATS[k]["ad"], v["n"], v["dropped"]))


def analyze_csv(path, no_open=False):
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    d = os.path.dirname(os.path.abspath(path))
    tp = os.path.join(d, "traceroute.txt")
    traces = open(tp, encoding="utf-8").read() if os.path.exists(tp) else ""
    out = os.path.join(d, "rapor.html")
    t0 = _f(rows[0].get("ts")) if rows else None
    meta = {"baslangic": datetime.fromtimestamp(t0).strftime("%d.%m.%Y %H:%M:%S") if t0 else "",
            "sistem": "", "ingest": "", "notlar": ["Rapor kayıttan yeniden üretildi."]}
    s = write_report(rows, out, meta, traces)
    print_summary(s)
    log("Rapor: %s" % out)
    if not no_open:
        open_file(out)


# ----------------------------------------------------------------------------
# OBS ayarlarını otomatik okuma, kayıt klasörü, kalıcı ayarlar
# ----------------------------------------------------------------------------
def obs_dir():
    if IS_WIN:
        return os.path.join(os.environ.get("APPDATA", ""), "obs-studio")
    if IS_MAC:
        return os.path.expanduser("~/Library/Application Support/obs-studio")
    return os.path.expanduser("~/.config/obs-studio")


def _ini_section(path, section):
    import configparser
    if not os.path.exists(path):
        return None
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            cp.read_file(f)
    except Exception:
        return None
    return dict(cp[section]) if cp.has_section(section) else None


def obs_websocket_config():
    """OBS'in kendi ayar dosyalarından WebSocket ayarlarını okur (şifre dahil)."""
    d = obs_dir()
    for ini in ("user.ini", "global.ini"):
        sec = _ini_section(os.path.join(d, ini), "OBSWebSocket")
        if sec and ("serverenabled" in sec or "serverpassword" in sec):
            return {"enabled": sec.get("serverenabled", "false").strip().lower() == "true",
                    "port": int(sec.get("serverport", "4455") or 4455),
                    "auth": sec.get("authrequired", "true").strip().lower() == "true",
                    "password": sec.get("serverpassword", "")}
    pj = os.path.join(d, "plugin_config", "obs-websocket", "config.json")
    if os.path.exists(pj):
        try:
            with open(pj, encoding="utf-8-sig") as f:
                j = json.load(f)
            return {"enabled": bool(j.get("server_enabled")), "port": int(j.get("server_port", 4455)),
                    "auth": bool(j.get("auth_required", True)), "password": j.get("server_password", "")}
        except Exception:
            pass
    return None


def obs_kick_server():
    """OBS profillerindeki yayın sunucusu adresi (yayın anahtarı OKUNMAZ)."""
    import glob
    d = obs_dir()
    cur = None
    for ini in ("user.ini", "global.ini"):
        sec = _ini_section(os.path.join(d, ini), "Basic")
        if sec and sec.get("profiledir"):
            cur = sec["profiledir"]
            break
    best = None
    for sj in glob.glob(os.path.join(d, "basic", "profiles", "*", "service.json")):
        try:
            with open(sj, encoding="utf-8-sig") as f:
                srv = ((json.load(f).get("settings") or {}).get("server") or "").strip()
        except Exception:
            continue
        if not srv or not ("live-video.net" in srv or "kick" in srv.lower()):
            continue
        prio = 1 if cur and os.path.basename(os.path.dirname(sj)) == cur else 0
        key = (prio, os.path.getmtime(sj))
        if best is None or key > best[0]:
            best = (key, srv)
    return best[1] if best else None


def desktop_dir():
    if IS_WIN:
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(520)
            ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)
            if buf.value and os.path.isdir(buf.value):
                return buf.value
        except Exception:
            pass
    d = os.path.expanduser("~/Desktop")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def settings_path():
    base = os.environ.get("APPDATA") if IS_WIN else os.path.expanduser("~/.config")
    return os.path.join(base or os.path.expanduser("~"), "OBSStreamDoctor", "ayarlar.json")


def load_settings():
    try:
        with open(settings_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(d):
    try:
        os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


def open_file(path):
    try:
        if IS_WIN:
            os.startfile(path)  # type: ignore
        else:
            import webbrowser
            webbrowser.open(path if "://" in path else "file://" + path)
    except Exception:
        pass


_close_cb = None


def install_close_handler(mon):
    """Pencere X ile kapatılırsa raporu son kez kaydet."""
    global _close_cb
    if not IS_WIN:
        return
    try:
        import ctypes
        from ctypes import wintypes
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def handler(ev):
            if ev in (2, 5, 6):
                try:
                    mon.emergency_save()
                except Exception:
                    pass
            return False
        _close_cb = proto(handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_close_cb, True)
    except Exception:
        pass


def setup_obs(args):
    """Şifre/port: önce komut satırı, sonra OBS'in kendi ayarları, en son kullanıcıya sor."""
    if args.no_obs or args.obs_password is not None:
        return
    for attempt in range(3):
        cfg = obs_websocket_config()
        if cfg and cfg["enabled"]:
            args.obs_port = cfg["port"]
            args.obs_password = cfg["password"] if cfg["auth"] else ""
            log(C_GRN + "OBS WebSocket ayarları otomatik bulundu (port %d)." % cfg["port"] + C_RST)
            return
        if cfg and not cfg["enabled"]:
            log("")
            log(C_YEL + C_BOLD + "OBS'te WebSocket kapalı. Bir kereliğine açmanız gerekiyor:" + C_RST)
            log("  1) OBS'i açın")
            log("  2) Üst menüden  Araçlar  >  WebSocket Sunucu Ayarları")
            log("  3) 'WebSocket sunucusunu etkinleştir' kutusunu işaretleyin")
            log("  4) 'Uygula' ve 'Tamam' düğmelerine basın")
            try:
                input(C_BOLD + "Bunu yaptıktan sonra bu pencerede Enter'a basın..." + C_RST)
            except EOFError:
                break
            continue
        break
    st = load_settings()
    saved = st.get("obs_password", "")
    try:
        prompt = "OBS WebSocket şifresi (OBS > Araçlar > WebSocket Sunucu Ayarları > Bağlantı bilgilerini göster)"
        prompt += " [Enter = kayıtlı şifre]: " if saved else " [şifre yoksa Enter]: "
        pw = input(prompt).strip()
    except EOFError:
        pw = ""
    args.obs_password = pw or saved
    if pw:
        st["obs_password"] = pw
        save_settings(st)


def main():
    ap = argparse.ArgumentParser(description="OBS -> Kick yayın donma teşhis monitörü")
    ap.add_argument("--obs-host", default="127.0.0.1")
    ap.add_argument("--obs-port", type=int, default=4455)
    ap.add_argument("--obs-password", default=None, help="OBS WebSocket şifresi (verilmezse OBS ayarlarından okunur)")
    ap.add_argument("--no-obs", action="store_true", help="OBS'e bağlanmadan sadece ağı ölç")
    ap.add_argument("--ingest", default=None, help="Kick yayın sunucusu (verilmezse OBS ayarlarından okunur)")
    ap.add_argument("--out", default=None, help="Kayıt klasörü (varsayılan: Masaüstü\\OBS Stream Doctor Raporları)")
    ap.add_argument("--analyze", metavar="CSV", help="Kayıtlı olcumler.csv'den raporu yeniden üret")
    ap.add_argument("--no-open", action="store_true", help="Bitince raporu tarayıcıda açma")
    ap.add_argument("--pause-at-end", action="store_true", help="Bitince pencereyi açık tut")
    ap.add_argument("--port", type=int, default=8765, help="Canlı panel portu (varsayılan 8765)")
    ap.add_argument("--lan", action="store_true", help="Canlı paneli aynı ağdaki telefondan da aç (güvenlik duvarı izni ister)")
    ap.add_argument("--no-live", action="store_true", help="Canlı paneli kapat")
    args = ap.parse_args()
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW("OBS Stream Doctor")
        except Exception:
            pass
    try:
        if args.analyze:
            analyze_csv(args.analyze, args.no_open)
            return
        if args.out is None:
            args.out = os.path.join(desktop_dir(), "OBS Stream Doctor Raporları")
        if not args.ingest:
            srv = obs_kick_server()
            if srv:
                log(C_DIM + "Kick sunucusu OBS ayarlarından alındı." + C_RST)
            args.ingest = srv or DEFAULT_INGEST
        args.ingest = re.sub(r"^\w+://", "", args.ingest).split("/")[0].split(":")[0]
        setup_obs(args)
        Monitor(args).run()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        import traceback
        log(C_RED + "\nBeklenmeyen hata: %s" % e + C_RST)
        traceback.print_exc()
    finally:
        if args.pause_at_end:
            try:
                input("\nPencereyi kapatmak için Enter'a basın...")
            except Exception:
                pass


if __name__ == "__main__":
    main()
