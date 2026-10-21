"""Tests de bout en bout : lance le vrai serveur + un faux ntfy. `python3 test_server.py`"""
import copy
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

RECEIVED = []


class FakeNtfy(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        RECEIVED.append(json.loads(self.rfile.read(n)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


fake = HTTPServer(("127.0.0.1", 0), FakeNtfy)
threading.Thread(target=fake.serve_forever, daemon=True).start()

TOKEN = "t" * 24
TMP = Path(tempfile.mkdtemp())
CONTENT_COPY = TMP / "content.json"
shutil.copy(Path(__file__).parent / "content.json", CONTENT_COPY)
os.environ.update({
    "MOB_TOKEN": TOKEN, "MOB_DB": str(TMP / "t.db"), "MOB_CONTENT": str(CONTENT_COPY),
    "NTFY_URL": f"http://127.0.0.1:{fake.server_port}", "NTFY_TOPIC": "test-topic",
    "PUBLIC_URL": "https://mob.example.org", "MOB_TZ": "Europe/Paris",
})
import server  # noqa: E402

server.init_db()
from http.server import ThreadingHTTPServer  # noqa: E402

srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_port


def call(method, path, body=None, token=TOKEN):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    h = {}
    if token:
        h["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body)
        h["Content-Type"] = "application/json"
    c.request(method, path, data, h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw


def at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=server.TZ)


class T(unittest.TestCase):
    def setUp(self):
        server.FAILS.clear()
        RECEIVED.clear()
        server.run("DELETE FROM log")
        server.run("DELETE FROM sent")
        server.run("DELETE FROM kv")

    def test_health_and_auth(self):
        self.assertEqual(call("GET", "/api/health", token=None)[0], 200)
        self.assertEqual(call("GET", "/api/state", token=None)[0], 401)
        self.assertEqual(call("GET", "/api/state", token="mauvais")[0], 401)
        s, d = call("GET", "/api/state")
        self.assertEqual(s, 200)
        self.assertEqual(d["cfg"]["every"], 2)
        self.assertTrue(d["meta"]["notify_enabled"])

    def test_log_roundtrip(self):
        today = datetime.now(server.TZ).date().isoformat()
        self.assertEqual(call("POST", "/api/log", {"day": today, "slot": "p10:00", "done": True})[0], 200)
        self.assertEqual(call("POST", "/api/log", {"day": today, "slot": "force", "done": True})[0], 200)
        _, d = call("GET", "/api/state")
        self.assertEqual(d["log"][today], {"p10:00": True, "force": True})
        call("POST", "/api/log", {"day": today, "slot": "p10:00", "done": False})
        _, d = call("GET", "/api/state")
        self.assertEqual(d["log"][today], {"force": True})

    def test_log_validation(self):
        today = datetime.now(server.TZ).date().isoformat()
        for body in ({"day": today, "slot": "hack", "done": True},
                     {"day": "2026-13-45", "slot": "reveil", "done": True},
                     {"day": "2099-01-01", "slot": "reveil", "done": True},
                     {"day": today, "slot": "reveil", "done": "oui"},
                     {"day": today, "slot": "p25:99", "done": True}):
            self.assertEqual(call("POST", "/api/log", body)[0], 400, body)

    def test_cfg_validation(self):
        s, c = call("PUT", "/api/cfg", {"every": 3, "wake": "06:45", "relance": 30, "notify": False,
                                        "workdays": [0, 1, 2], "force_days": [6]})
        self.assertEqual((s, c["every"], c["wake"], c["relance"], c["notify"]), (200, 3, "06:45", 30, False))
        s, c = call("PUT", "/api/cfg", {"every": 5, "wake": "25:00", "workdays": [9], "bogus": 1, "notify": "x",
                                        "stand": "non"})
        self.assertEqual((c["every"], c["wake"], c["workdays"], c["stand"]), (3, "06:45", [0, 1, 2], True))
        self.assertNotIn("bogus", c)

    def test_quick_link(self):
        today = datetime.now(server.TZ).date().isoformat()
        sig = server.sign(today, "reveil")
        self.assertEqual(call("POST", f"/api/quick?day={today}&slot=reveil&sig={sig}", token=None)[0], 200)
        self.assertIn("reveil", server.done_slots(today))
        self.assertEqual(call("POST", f"/api/quick?day={today}&slot=coucher&sig={sig}", token=None)[0], 403)
        old = "2020-01-01"
        self.assertEqual(call("POST", f"/api/quick?day={old}&slot=reveil&sig={server.sign(old, 'reveil')}", token=None)[0], 410)

    def test_tick_sends_once_then_relance(self):
        monday = (2026, 10, 19)
        self.assertEqual(server.tick(at(*monday, 10, 0)), 1)
        m = RECEIVED[0]
        self.assertEqual(m["topic"], "test-topic")
        self.assertIn("Pause mouvement · 10:00", m["title"])
        self.assertEqual(m["click"], "https://mob.example.org/?start=p10:00")
        http_action = [a for a in m["actions"] if a["action"] == "http"][0]
        self.assertIn("sig=" + server.sign("2026-10-19", "p10:00"), http_action["url"])
        self.assertEqual(server.tick(at(*monday, 10, 3)), 0)       # pas de doublon
        self.assertEqual(server.tick(at(*monday, 10, 15)), 1)      # relance
        self.assertTrue(RECEIVED[1]["title"].startswith("Pas encore fait ?"))
        self.assertEqual(server.tick(at(*monday, 10, 16)), 0)      # une seule relance
        self.assertEqual(server.tick(at(*monday, 10, 40)), 0)      # fenêtre dépassée

    def test_tick_skips_done_and_offdays(self):
        server.set_done("2026-10-19", "p10:00", True)
        self.assertEqual(server.tick(at(2026, 10, 19, 10, 0)), 0)  # déjà fait
        self.assertEqual(server.tick(at(2026, 10, 17, 10, 0)), 0)  # samedi : pas de pause bureau
        self.assertEqual(server.tick(at(2026, 10, 17, 18, 30)), 1)  # samedi : séance de force
        self.assertIn("Séance de force", RECEIVED[-1]["title"])
        self.assertEqual(server.tick(at(2026, 10, 18, 7, 30)), 1)  # réveil tous les jours
        self.assertEqual(server.tick(at(2026, 10, 19, 22, 30)), 1)  # coucher

    def test_standing_slots(self):
        monday = server.date(2026, 10, 19)
        ids = [s["id"] for s in server.slots_for(monday, server.get_cfg())]
        self.assertEqual([i for i in ids if i[0] in "pd"],
                         ["p10:00", "d11:00", "p12:00", "d13:00", "p14:00", "d15:00", "p16:00", "d17:00", "p18:00"])
        self.assertEqual(server.tick(at(2026, 10, 19, 11, 0)), 1)
        self.assertIn("Bureau debout · 11:00", RECEIVED[-1]["title"])
        self.assertIn("slot=d11:00", [a for a in RECEIVED[-1]["actions"] if a["action"] == "http"][0]["url"])
        today = datetime.now(server.TZ).date().isoformat()
        self.assertEqual(call("POST", "/api/log", {"day": today, "slot": "d11:00", "done": True})[0], 200)
        self.assertEqual(server.tick(at(2026, 10, 17, 11, 0)), 0)  # samedi : pas de bureau
        call("PUT", "/api/cfg", {"stand": False, "every": 1})
        self.assertEqual(server.tick(at(2026, 10, 19, 13, 30)), 0)
        self.assertNotIn("d10:30", [s["id"] for s in server.slots_for(monday, server.get_cfg())])

    def test_server_down_does_not_spam(self):
        self.assertEqual(server.tick(at(2026, 10, 19, 14, 30)), 0)  # 14:00 raté de 30 min, pas de rattrapage

    def test_notify_off(self):
        call("PUT", "/api/cfg", {"notify": False})
        self.assertEqual(server.tick(at(2026, 10, 19, 10, 0)), 0)

    def test_test_notify(self):
        s, d = call("POST", "/api/test-notify")
        self.assertEqual((s, d["ok"]), (200, True))
        self.assertEqual(RECEIVED[-1]["title"], "Test des rappels")

    def test_static(self):
        s, body = call("GET", "/", token=None)
        self.assertEqual(s, 200)
        self.assertIn(b"Cinq minutes", body)
        self.assertEqual(call("GET", "/manifest.webmanifest", token=None)[0], 200)
        self.assertEqual(call("GET", "/../server.py", token=None)[0], 404)
        self.assertEqual(call("GET", "/%2e%2e/server.py", token=None)[0], 404)
        self.assertEqual(call("GET", "/nope", token=None)[0], 404)

    # ---- content.json
    def write_content(self, data):
        CONTENT_COPY.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        st = CONTENT_COPY.stat()
        os.utime(CONTENT_COPY, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # force un nouvel mtime

    def tearDown(self):
        self.write_content(self.original) if hasattr(self, "original") else None

    def load_original(self):
        self.original = json.loads((Path(__file__).parent / "content.json").read_text(encoding="utf-8"))
        return copy.deepcopy(self.original)

    def test_content_served_and_valid(self):
        s, d = call("GET", "/content.json", token=None)
        self.assertEqual(s, 200)
        self.assertEqual(len(d["exercises"]), 34)
        errs, warns = server.validate_content(d)
        self.assertEqual((errs, warns), ([], []))

    def test_content_validation_catches_mistakes(self):
        c = self.load_original()
        c["routines"]["pause"]["steps"].append({"ex": "exo_qui_nexiste_pas", "sec": 30})
        c["routines"]["reveil"]["steps"][0]["sec"] = "45"
        c["exercises"]["Mauvais Id"] = {"name": "x", "how": "y", "tags": ["Cou"]}
        c["exercises"]["clam"]["tags"] = ["Inconnu"]
        del c["routines"]["coucher"]
        c["force"]["phases"][0]["items"][0]["ex"] = "nope"
        c["tips"].append({"title": "Sans items"})
        errs, warns = server.validate_content(c)
        joined = "\n".join(errs)
        for needle in ("exo_qui_nexiste_pas", '"sec"', "Mauvais Id", "coucher", "phase 1", "conseil 8"):
            self.assertIn(needle, joined)
        self.assertTrue(any("Inconnu" in w for w in warns))

    def test_content_durations_warning(self):
        c = self.load_original()
        c["routines"]["pause"]["steps"][0]["sec"] = 200
        _, warns = server.validate_content(c)
        self.assertTrue(any("routine « pause » : dure" in w for w in warns))

    def test_notification_text_comes_from_content(self):
        c = self.load_original()
        c["notifications"]["pause"] = {"title": "Debout !", "body": "Texte perso"}
        c["notifications"]["relance"] = {"prefix": "Hé ! ", "body": "Relance perso"}
        self.write_content(c)
        server.tick(at(2026, 10, 19, 10, 0))
        self.assertEqual((RECEIVED[-1]["title"], RECEIVED[-1]["message"]), ("Debout ! · 10:00", "Texte perso"))
        server.tick(at(2026, 10, 19, 10, 15))
        self.assertEqual((RECEIVED[-1]["title"], RECEIVED[-1]["message"]), ("Hé ! Debout ! · 10:00", "Relance perso"))

    def test_broken_content_keeps_last_good_version(self):
        c = self.load_original()
        c["notifications"]["reveil"] = {"title": "Version valide", "body": "ok"}
        self.write_content(c)
        self.assertEqual(server.notif_text("reveil")[0], "Version valide")
        CONTENT_COPY.write_text("{ pas du json", encoding="utf-8")
        st = CONTENT_COPY.stat()
        os.utime(CONTENT_COPY, ns=(st.st_atime_ns, st.st_mtime_ns + 9_000_000_000))
        self.assertEqual(server.notif_text("reveil")[0], "Version valide")   # notifications : dernière version valide
        self.assertEqual(call("GET", "/content.json", token=None)[0], 500)   # la page est prévenue
        self.assertEqual(server.check_cli(), 1)

    def test_rate_limit(self):
        for _ in range(10):
            self.assertEqual(call("GET", "/api/state", token="x")[0], 401)
        self.assertEqual(call("GET", "/api/state", token="x")[0], 429)
        self.assertEqual(call("GET", "/api/state")[0], 429)  # même le bon jeton est bloqué un temps
        server.FAILS.clear()


if __name__ == "__main__":
    unittest.main(verbosity=2)
