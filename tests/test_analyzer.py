"""Teşhis motoru testleri: her sebep türü için sentetik ölçüm üretip doğru sınıflandırıldığını kontrol eder."""
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import obs_stream_doctor as osd  # noqa: E402

SCENARIOS = {200: "LOCAL", 500: "UPLOAD", 800: "KICK_ROUTE", 1100: "ISP", 1400: "OTHER_UPLOAD", 1650: "UNKNOWN"}


def make_rows(n=1800, seed=1):
    random.seed(seed)
    t0 = 1_800_000_000
    rows = []
    for i in range(n):
        r = {"ts": t0 + i, "saat": "%02d:%02d:%02d" % ((i // 3600) % 24, (i // 60) % 60, i % 60)}
        gw, isp, net, kt = random.uniform(1, 3), random.uniform(6, 10), random.uniform(12, 16), random.uniform(45, 55)
        if random.random() < 0.08:
            isp = osd.LOSS  # operatör ilk hop'u ICMP'yi bazen düşürür (gürültü)
        kbps, drop, up = 6000 + random.randint(-150, 150), 0, None
        cur = next((c for s, c in SCENARIOS.items() if s <= i < s + 5), None)
        if cur:
            drop, kbps = random.randint(10, 40), 2500
            if cur == "LOCAL":
                gw = osd.LOSS if i % 2 else 180
                net = kt = osd.LOSS
            elif cur == "UPLOAD":
                isp, net, kt = isp + 400, net + 420, kt + 430
            elif cur == "KICK_ROUTE":
                kt = osd.LOSS if i % 2 else 300
            elif cur == "ISP":
                isp = net = osd.LOSS
                kt = osd.LOSS if i % 3 else kt
            elif cur == "OTHER_UPLOAD":
                up = kbps + 8000
        if up is None:
            up = kbps * 1.03
        r.update(gw_ms=round(gw, 1), isp_ms=round(isp, 1), net_ms=round(net, 1), kick_icmp_ms=osd.LOSS,
                 obs_aktif=1, obs_kbps=kbps, obs_ag_dusen_kare=drop, obs_toplam_kare=60,
                 pc_upload_kbps=round(up), wifi_sinyal=85, wifi_bssid="aa:bb")
        if i % 2 == 0:
            r["kick_tcp_ms"] = round(kt, 1)
        rows.append(r)
    return rows


class AnalyzerTest(unittest.TestCase):
    def test_scenarios_classified(self):
        s = osd.Analyzer(make_rows()).summary()
        got = [x["cat"] for x in s["incidents"]]
        self.assertEqual(got, list(SCENARIOS.values()))

    def test_unresponsive_target_ignored(self):
        s = osd.Analyzer(make_rows()).summary()
        self.assertGreaterEqual(s["tstats"]["kick_icmp"]["gurultu"], 0.5)
        for x in s["incidents"]:
            self.assertFalse(any("ping)" in k for k in x["kanit"]), x["kanit"])

    def test_no_incidents(self):
        rows = [r for r in make_rows() if not (r["obs_ag_dusen_kare"])]
        for r in rows:
            r["obs_kbps"] = 6000
        self.assertEqual(osd.Analyzer(rows).summary()["incidents"], [])

    def test_report_files(self):
        rows = make_rows()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "rapor.html")
            osd.write_report(rows, p, {"baslangic": "test", "notlar": []})
            with open(p, encoding="utf-8") as f:
                html = f.read()
            self.assertIn("Upload hattı doluyor", html)
            self.assertNotIn("%%", html)
            live = osd.build_report(rows, {"notlar": []}, window=600, live=True)
            self.assertEqual(len(live["payload"]["d"]["t"]), 600)
            self.assertTrue(live["now"])


if __name__ == "__main__":
    unittest.main()
