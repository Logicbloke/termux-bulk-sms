"""The one worker that sends: any number of campaigns, one message at a time."""

import datetime
import json
import random
import subprocess
import threading
import time

from . import template


def termux_send(cfg):
    """Build send(phone, text) -> ("sent" | "dry" | "failed" | "uncertain", detail)."""

    def send(phone, text):
        if cfg.get("dry_run"):
            print(f"[dry run] to {phone}: {text!r}", flush=True)
            return "dry", "Dry run — nothing was sent."
        cmd = ["termux-sms-send", "-n", phone]
        if cfg.get("sim_slot") is not None:
            cmd += ["-s", str(cfg["sim_slot"])]
        try:
            # The text goes on stdin: as an argument, a message that begins
            # with "-" would be read as an option.
            done = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=90)
        except FileNotFoundError:
            return "failed", "termux-sms-send was not found (pkg install termux-api, and the Termux:API app)."
        except subprocess.TimeoutExpired:
            return "uncertain", "termux-sms-send did not answer within 90 s. Check the phone."
        output = (done.stdout + " " + done.stderr).strip()
        if done.returncode != 0 or "error" in output.lower() or "exception" in output.lower():
            return "failed", output[:300] or f"termux-sms-send exited with {done.returncode}"
        return "sent", ""

    return send


