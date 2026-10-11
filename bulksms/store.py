"""SQLite state: campaigns, recipients, block list, history.

Everything the sender does is written here before and after it happens, so a
killed process can never lead to the same person being texted twice.
"""

import contextlib
import json
import sqlite3
import threading
import time

from . import template
from .phone import to_e164

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    template TEXT NOT NULL DEFAULT '',
    delay REAL NOT NULL DEFAULT 20,
    jitter REAL NOT NULL DEFAULT 0,
    start_at TEXT NOT NULL DEFAULT '',
    window_start TEXT NOT NULL DEFAULT '',
    window_end TEXT NOT NULL DEFAULT '',
    first_name_only INTEGER NOT NULL DEFAULT 0,
    allow_repeat INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    note TEXT NOT NULL DEFAULT '',
    checked_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recipients (
    id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    phone_raw TEXT NOT NULL DEFAULT '',
    phone TEXT,
    fields TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'unchecked',
    detail TEXT NOT NULL DEFAULT '',
    lookup TEXT,
    lookup_phone TEXT,
    sent_at REAL
);
CREATE INDEX IF NOT EXISTS idx_rec_campaign ON recipients (campaign_id, status, position);
CREATE TABLE IF NOT EXISTS blocklist (
    phone TEXT PRIMARY KEY,
    note TEXT NOT NULL DEFAULT '',
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sent_log (
    id INTEGER PRIMARY KEY,
    phone TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    campaign_id INTEGER,
    campaign_name TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    sent_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sent_phone ON sent_log (phone);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL,
    at REAL NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    expires REAL NOT NULL
);
"""

# Statuses the classifier never touches: something already happened to the row.
# "dry" is a row a rehearsal went through: settled for as long as the app is in
# dry-run mode, and handed back to the queue the moment it is started live.
SETTLED = ("sent", "dry", "sending", "uncertain", "failed")

CAMPAIGN_FIELDS = ("name", "template", "delay", "jitter", "start_at", "window_start",
                   "window_end", "first_name_only", "allow_repeat")


class Store:
    def __init__(self, file, country_code="46", lookup_enabled=False, dry_run=False):
        self.dry_run = dry_run
        self.country_code = country_code
        self.lookup_enabled = lookup_enabled
        self.lock = threading.RLock()
        self.db = sqlite3.connect(file, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self._depth = 0

    # -- plumbing ----------------------------------------------------------

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            outer = self._depth == 0
            if outer:
                self.db.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer:
                    self.db.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self.db.execute("COMMIT")

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    # -- start-up ----------------------------------------------------------

    def recover(self):
        """After a crash: nothing resumes on its own, and nothing is resent.

        A row caught in "sending" may or may not have left the phone. It is
        marked uncertain and left for a human to settle.
        """
        with self.tx():
            self.x("UPDATE recipients SET status='uncertain', "
                   "detail='The app stopped while this was being sent. Check the phone.' "
                   "WHERE status='sending'")
            for c in self.q("SELECT id FROM campaigns WHERE status='running'"):
                self.x("UPDATE campaigns SET status='paused', note=? WHERE id=?",
                       ("Paused: the app was restarted.", c["id"]))
                self.event(c["id"], "The app was restarted; the campaign is paused.")
            # Rows written by a version that recorded a rehearsal as "sent".
            self.x("UPDATE recipients SET status='dry' WHERE status='sent' AND detail LIKE 'Dry run%'"
                   " AND NOT EXISTS (SELECT 1 FROM sent_log s WHERE s.phone = recipients.phone"
                   " AND s.campaign_id = recipients.campaign_id)")
            if not self.dry_run:
                self.release_rehearsed()

    def release_rehearsed(self):
        """Live mode: whatever a rehearsal "sent" is put back in the queue."""
        with self.tx():
            touched = self.q("SELECT DISTINCT campaign_id AS id FROM recipients WHERE status='dry'")
            self.x("UPDATE recipients SET status='unchecked', detail='', sent_at=NULL WHERE status='dry'")
            for c in touched:
                self.classify(c["id"])
                self.x("UPDATE campaigns SET status='paused', note=? WHERE id=? AND status IN ('done','paused')",
                       ("The rehearsal is over: everyone it covered is back in the queue.", c["id"]))
                self.event(c["id"], "Now live: the rows from the dry run are back in the queue.")

    # -- campaigns ---------------------------------------------------------

    def campaigns(self):
        rows = self.q("SELECT * FROM campaigns ORDER BY id DESC")
        for c in rows:
            c["counts"] = self.counts(c["id"])
        return rows

    def campaign(self, cid):
        return self.one("SELECT * FROM campaigns WHERE id=?", (cid,))

    def create_campaign(self, data):
        with self.tx():
            cur = self.x(
                "INSERT INTO campaigns (name, template, delay, jitter, start_at, window_start, window_end,"
                " first_name_only, allow_repeat, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (data["name"], data["template"], data["delay"], data["jitter"], data["start_at"],
                 data["window_start"], data["window_end"], int(data["first_name_only"]),
                 int(data["allow_repeat"]), time.time()))
            return cur.lastrowid

    def update_campaign(self, cid, data):
        sets = [k for k in CAMPAIGN_FIELDS if k in data]
        if sets:
            self.x("UPDATE campaigns SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?",
                   [data[k] for k in sets] + [cid])

    def set_status(self, cid, status, note=""):
        self.x("UPDATE campaigns SET status=?, note=? WHERE id=?", (status, note, cid))

    def set_note(self, cid, note):
        self.x("UPDATE campaigns SET note=? WHERE id=? AND note<>?", (note, cid, note))

    def delete_campaign(self, cid):
        with self.tx():
            self.x("DELETE FROM events WHERE campaign_id=?", (cid,))
            self.x("DELETE FROM campaigns WHERE id=?", (cid,))

    def counts(self, cid):
        rows = self.q("SELECT status, COUNT(*) AS n FROM recipients WHERE campaign_id=? GROUP BY status", (cid,))
        return {r["status"]: r["n"] for r in rows}

    def event(self, cid, message):
        self.x("INSERT INTO events (campaign_id, at, message) VALUES (?,?,?)", (cid, time.time(), message))

    def events(self, cid, limit=60):
        return self.q("SELECT at, message FROM events WHERE campaign_id=? ORDER BY id DESC LIMIT ?", (cid, limit))

    # -- recipients --------------------------------------------------------

    def add_recipients(self, cid, parsed):
        with self.tx():
            start = self.one("SELECT COALESCE(MAX(position), 0) AS p FROM recipients WHERE campaign_id=?", (cid,))["p"]
            for i, row in enumerate(parsed, 1):
                self.x("INSERT INTO recipients (campaign_id, position, name, phone_raw, phone, fields)"
                       " VALUES (?,?,?,?,?,?)",
                       (cid, start + i, row["name"], row["phone_raw"],
                        to_e164(row["phone_raw"], self.country_code),
                        json.dumps(row.get("fields") or {}, ensure_ascii=False)))

    def recipient(self, rid):
        return self.one("SELECT * FROM recipients WHERE id=?", (rid,))

    def recipients(self, cid, statuses=None, limit=100, offset=0):
        where, args = "campaign_id=?", [cid]
        if statuses:
            where += " AND status IN (%s)" % ",".join("?" * len(statuses))
            args += list(statuses)
        total = self.one(f"SELECT COUNT(*) AS n FROM recipients WHERE {where}", args)["n"]
        rows = self.q(f"SELECT id, position, name, phone_raw, phone, status, detail, sent_at FROM recipients"
                      f" WHERE {where} ORDER BY position, id LIMIT ? OFFSET ?", args + [limit, offset])
        return rows, total

    def edit_recipient(self, rid, name, phone_raw):
        self.x("UPDATE recipients SET name=?, phone_raw=?, phone=?, status='unchecked', detail=''"
               " WHERE id=? AND status NOT IN ('sent','dry','sending')",
               (name, phone_raw, to_e164(phone_raw, self.country_code), rid))

    def delete_recipient(self, rid):
        self.x("DELETE FROM recipients WHERE id=? AND status NOT IN ('sent','dry','sending')", (rid,))

    def retry_failed(self, cid):
        self.x("UPDATE recipients SET status='unchecked', detail='' WHERE campaign_id=? AND status='failed'", (cid,))

    def phones_to_look_up(self, cid):
        rows = self.q("SELECT DISTINCT phone FROM recipients WHERE campaign_id=? AND phone IS NOT NULL"
                      " AND status NOT IN ('sent','dry','sending','uncertain','failed')", (cid,))
        return [r["phone"] for r in rows]

    def store_lookup(self, cid, answers):
        with self.tx():
            for phone, answer in answers.items():
                self.x("UPDATE recipients SET lookup=?, lookup_phone=? WHERE campaign_id=? AND phone=?",
                       (answer, phone, cid, phone))
            self.x("UPDATE campaigns SET checked_at=? WHERE id=?", (time.time(), cid))

    def known_keys(self, cid):
        keys = set()
        for r in self.q("SELECT DISTINCT fields FROM recipients WHERE campaign_id=?", (cid,)):
            keys.update(k.strip().lower() for k in json.loads(r["fields"]))
        return keys

    def classify(self, cid):
        """Decide, for every row nothing has happened to yet, whether it may be sent.

        Only "ready" is ever sent, and with a lookup configured a row is ready
        only if the lookup answered "unknown" for the number it has now.
        """
        with self.tx():
            c = self.campaign(cid)
            if c is None:
                return
            blocked = {r["phone"] for r in self.q("SELECT phone FROM blocklist")}
            texted = {r["phone"] for r in self.q("SELECT DISTINCT phone FROM sent_log")}
            seen = set()
            for r in self.q("SELECT * FROM recipients WHERE campaign_id=? ORDER BY position, id", (cid,)):
                phone = r["phone"]
                if r["status"] in SETTLED:
                    if phone:
                        seen.add(phone)
                    continue
                detail = ""
                fresh = r["lookup"] if r["lookup_phone"] == phone else None
                if not phone:
                    status = "invalid"
                elif phone in seen:
                    status = "duplicate"
                elif phone in blocked:
                    status = "blocked"
                elif phone in texted and not c["allow_repeat"]:
                    status = "texted"
                elif self.lookup_enabled and fresh is None:
                    status = "unchecked"
                elif self.lookup_enabled and fresh == "known":
                    status = "known"
                elif self.lookup_enabled and fresh == "invalid":
                    status, detail = "invalid", "Rejected by the lookup."
                else:
                    vals = template.values(r["name"], json.loads(r["fields"]), c["first_name_only"])
                    empty = template.empty_keys(c["template"], vals)
                    if empty:
                        status, detail = "incomplete", "Empty: " + ", ".join(f"[{k}]" for k in empty)
                    else:
                        status = "ready"
                if phone:
                    seen.add(phone)
                if status != r["status"] or detail != r["detail"]:
                    self.x("UPDATE recipients SET status=?, detail=? WHERE id=?", (status, detail, r["id"]))

    # -- sending -----------------------------------------------------------

    def claim_next(self, cid):
        """Take the next ready row and mark it "sending", in one transaction.

        The block list and the history are read again here: both can change
        while a campaign runs, and this is the last moment that matters.
        """
        with self.tx():
            c = self.campaign(cid)
            while True:
                r = self.one("SELECT * FROM recipients WHERE campaign_id=? AND status='ready'"
                             " ORDER BY position, id LIMIT 1", (cid,))
                if r is None:
                    return None
                if self.one("SELECT 1 AS x FROM blocklist WHERE phone=?", (r["phone"],)):
                    self.x("UPDATE recipients SET status='blocked' WHERE id=?", (r["id"],))
                    continue
                if not c["allow_repeat"] and self.one("SELECT 1 AS x FROM sent_log WHERE phone=?", (r["phone"],)):
                    self.x("UPDATE recipients SET status='texted' WHERE id=?", (r["id"],))
                    continue
                self.x("UPDATE recipients SET status='sending', detail='' WHERE id=?", (r["id"],))
                return r

    def mark_sent(self, r, text, detail="", log=True):
        with self.tx():
            now = time.time()
            c = self.campaign(r["campaign_id"]) or {"name": ""}
            self.x("UPDATE recipients SET status=?, detail=?, sent_at=? WHERE id=?",
                   ("sent" if log else "dry", detail, now, r["id"]))
            if log:
                self.x("INSERT INTO sent_log (phone, name, campaign_id, campaign_name, text, sent_at)"
                       " VALUES (?,?,?,?,?,?)", (r["phone"], r["name"], r["campaign_id"], c["name"], text, now))

    def mark(self, rid, status, detail=""):
        self.x("UPDATE recipients SET status=?, detail=? WHERE id=?", (status, detail, rid))

    def has_ready(self, cid):
        return self.one("SELECT 1 AS x FROM recipients WHERE campaign_id=? AND status='ready' LIMIT 1", (cid,)) is not None

    # -- block list and history -------------------------------------------

    def block(self, phone, note=""):
        self.x("INSERT INTO blocklist (phone, note, added_at) VALUES (?,?,?)"
               " ON CONFLICT(phone) DO UPDATE SET note=CASE WHEN excluded.note<>'' THEN excluded.note ELSE note END",
               (phone, note, time.time()))

    def unblock(self, phone):
        self.x("DELETE FROM blocklist WHERE phone=?", (phone,))

    def blocklist(self):
        return self.q("SELECT * FROM blocklist ORDER BY added_at DESC")

    def history(self, query="", limit=200):
        like = f"%{query}%"
        return self.q("SELECT phone, name, campaign_name, text, sent_at FROM sent_log"
                      " WHERE phone LIKE ? OR name LIKE ? ORDER BY id DESC LIMIT ?", (like, like, limit))

    # -- sessions ----------------------------------------------------------

    def add_session(self, token_hash, expires):
        self.x("DELETE FROM sessions WHERE expires < ?", (time.time(),))
        self.x("INSERT OR REPLACE INTO sessions (token_hash, expires) VALUES (?,?)", (token_hash, expires))

    def has_session(self, token_hash):
        return self.one("SELECT 1 AS x FROM sessions WHERE token_hash=? AND expires > ?",
                        (token_hash, time.time())) is not None

    def drop_session(self, token_hash):
        self.x("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
