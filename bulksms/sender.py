"""The one worker that sends: one campaign at a time, one message at a time."""

import datetime
import json
import random
import subprocess
import threading
import time

from . import template


class Busy(Exception):
    pass


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
    def __init__(self, store, cfg, send=None, sleep=time.sleep, now=datetime.datetime.now, wake=_wake):
        self.store, self.cfg = store, cfg
        self.send = send or termux_send(cfg)
        self.sleep, self.now, self.wake = sleep, now, wake
        self.lock = threading.Lock()
        self.thread = None
        self.stop = threading.Event()
        self.campaign_id = None
        self.next_at = None

    def running_id(self):
        return self.campaign_id if self.thread and self.thread.is_alive() else None

    def start(self, cid):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise Busy()
            self.stop = threading.Event()
            self.campaign_id = cid
            self.store.set_status(cid, "running")
            self.store.event(cid, "Started." + (" DRY RUN: nothing is really sent." if self.cfg.get("dry_run") else ""))
            self.thread = threading.Thread(target=self._run, args=(cid, self.stop), daemon=True)
            self.thread.start()

    def pause(self, cid, note="Paused."):
        if self.campaign_id == cid:
            self.stop.set()
        c = self.store.campaign(cid)
        if c and c["status"] == "running":
            self.store.set_status(cid, "paused", note)
            self.store.event(cid, note)

    def join(self, timeout=None):
        if self.thread:
            self.thread.join(timeout)

    # -- the loop ----------------------------------------------------------

    def _run(self, cid, stop):
        self.wake(True)
        failures = 0
        try:
            while not stop.is_set():
                c = self.store.campaign(cid)
                if c is None or c["status"] != "running":
                    return
                wait, why = self._hold(c)
                if wait > 0:
                    self.store.set_note(cid, why)
                    self._nap(min(wait, 5), stop)
                    continue
                self.store.set_note(cid, "")
                r = self.store.claim_next(cid)
                if r is None:
                    self.store.set_status(cid, "done")
                    self.store.event(cid, "Finished: nobody left to send to.")
                    return
                vals = template.values(r["name"], json.loads(r["fields"]), c["first_name_only"])
                text = template.render(c["template"], vals)
                outcome, detail = self.send(r["phone"], text)
                if outcome in ("sent", "dry"):
                    # A rehearsal must not put anyone in the history: the real
                    # run would then skip them all as "already texted".
                    self.store.mark_sent(r, text, detail, log=outcome == "sent")
                    failures = 0
                elif outcome == "uncertain":
                    self.store.mark(r["id"], "uncertain", detail)
                    self.store.set_status(cid, "paused", "Paused: one message may or may not have been sent.")
                    self.store.event(cid, f"Uncertain result for {r['phone']}; paused.")
                    return
                else:
                    self.store.mark(r["id"], "failed", detail)
                    self.store.event(cid, f"Failed for {r['phone']}: {detail}")
                    failures += 1
                    if failures >= int(self.cfg.get("max_consecutive_failures", 3)):
                        self.store.set_status(cid, "paused", f"Paused after {failures} failures in a row.")
                        self.store.event(cid, f"Paused after {failures} failures in a row.")
                        return
                if not self.store.has_ready(cid):
                    continue
                self._nap(self._delay(c), stop, cid)
        finally:
            self.next_at = None
            self.wake(False)

    def _delay(self, c):
        jitter = max(0.0, float(c["jitter"]))
        return max(1.0, float(c["delay"]) + random.uniform(-jitter, jitter))

    def _nap(self, seconds, stop, cid=None):
        """Sleep in short steps so pause is immediate; a shorter delay set meanwhile applies."""
        started = time.monotonic()
        self.next_at = time.time() + seconds
        while not stop.is_set():
            left = seconds - (time.monotonic() - started)
            if cid is not None:
                c = self.store.campaign(cid)
                if c is None or c["status"] != "running":
                    break
                ceiling = float(c["delay"]) + max(0.0, float(c["jitter"]))
                if ceiling < seconds:
                    seconds = max(1.0, ceiling)
                    self.next_at = time.time() + max(0.0, seconds - (time.monotonic() - started))
                    continue
            if left <= 0:
                break
            self.sleep(min(0.5, left))
        self.next_at = None

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