def _wake(lock):
    try:
        subprocess.run(["termux-wake-lock" if lock else "termux-wake-unlock"],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


class Sender:
    """One thread serves every campaign that is switched on.

    A fixed campaign finishes when its queue is empty. An ongoing one stays on
    and waits: whoever is added later is sent to, at the campaign's own pace.
    """

    IDLE = 0.5

    def __init__(self, store, cfg, send=None, sleep=time.sleep, now=datetime.datetime.now,
                 wake=_wake, clock=time.time):
        self.store, self.cfg = store, cfg
        self.send = send or termux_send(cfg)
        self.sleep, self.now, self.wake, self.clock = sleep, now, wake, clock
        self.lock = threading.Lock()
        self.thread = None
        self.alive = False
        self.quit = False
        self.pauses = {}     # campaign id -> (since, seconds): the delay after a round
        self.in_round = {}   # campaign id -> messages sent in the current round
        self.failures = {}   # campaign id -> failures in a row

    def start(self, cid):
        with self.lock:
            self.store.set_status(cid, "running")
            self.store.event(cid, "Started." + (" DRY RUN: nothing is really sent." if self.cfg.get("dry_run") else ""))
            for book in (self.pauses, self.in_round, self.failures):
                book.pop(cid, None)
            if not self.alive:
                self.alive, self.quit = True, False
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()

    def pause(self, cid, note="Paused."):
        c = self.store.campaign(cid)
        if c and c["status"] == "running":
            self.store.set_status(cid, "paused", note)
            self.store.event(cid, note)

    def shutdown(self, note):
        """The app is stopping: nothing may be left marked as running."""
        for c in self.store.q("SELECT id FROM campaigns WHERE status='running'"):
            self.pause(c["id"], note)
        self.quit = True
        self.join(5)

    def join(self, timeout=None):
        if self.thread:
            self.thread.join(timeout)

    def next_at(self, cid):
        """When this campaign sends next, if it is waiting out a delay."""
        c = self.store.campaign(cid)
        pause = self.pauses.get(cid)
        if not c or not pause:
            return None
        return pause[0] + self._ceiling(c, pause[1])

    # -- the loop ----------------------------------------------------------

    def _run(self):
        self.wake(True)
        try:
            while True:
                with self.lock:
                    running = self.store.q("SELECT * FROM campaigns WHERE status='running' ORDER BY id")
                    if not running or self.quit:
                        self.alive = False
                        return
                worked = False
                for c in running:
                    try:
                        worked = self._step(c) or worked
                    except Exception as error:  # noqa: BLE001 — one campaign must not take the rest down
                        self.store.set_status(c["id"], "paused", f"Paused: internal error ({type(error).__name__}: {error}).")
                        self.store.event(c["id"], f"Internal error: {type(error).__name__}: {error}")
                if not worked:
                    self.sleep(self.IDLE)
        finally:
            self.alive = False
            self.wake(False)

    def _step(self, c):
        """Do at most one thing for one campaign. True if something was sent or settled."""
        cid = c["id"]
        pause = self.pauses.get(cid)
        if pause and self.clock() < pause[0] + self._ceiling(c, pause[1]):
            return False
        self.pauses.pop(cid, None)
        wait, why = self._hold(c)
        if wait > 0:
            self.store.set_note(cid, why)
            return False

        r = self.store.claim_next(cid)
        if r is None:
            self.in_round.pop(cid, None)
            if c["kind"] == "ongoing":
                self.store.set_note(cid, "On: waiting for new recipients.")
                return False
            self.store.set_status(cid, "done")
            self.store.event(cid, "Finished: nobody left to send to.")
            return True
        self.store.set_note(cid, "")

        vals = template.values(r["name"], json.loads(r["fields"]), c["first_name_only"])
        text = template.render(c["template"], vals)
        outcome, detail = self.send(r["phone"], text)
        if outcome in ("sent", "dry"):
            # A rehearsal must not put anyone in the history: the real
            # run would then skip them all as "already texted".
            self.store.mark_sent(r, text, detail, log=outcome == "sent")
            self.failures[cid] = 0
        elif outcome == "uncertain":
            self.store.mark(r["id"], "uncertain", detail)
            self.store.set_status(cid, "paused", "Paused: one message may or may not have been sent.")
            self.store.event(cid, f"Uncertain result for {r['phone']}; paused.")
            return True
        else:
            self.store.mark(r["id"], "failed", detail)
            self.store.event(cid, f"Failed for {r['phone']}: {detail}")
            self.failures[cid] = self.failures.get(cid, 0) + 1
            if self.failures[cid] >= int(self.cfg.get("max_consecutive_failures", 3)):
                note = f"Paused after {self.failures[cid]} failures in a row."
                self.store.set_status(cid, "paused", note)
                self.store.event(cid, note)
                return True

        # "batch" messages go out back to back; the delay comes after the
        # round, not after each message. A fixed campaign that just emptied
        # its queue skips the delay and finishes on the next pass; an ongoing
        # one keeps it, so the next person added still waits their turn.
        if c["kind"] != "ongoing" and not self.store.has_ready(cid):
            return True
        self.in_round[cid] = self.in_round.get(cid, 0) + 1
        if self.in_round[cid] >= max(1, int(c["batch"])):
            self.in_round[cid] = 0
            self.pauses[cid] = (self.clock(), self._delay(c))
        return True

    def _delay(self, c):
        jitter = max(0.0, float(c["jitter"]))
        return max(1.0, float(c["delay"]) + random.uniform(-jitter, jitter))

    @staticmethod
    def _ceiling(c, seconds):
        """A delay shortened on the page applies to the wait already under way."""
        return min(seconds, max(1.0, float(c["delay"]) + max(0.0, float(c["jitter"]))))

    def _hold(self, c):
        """(seconds to wait, why) — a start time in the future, or outside the allowed hours."""
        now = self.now()
        if c["start_at"]:
            try:
                start = datetime.datetime.fromisoformat(c["start_at"])
                if now < start:
                    return (start - now).total_seconds(), "Waiting for the start time, " + start.strftime("%Y-%m-%d %H:%M") + "."
            except ValueError:
                pass
        opens, closes = _clock(c["window_start"]), _clock(c["window_end"])
        if opens is None or closes is None or opens == closes:
            return 0, ""
        t = now.time()
        inside = opens <= t < closes if opens < closes else (t >= opens or t < closes)
        if inside:
            return 0, ""
        nxt = now.replace(hour=opens.hour, minute=opens.minute, second=0, microsecond=0)
        if nxt <= now:
            nxt += datetime.timedelta(days=1)
        return (nxt - now).total_seconds(), f"Outside the allowed hours; continues at {c['window_start']}."


def _clock(value):
    try:
        return datetime.time.fromisoformat(value) if value else None
    except ValueError:
        return None
