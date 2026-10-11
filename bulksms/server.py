"""The web page and its JSON API. LAN only, password required."""

import hashlib
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as configuration
from . import csvin, lookup, template
from .phone import to_e164
from .sender import Busy, Sender
from .store import Store

STATIC = os.path.join(os.path.dirname(__file__), "static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
LOCAL_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10",
)]
SESSION_DAYS = 30
MAX_BODY = 5 * 1024 * 1024
PAGE = 100

FILTERS = {
    "todo": ("ready", "sending"),
    "sent": ("sent", "dry"),
    "problem": ("failed", "uncertain", "invalid", "incomplete", "unchecked"),
    "skipped": ("known", "blocked", "texted", "duplicate"),
}


def is_local(address):
    """Loopback or a private network — never the mobile-data side of the phone."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in LOCAL_NETS)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


class App:
    def __init__(self, cfg, send=None):
        self.cfg = cfg
        os.makedirs(cfg["data_dir"], exist_ok=True)
        self.store = Store(os.path.join(cfg["data_dir"], "bulksms.sqlite3"),
                           cfg["country_code"], lookup.enabled(cfg), bool(cfg["dry_run"]))
        self.store.recover()
        self.sender = Sender(self.store, cfg, send=send)
        self.fails = {}
        self.fail_lock = threading.Lock()

    # -- checking ----------------------------------------------------------

    def check(self, cid, must=False):
        """Classify a campaign, asking the lookup first. Returns an error text or ""."""
        problem = ""
        if lookup.enabled(self.cfg):
            phones = self.store.phones_to_look_up(cid)
            try:
                if phones:
                    self.store.store_lookup(cid, lookup.check(self.cfg, phones))
            except lookup.LookupFailed as error:
                problem = str(error)
                if must:
                    raise ApiError(502, f"Not started: {problem}.")
        self.store.classify(cid)
        return problem

    def describe(self, cid, flt="", page=0):
        c = self.store.campaign(cid)
        if c is None:
            raise ApiError(404, "No such campaign.")
        rows, total = self.store.recipients(cid, FILTERS.get(flt), PAGE, page * PAGE)
        counts = self.store.counts(cid)
        sample = self.store.one("SELECT * FROM recipients WHERE campaign_id=? ORDER BY (status='ready') DESC,"
                                " position LIMIT 1", (cid,))
        vals = template.values(sample["name"], json.loads(sample["fields"]), c["first_name_only"]) if sample \
            else {"name": "Anna"}
        text = template.render(c["template"], vals)
        chars, parts, encoding = template.segments(text)
        running = self.sender.running_id() == cid
        return {
            "campaign": c,
            "counts": counts,
            "recipients": rows,
            "total": total,
            "page": page,
            "page_size": PAGE,
            "events": self.store.events(cid),
            "running": running,
            "next_at": self.sender.next_at if running else None,
            "preview": {"text": text, "chars": chars, "parts": parts, "encoding": encoding,
                        "to": sample["name"] if sample else ""},
            "unknown_placeholders": template.unknown_keys(c["template"], self.store.known_keys(cid)),
            "placeholders": sorted(self.store.known_keys(cid) | {"name"}),
        }

    def campaign_input(self, body, partial=False):
        out = {}

        def number(key, low, high, default):
            if key in body:
                try:
                    value = float(str(body[key]).replace(",", "."))
                except ValueError:
                    raise ApiError(422, f"{key} must be a number.") from None
                if not low <= value <= high:
                    raise ApiError(422, f"{key} must be between {low} and {high}.")
                out[key] = value
            elif not partial:
                out[key] = default

        for key, limit in (("name", 120), ("template", 2000)):
            if key in body or not partial:
                value = str(body.get(key, "")).strip()
                if not value:
                    raise ApiError(422, f"{key} is required.")
                out[key] = value[:limit]
        number("delay", 1, 3600, float(self.cfg["default_delay"]))
        number("jitter", 0, 600, float(self.cfg["default_jitter"]))
        for key, default in (("window_start", self.cfg["window_start"]), ("window_end", self.cfg["window_end"])):
            if key in body or not partial:
                value = str(body.get(key, default) or "").strip()
                if value and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
                    raise ApiError(422, f"{key} must look like 08:00.")
                out[key] = value
        if "start_at" in body or not partial:
            value = str(body.get("start_at", "") or "").strip()
            if value and not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?", value):
                raise ApiError(422, "start_at must be a date and time.")
            out["start_at"] = value
        for key in ("first_name_only", "allow_repeat"):
            if key in body or not partial:
                out[key] = 1 if body.get(key) else 0
        return out

    # -- login throttle ----------------------------------------------------

    def locked_for(self, ip):
        with self.fail_lock:
            count, until = self.fails.get(ip, (0, 0))
            return max(0, int(until - time.time()))

    def login_failed(self, ip):
        with self.fail_lock:
            count = self.fails.get(ip, (0, 0))[0] + 1
            wait = 0 if count < 5 else min(900, 30 * 2 ** (count - 5))
            self.fails[ip] = (count, time.time() + wait)

    def login_ok(self, ip):
        with self.fail_lock:
            self.fails.pop(ip, None)


class Handler(BaseHTTPRequestHandler):
    server_version = "bulksms"
    protocol_version = "HTTP/1.1"
    app = None

    def log_message(self, fmt, *args):  # quiet: the page polls every two seconds
        pass

    # -- plumbing ----------------------------------------------------------

    def _send(self, status, body, ctype="application/json; charset=utf-8", headers=()):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self';"
                         " img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _token_hash(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == "sid" and value:
                return hashlib.sha256(value.encode()).hexdigest()
        return None

    def _authed(self):
        token = self._token_hash()
        return token is not None and self.app.store.has_session(token)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "Too much data in one request.")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "The request was not valid JSON.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "The request was not a JSON object.")
        return data

    def _same_origin(self):
        """A custom header cannot be sent across origins without a preflight we never answer."""
        if self.headers.get("X-Requested-With") != "bulksms":
            return False
        origin = self.headers.get("Origin")
        if origin:
            return urllib.parse.urlsplit(origin).netloc == (self.headers.get("Host") or "")
        return True

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        try:
            if not is_local(self.client_address[0]):
                self._send(403, {"error": "This page only answers on the local network."})
                return
            url = urllib.parse.urlsplit(self.path)
            query = dict(urllib.parse.parse_qsl(url.query))
            if method == "GET" and url.path in STATIC_FILES:
                name, ctype = STATIC_FILES[url.path]
                with open(os.path.join(STATIC, name), "rb") as fh:
                    self._send(200, fh.read(), ctype)
                return
            if method == "GET" and url.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            if not url.path.startswith("/api/"):
                raise ApiError(404, "Not found.")
            if method == "POST" and not self._same_origin():
                raise ApiError(403, "The request came from another site.")
            body = self._body() if method == "POST" else {}
            self._api(method, url.path[4:], query, body)
        except ApiError as error:
            self._send(error.status, {"error": error.message})
        except Busy:
            self._send(409, {"error": "Another campaign is running. Pause it first."})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:  # noqa: BLE001 — the page must get an answer
            self._send(500, {"error": f"Internal error: {type(error).__name__}: {error}"})

    # -- routes ------------------------------------------------------------

    def _api(self, method, path, query, body):
        app, store, cfg = self.app, self.app.store, self.app.cfg
        ip = self.client_address[0]

        if path == "/login" and method == "POST":
            if not cfg["password_hash"]:
                raise ApiError(503, "No password is set. Run: python -m bulksms set-password")
            wait = app.locked_for(ip)
            if wait:
                raise ApiError(429, f"Too many attempts. Try again in {wait} s.")
            if not configuration.check_password(str(body.get("password", "")), cfg["password_hash"]):
                app.login_failed(ip)
                raise ApiError(401, "Wrong password.")
            app.login_ok(ip)
            token = secrets.token_urlsafe(32)
            store.add_session(hashlib.sha256(token.encode()).hexdigest(), time.time() + SESSION_DAYS * 86400)
            self._send(200, {"ok": True}, headers=[(
                "Set-Cookie", f"sid={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}")])
            return

        if not self._authed():
            raise ApiError(401, "Log in first.")

        if path == "/logout" and method == "POST":
            store.drop_session(self._token_hash())
            self._send(200, {"ok": True}, headers=[("Set-Cookie", "sid=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")])
            return

        if path == "/state" and method == "GET":
            self._send(200, {
                "title": cfg["title"],
                "dry_run": bool(cfg["dry_run"]),
                "lookup": {"enabled": lookup.enabled(cfg), "label": cfg["lookup"]["label"]},
                "defaults": {"delay": cfg["default_delay"], "jitter": cfg["default_jitter"],
                             "window_start": cfg["window_start"], "window_end": cfg["window_end"]},
                "running_id": app.sender.running_id(),
                "campaigns": store.campaigns(),
            })
            return

        if path == "/campaigns" and method == "POST":
            data = app.campaign_input(body)
            parsed = csvin.parse(str(body.get("csv", "")), cfg["country_code"])
            if not parsed:
                raise ApiError(422, "Paste at least one row with a phone number.")
            cid = store.create_campaign(data)
            store.add_recipients(cid, parsed)
            problem = app.check(cid)
            store.event(cid, f"Created with {len(parsed)} rows.")
            self._send(200, {"id": cid, "lookup_problem": problem})
            return

        match = re.fullmatch(r"/campaigns/(\d+)(?:/([a-z-]+))?", path)
        if match:
            cid, action = int(match.group(1)), match.group(2)
            c = store.campaign(cid)
            if c is None:
                raise ApiError(404, "No such campaign.")
            running = app.sender.running_id() == cid
            if method == "GET" and action is None:
                try:
                    page = max(0, int(query.get("page", 0)))
                except ValueError:
                    page = 0
                self._send(200, app.describe(cid, query.get("filter", ""), page))
                return
            if method != "POST":
                raise ApiError(405, "Use POST.")
            problem = ""
            if action is None:
                data = app.campaign_input(body, partial=True)
                if running and "template" in data and data["template"] != c["template"]:
                    raise ApiError(409, "Pause the campaign before changing the message.")
                store.update_campaign(cid, data)
                store.classify(cid)
            elif action == "add":
                parsed = csvin.parse(str(body.get("csv", "")), cfg["country_code"])
                if not parsed:
                    raise ApiError(422, "Paste at least one row with a phone number.")
                store.add_recipients(cid, parsed)
                problem = app.check(cid)
                store.event(cid, f"Added {len(parsed)} rows.")
                if c["status"] == "done":
                    store.set_status(cid, "paused", "New rows were added.")
            elif action == "check":
                problem = app.check(cid)
            elif action == "start":
                if running:
                    raise ApiError(409, "Already running.")
                if app.sender.running_id() is not None:
                    raise Busy()
                # Never start on an old answer: ask again, and refuse if nobody answers.
                app.check(cid, must=True)
                if not store.has_ready(cid):
                    raise ApiError(422, "Nobody is ready to be sent to.")
                app.sender.start(cid)
            elif action == "pause":
                app.sender.pause(cid, "Paused by you.")
            elif action == "retry-failed":
                store.retry_failed(cid)
                problem = app.check(cid)
            elif action == "delete":
                if running:
                    raise ApiError(409, "Pause the campaign before deleting it.")
                store.delete_campaign(cid)
                self._send(200, {"ok": True})
                return
            else:
                raise ApiError(404, "Not found.")
            self._send(200, {"ok": True, "lookup_problem": problem})
            return

        match = re.fullmatch(r"/recipients/(\d+)(?:/([a-z-]+))?", path)
        if match and method == "POST":
            rid, action = int(match.group(1)), match.group(2)
            r = store.recipient(rid)
            if r is None:
                raise ApiError(404, "No such row.")
            cid = r["campaign_id"]
            if r["status"] == "sending":
                raise ApiError(409, "This one is being sent right now.")
            problem = ""
            if action is None:
                if r["status"] in ("sent", "dry"):
                    raise ApiError(409, "Already sent; it cannot be edited.")
                store.edit_recipient(rid, str(body.get("name", "")).strip()[:120],
                                     str(body.get("phone", "")).strip()[:40])
                problem = app.check(cid)
            elif action == "delete":
                if r["status"] in ("sent", "dry"):
                    raise ApiError(409, "Already sent; the row is kept as the record of it.")
                store.delete_recipient(rid)
                store.classify(cid)
            elif action == "block":
                if not r["phone"]:
                    raise ApiError(422, "There is no valid number to block.")
                store.block(r["phone"], str(body.get("note", "") or r["name"])[:200])
                store.classify(cid)
            elif action == "resolve":
                if r["status"] != "uncertain":
                    raise ApiError(409, "Only an uncertain row can be settled.")
                if body.get("sent"):
                    c = store.campaign(cid)
                    vals = template.values(r["name"], json.loads(r["fields"]), c["first_name_only"])
                    store.mark_sent(r, template.render(c["template"], vals), "Marked as sent by you.")
                else:
                    store.mark(rid, "unchecked", "")
                    problem = app.check(cid)
            else:
                raise ApiError(404, "Not found.")
            self._send(200, {"ok": True, "lookup_problem": problem})
            return

        if path == "/blocklist" and method == "GET":
            self._send(200, {"blocklist": store.blocklist()})
            return
        if path == "/blocklist" and method == "POST":
            added, bad = 0, []
            for line in str(body.get("phones", "")).splitlines():
                if not line.strip():
                    continue
                phone = to_e164(line, cfg["country_code"])
                if phone:
                    store.block(phone, str(body.get("note", ""))[:200])
                    added += 1
                else:
                    bad.append(line.strip()[:40])
            if not added and not bad:
                raise ApiError(422, "Type at least one number.")
            self._send(200, {"added": added, "invalid": bad})
            return
        if path == "/blocklist/delete" and method == "POST":
            store.unblock(str(body.get("phone", "")))
            self._send(200, {"ok": True})
            return
        if path == "/history" and method == "GET":
            self._send(200, {"history": store.history(query.get("q", "")[:60])})
            return

        raise ApiError(404, "Not found.")


def serve(cfg):
    app = App(cfg)
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((cfg["host"], int(cfg["port"])), handler)
    httpd.daemon_threads = True
    print(f"bulksms listening on {cfg['host']}:{cfg['port']}"
          + ("  (DRY RUN: nothing is really sent)" if cfg["dry_run"] else ""), flush=True)
    if not cfg["password_hash"]:
        print("No password is set — nobody can log in. Run: python -m bulksms set-password", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        running = app.sender.running_id()
        if running:
            app.sender.pause(running, "Paused: the app was stopped.")
            app.sender.join(5)
