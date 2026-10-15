#!/usr/bin/env python3
"""Mobilité : suivi de routines de mouvement + rappels ntfy.

Serveur mono-utilisateur, bibliothèque standard uniquement (Python 3.9+).
Configuration par variables d'environnement, voir .env.example.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

LOG = logging.getLogger("mobilite")
ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


TOKEN = env("MOB_TOKEN")
HOST = env("MOB_HOST", "127.0.0.1")
PORT = int(env("MOB_PORT", "8080"))
DB_PATH = Path(env("MOB_DB", str(ROOT / "data" / "mobilite.db")))
TRUST_PROXY = env("MOB_TRUST_PROXY", "0") == "1"
PUBLIC_URL = env("PUBLIC_URL").rstrip("/")
NTFY_URL = env("NTFY_URL", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = env("NTFY_TOPIC")
NTFY_TOKEN = env("NTFY_TOKEN")
try:
    TZ = ZoneInfo(env("MOB_TZ", "Europe/Paris"))
except Exception:  # tzdata manquant ou fuseau inconnu
    sys.exit("Fuseau horaire introuvable : vérifie MOB_TZ et installe tzdata (pip install tzdata).")

WINDOW_MIN = 10  # un rappel n'est envoyé que dans les 10 min suivant l'heure prévue
DBLOCK = threading.Lock()

DEFAULT_CFG = {
    "wake": "07:30",
    "start": "10:00",
    "end": "18:00",
    "every": 2,
    "bed": "22:30",
    "workdays": [0, 1, 2, 3, 4],  # 0 = lundi
    "force_days": [0, 2, 5],
    "force_time": "18:30",
    "notify": True,
    "relance": 15,  # minutes ; 0 = pas de relance
}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
SLOT_RE = re.compile(r"^(reveil|coucher|force|p([01]\d|2[0-3]):[0-5]\d)$")

MESSAGES = {
    "reveil": ("Routine du réveil", "5 minutes au bord du lit : cou, épaules, dos, psoas."),
    "pause": ("Pause mouvement", "Lève-toi 5 minutes : menton rentré, pectoral, hanches, moyen fessier."),
    "force": ("Séance de force", "20 minutes : moyen fessier et haut du dos."),
    "coucher": ("Routine du coucher", "5 minutes pour relâcher le cou, les fessiers et les hanches."),
}


# ---------------------------------------------------------------- base de données
def run(sql: str, args: tuple = ()) -> list:
    with DBLOCK:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            rows = conn.execute(sql, args).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    run("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    run("CREATE TABLE IF NOT EXISTS log (day TEXT, slot TEXT, at TEXT, PRIMARY KEY (day, slot))")
    run("CREATE TABLE IF NOT EXISTS sent (day TEXT, slot TEXT, stage INTEGER, at TEXT, PRIMARY KEY (day, slot, stage))")


# ---------------------------------------------------------------- configuration utilisateur
def clean_cfg(raw: dict, base: dict) -> dict:
    cfg = dict(base)
    for k in ("wake", "start", "end", "bed", "force_time"):
        v = raw.get(k)
        if isinstance(v, str) and TIME_RE.match(v):
            cfg[k] = v
    if type(raw.get("every")) is int and raw["every"] in (1, 2, 3):
        cfg["every"] = raw["every"]
    if type(raw.get("relance")) is int and raw["relance"] in (0, 5, 10, 15, 30, 45, 60):
        cfg["relance"] = raw["relance"]
    if isinstance(raw.get("notify"), bool):
        cfg["notify"] = raw["notify"]
    for k in ("workdays", "force_days"):
        v = raw.get(k)
        if isinstance(v, list) and all(type(i) is int and 0 <= i <= 6 for i in v):
            cfg[k] = sorted(set(v))
    return cfg


def get_cfg() -> dict:
    rows = run("SELECT v FROM kv WHERE k='cfg'")
    if not rows:
        return dict(DEFAULT_CFG)
    try:
        return clean_cfg(json.loads(rows[0][0]), DEFAULT_CFG)
    except ValueError:
        return dict(DEFAULT_CFG)


def put_cfg(raw: dict) -> dict:
    cfg = clean_cfg(raw, get_cfg())
    run("INSERT INTO kv(k,v) VALUES('cfg',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (json.dumps(cfg),))
    return cfg


# ---------------------------------------------------------------- créneaux
def to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def slots_for(d: date, cfg: dict) -> list[dict]:
    """Même logique que la page : réveil, pauses (jours travaillés), force, coucher."""
    dow = d.weekday()
    out = [{"id": "reveil", "time": cfg["wake"], "kind": "reveil"}]
    if dow in cfg["workdays"]:
        m, end, step = to_min(cfg["start"]), to_min(cfg["end"]), cfg["every"] * 60
        while m <= end:
            out.append({"id": "p" + hhmm(m), "time": hhmm(m), "kind": "pause"})
            m += step
    if dow in cfg["force_days"]:
        out.append({"id": "force", "time": cfg["force_time"], "kind": "force"})
    out.append({"id": "coucher", "time": cfg["bed"], "kind": "coucher"})
    out.sort(key=lambda s: to_min(s["time"]))
    return out


def done_slots(day: str) -> set[str]:
    return {r[0] for r in run("SELECT slot FROM log WHERE day=?", (day,))}


def set_done(day: str, slot: str, done: bool) -> None:
    if done:
        run("INSERT OR IGNORE INTO log(day,slot,at) VALUES(?,?,?)",
            (day, slot, datetime.now(TZ).isoformat(timespec="seconds")))
    else:
        run("DELETE FROM log WHERE day=? AND slot=?", (day, slot))


# ---------------------------------------------------------------- notifications (ntfy)
def sign(day: str, slot: str) -> str:
    return hmac.new(TOKEN.encode(), f"{day}|{slot}".encode(), hashlib.sha256).hexdigest()[:32]


def ntfy_send(title: str, message: str, day: str | None = None, slot: str | None = None,
              tags: list[str] | None = None) -> tuple[bool, str]:
    if not NTFY_TOPIC:
        return False, "NTFY_TOPIC n'est pas défini"
    payload: dict = {"topic": NTFY_TOPIC, "title": title, "message": message,
                     "tags": tags or ["man_in_lotus_position"], "priority": 3}
    if PUBLIC_URL and slot and day:
        payload["click"] = f"{PUBLIC_URL}/?start={slot}"
        payload["actions"] = [
            {"action": "view", "label": "Ouvrir", "url": f"{PUBLIC_URL}/?start={slot}"},
            {"action": "http", "label": "Fait", "method": "POST", "clear": True,
             "url": f"{PUBLIC_URL}/api/quick?day={day}&slot={slot}&sig={sign(day, slot)}"},
        ]
    headers = {"Content-Type": "application/json"}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    req = urllib.request.Request(NTFY_URL, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return (200 <= r.status < 300), f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # réseau, DNS, timeout…
        return False, str(e)


def was_sent(day: str, slot: str, stage: int) -> bool:
    return bool(run("SELECT 1 FROM sent WHERE day=? AND slot=? AND stage=?", (day, slot, stage)))


def mark_sent(day: str, slot: str, stage: int) -> None:
    run("INSERT OR IGNORE INTO sent(day,slot,stage,at) VALUES(?,?,?,?)",
        (day, slot, stage, datetime.now(TZ).isoformat(timespec="seconds")))


def tick(now: datetime | None = None) -> int:
    """Envoie les rappels dus. Retourne le nombre de notifications envoyées."""
    cfg = get_cfg()
    if not cfg["notify"] or not NTFY_TOPIC:
        return 0
    now = now or datetime.now(TZ)
    day = now.date().isoformat()
    done = done_slots(day)
    sent = 0
    for s in slots_for(now.date(), cfg):
        if s["id"] in done:
            continue
        minutes = now.hour * 60 + now.minute - to_min(s["time"])
        title, body = MESSAGES[s["kind"]]
        stage = None
        if 0 <= minutes < WINDOW_MIN and not was_sent(day, s["id"], 0):
            stage = 0
        elif cfg["relance"] and cfg["relance"] <= minutes < cfg["relance"] + WINDOW_MIN \
                and was_sent(day, s["id"], 0) and not was_sent(day, s["id"], 1):
            stage, title, body = 1, "Pas encore fait ? " + title, "Deux minutes comptent déjà. Lance-toi."
        if stage is None:
            continue
        ok, info = ntfy_send(f"{title} · {s['time']}", body, day, s["id"])
        if ok:
            mark_sent(day, s["id"], stage)
            sent += 1
            LOG.info("notification envoyée : %s %s (étape %d)", day, s["id"], stage)
        else:
            LOG.warning("échec ntfy : %s", info)
    return sent


def scheduler() -> None:
    while True:
        try:
            tick()
        except Exception:
            LOG.exception("erreur du planificateur")
        time.sleep(20)


# ---------------------------------------------------------------- HTTP
FAILS: dict[str, list[float]] = {}
FAILS_LOCK = threading.Lock()
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".webmanifest": "application/manifest+json", ".png": "image/png", ".svg": "image/svg+xml"}
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'")


class Handler(BaseHTTPRequestHandler):
    server_version = "Mobilite/1.0"

    def log_message(self, fmt, *args):
        LOG.info("%s %s", self.address_string(), fmt % args)

    # -- utilitaires
    def ip(self) -> str:
        if TRUST_PROXY:
            xff = self.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[0].strip()
        return self.client_address[0]

    def throttled(self) -> bool:
        with FAILS_LOCK:
            now = time.time()
            recent = [t for t in FAILS.get(self.ip(), []) if now - t < 600]
            FAILS[self.ip()] = recent
            return len(recent) >= 10

    def fail(self) -> None:
        with FAILS_LOCK:
            FAILS.setdefault(self.ip(), []).append(time.time())

    def send_json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def authed(self) -> bool:
        h = self.headers.get("Authorization", "")
        return h.startswith("Bearer ") and hmac.compare_digest(h[7:].encode(), TOKEN.encode())

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 16384:
            raise ValueError("corps trop volumineux")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("objet JSON attendu")
        return data

    # -- routage
    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def route(self, method: str) -> None:
        u = urlparse(self.path)
        try:
            if u.path.startswith("/api/"):
                self.api(method, u.path, parse_qs(u.query))
            elif method == "GET":
                self.static(u.path)
            else:
                self.send_json(405, {"error": "méthode non autorisée"})
        except ValueError as e:
            self.send_json(400, {"error": str(e)})
        except Exception:
            LOG.exception("erreur serveur")
            self.send_json(500, {"error": "erreur serveur"})

    def api(self, method: str, path: str, q: dict) -> None:
        if path == "/api/health" and method == "GET":
            return self.send_json(200, {"ok": True})
        if path == "/api/quick" and method == "POST":
            return self.quick(q)
        if self.throttled():
            return self.send_json(429, {"error": "trop de tentatives, réessaie plus tard"})
        if not self.authed():
            self.fail()
            return self.send_json(401, {"error": "jeton invalide"})

        if path == "/api/state" and method == "GET":
            days = max(1, min(int(q.get("days", ["90"])[0]), 400))
            since = date.fromordinal(datetime.now(TZ).date().toordinal() - days).isoformat()
            log: dict[str, dict] = {}
            for day, slot in run("SELECT day, slot FROM log WHERE day>=?", (since,)):
                log.setdefault(day, {})[slot] = True
            return self.send_json(200, {"cfg": get_cfg(), "log": log,
                                        "meta": {"notify_enabled": bool(NTFY_TOPIC)}})
        if path == "/api/cfg" and method == "PUT":
            return self.send_json(200, put_cfg(self.body()))
        if path == "/api/log" and method == "POST":
            b = self.body()
            day, slot, done = b.get("day"), b.get("slot"), b.get("done")
            if not isinstance(day, str) or not isinstance(slot, str) or not isinstance(done, bool):
                raise ValueError("day, slot, done requis")
            delta = (datetime.now(TZ).date() - date.fromisoformat(day)).days
            if not SLOT_RE.match(slot) or delta < -1 or delta > 400:
                raise ValueError("créneau ou date invalide")
            set_done(day, slot, done)
            return self.send_json(200, {"ok": True})
        if path == "/api/test-notify" and method == "POST":
            ok, info = ntfy_send("Test des rappels", "Si tu lis ceci, les notifications fonctionnent.")
            return self.send_json(200 if ok else 502, {"ok": ok, "info": info})
        self.send_json(404, {"error": "introuvable"})

    def quick(self, q: dict) -> None:
        """Bouton « Fait » des notifications : lien signé, ne permet que de cocher ce créneau."""
        if self.throttled():
            return self.send_json(429, {"error": "trop de tentatives"})
        day, slot, sig = (q.get(k, [""])[0] for k in ("day", "slot", "sig"))
        if not SLOT_RE.match(slot) or not hmac.compare_digest(sig.encode(), sign(day, slot).encode()):
            self.fail()
            return self.send_json(403, {"error": "lien invalide"})
        delta = (datetime.now(TZ).date() - date.fromisoformat(day)).days
        if delta < 0 or delta > 1:
            return self.send_json(410, {"error": "lien expiré"})
        set_done(day, slot, True)
        self.send_json(200, {"ok": True})

    def static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        f = (STATIC / rel).resolve()
        if STATIC.resolve() not in f.parents or not f.is_file():
            self.send_response(404)
            self.end_headers()
            return
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(f.suffix, mimetypes.guess_type(f.name)[0] or "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache" if f.suffix in (".html", ".js") else "public, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if f.suffix == ".html":
            self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(TOKEN) < 16:
        sys.exit("MOB_TOKEN manquant ou trop court (16 caractères minimum).\n"
                 "Génère-en un : python3 -c \"import secrets; print(secrets.token_urlsafe(24))\"")
    init_db()
    threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    LOG.info("écoute sur http://%s:%d (fuseau %s, ntfy %s)", HOST, PORT, TZ,
             f"{NTFY_URL}/{NTFY_TOPIC}" if NTFY_TOPIC else "désactivé")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
