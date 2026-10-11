import datetime
import os
import time
import tempfile
import unittest

from termux_bulk_sms.sender import Sender
from termux_bulk_sms.store import Store

NOON = lambda: datetime.datetime(2026, 1, 15, 12, 0)  # noqa: E731


class SendingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.file = os.path.join(self.dir.name, "t.sqlite3")
        self.store = Store(self.file, "46", lookup_enabled=True, dry_run=True)
        self.sent = []

    def tearDown(self):
        self.store.db.close()
        self.dir.cleanup()

    def campaign(self, rows, **over):
        data = {"name": "Test", "template": "Hej [name]!", "delay": 1, "jitter": 0, "start_at": "",
                "window_start": "08:00", "window_end": "21:00", "first_name_only": 0, "allow_repeat": 0}
        data.update(over)
        cid = self.store.create_campaign(data)
        self.store.add_recipients(cid, [{"name": n, "phone_raw": p, "fields": {}} for n, p in rows])
        return cid

    def answer(self, cid, known=()):
        phones = self.store.phones_to_look_up(cid)
        self.store.store_lookup(cid, {p: ("known" if p in known else "unknown") for p in phones})
        self.store.classify(cid)

    def statuses(self, cid):
        return [r["status"] for r in self.store.recipients(cid, None, 500)[0]]

    def sender(self, send=None, now=NOON):
        def default(phone, text):
            self.sent.append((phone, text))
            return "sent", ""
        # Time is the test's own: sleeping moves the clock, nothing really waits.
        self.clock = 0.0

        def sleep(seconds):
            self.clock += seconds
            self.naps.append(seconds)
            time.sleep(0.0005)
        self.naps = []
        return Sender(self.store, {"max_consecutive_failures": 3}, send=send or default,
                      sleep=sleep, now=now, wake=lambda lock: None, clock=lambda: self.clock)

    def run_to_end(self, cid, **kw):
        s = self.sender(**kw)
        s.start(cid)
        s.join(10)
        self.assertFalse(s.thread.is_alive())
        return s

    def until(self, condition):
        deadline = time.monotonic() + 5
        while not condition():
            self.assertLess(time.monotonic(), deadline, "timed out")
            time.sleep(0.002)

    def test_nothing_is_ready_before_the_lookup_has_answered(self):
        cid = self.campaign([("Anna", "0701234567")])
        self.store.classify(cid)
        self.assertEqual(self.statuses(cid), ["unchecked"])
        self.assertIsNone(self.store.claim_next(cid))

    def test_every_reason_to_skip(self):
        self.store.block("+46700000003")
        self.store.x("INSERT INTO sent_log (phone, sent_at) VALUES ('+46700000004', 1)")
        cid = self.campaign([("A", "0700000001"), ("B", "070-000 00 01"), ("C", "0700000002"),
                             ("D", "0700000003"), ("E", "0700000004"), ("F", "nope"), ("", "0700000005")])
        self.answer(cid, known={"+46700000002"})
        self.assertEqual(self.statuses(cid),
                         ["ready", "duplicate", "known", "blocked", "texted", "invalid", "incomplete"])

    def test_an_edited_number_must_be_looked_up_again(self):
        cid = self.campaign([("A", "0700000001")])
        self.answer(cid)
        rid = self.store.recipients(cid)[0][0]["id"]
        self.store.edit_recipient(rid, "A", "0700000009")
        self.store.classify(cid)
        self.assertEqual(self.statuses(cid), ["unchecked"])

    def test_a_run_sends_each_ready_row_once_and_finishes(self):
        cid = self.campaign([("Anna", "0700000001"), ("Bo", "0700000002"), ("Bo igen", "0700000002")])
        self.answer(cid)
        self.run_to_end(cid)
        self.assertEqual(self.sent, [("+46700000001", "Hej Anna!"), ("+46700000002", "Hej Bo!")])
        self.assertEqual(self.store.campaign(cid)["status"], "done")
        # A later campaign never texts them again.
        again = self.campaign([("Anna", "0700000001")])
        self.answer(again)
        self.assertEqual(self.statuses(again), ["texted"])

    def test_going_live_puts_the_rehearsed_rows_back_in_the_queue(self):
        cid = self.campaign([("Anna", "0700000001"), ("Bo", "0700000002")])
        self.answer(cid)
        self.run_to_end(cid, send=lambda phone, text: ("dry", "Dry run — nothing was sent."))
        self.assertEqual(self.statuses(cid), ["dry", "dry"])
        self.assertEqual(self.store.campaign(cid)["status"], "done")
        # A row an older version recorded as "sent" during a rehearsal.
        self.store.x("UPDATE recipients SET status='sent' WHERE position=1 AND campaign_id=?", (cid,))
        # Restarted, still rehearsing: nothing moves.
        self.store.recover()
        self.assertEqual(self.statuses(cid), ["dry", "dry"])
        # Restarted live.
        self.store.db.close()
        self.store = Store(self.file, "46", lookup_enabled=True, dry_run=False)
        self.store.recover()
        self.assertEqual(self.statuses(cid), ["ready", "ready"])
        self.assertEqual(self.store.campaign(cid)["status"], "paused")
        self.run_to_end(cid)
        self.assertEqual([p for p, _ in self.sent], ["+46700000001", "+46700000002"])
        self.assertEqual(self.statuses(cid), ["sent", "sent"])
        # And a real "sent" is never released.
        self.store.recover()
        self.assertEqual(self.statuses(cid), ["sent", "sent"])

    def test_a_round_sends_several_and_then_waits(self):
        cid = self.campaign([(str(i), f"070000000{i}") for i in range(1, 8)], batch=3)
        self.answer(cid)
        trace = []
        s = self.sender(send=lambda phone, text: (trace.append(self.clock), ("sent", ""))[1])
        s.start(cid)
        s.join(10)
        # delay 1 s, no jitter: three at once, a second later three more, then the last.
        self.assertEqual(trace, [0.0] * 3 + [1.0] * 3 + [2.0])
        self.assertEqual(self.store.campaign(cid)["status"], "done")

    def test_an_ongoing_campaign_stays_on_and_sends_to_whoever_is_added(self):
        cid = self.campaign([("Anna", "0700000001")], kind="ongoing")
        self.answer(cid)
        s = self.sender()
        s.start(cid)
        self.until(lambda: len(self.sent) == 1)
        self.until(lambda: self.store.campaign(cid)["note"].startswith("On: waiting"))
        self.assertEqual(self.store.campaign(cid)["status"], "running")
        self.store.add_recipients(cid, [{"name": "Bo", "phone_raw": "0700000002", "fields": {}},
                                        {"name": "Anna igen", "phone_raw": "0700000001", "fields": {}}])
        self.answer(cid)
        self.until(lambda: len(self.sent) == 2)
        self.assertEqual(self.sent[1], ("+46700000002", "Hej Bo!"))
        self.assertEqual(self.statuses(cid), ["sent", "sent", "duplicate"])
        self.assertEqual(self.store.campaign(cid)["status"], "running")
        s.pause(cid, "Stopped by you.")
        s.join(5)
        self.assertFalse(s.thread.is_alive())
        self.store.add_recipients(cid, [{"name": "Cia", "phone_raw": "0700000003", "fields": {}}])
        self.answer(cid)
        time.sleep(0.05)
        self.assertEqual(len(self.sent), 2, "a stopped campaign sends to nobody")

    def test_an_ongoing_campaign_may_be_turned_on_empty(self):
        cid = self.campaign([], kind="ongoing")
        s = self.sender()
        s.start(cid)
        self.until(lambda: self.store.campaign(cid)["note"].startswith("On: waiting"))
        self.store.add_recipients(cid, [{"name": "Dan", "phone_raw": "0700000004", "fields": {}}])
        self.answer(cid)
        self.until(lambda: len(self.sent) == 1)
        s.shutdown("Paused: the app was stopped.")
        self.assertEqual(self.store.campaign(cid)["status"], "paused")

    def test_two_campaigns_can_be_on_at_once(self):
        ongoing = self.campaign([], kind="ongoing")
        fixed = self.campaign([("A", "0700000001"), ("B", "0700000002")])
        self.answer(fixed)
        s = self.sender()
        s.start(ongoing)
        s.start(fixed)
        self.until(lambda: self.store.campaign(fixed)["status"] == "done")
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.store.campaign(ongoing)["status"], "running")
        s.shutdown("stop")

    def test_a_dry_run_leaves_no_history(self):
        cid = self.campaign([("Anna", "0700000001")])
        self.answer(cid)
        self.run_to_end(cid, send=lambda phone, text: ("dry", "Dry run"))
        self.assertEqual(self.statuses(cid), ["dry"])
        real = self.campaign([("Anna", "0700000001")])
        self.answer(real)
        self.assertEqual(self.statuses(real), ["ready"])

    def test_a_number_blocked_during_the_run_is_not_sent(self):
        cid = self.campaign([("A", "0700000001"), ("B", "0700000002")])
        self.answer(cid)
        self.store.block("+46700000002")
        self.run_to_end(cid)
        self.assertEqual([p for p, _ in self.sent], ["+46700000001"])

    def test_three_failures_in_a_row_pause_the_campaign(self):
        cid = self.campaign([(str(i), f"070000000{i}") for i in range(1, 6)])
        self.answer(cid)
        self.run_to_end(cid, send=lambda phone, text: ("failed", "no signal"))
        self.assertEqual(self.store.campaign(cid)["status"], "paused")
        self.assertEqual(self.statuses(cid), ["failed"] * 3 + ["ready"] * 2)

    def test_an_uncertain_send_stops_everything(self):
        cid = self.campaign([("A", "0700000001"), ("B", "0700000002")])
        self.answer(cid)
        self.run_to_end(cid, send=lambda phone, text: ("uncertain", "timeout"))
        self.assertEqual(self.statuses(cid), ["uncertain", "ready"])
        self.assertEqual(self.store.campaign(cid)["status"], "paused")

    def test_nothing_is_sent_outside_the_allowed_hours(self):
        s = self.sender(now=lambda: datetime.datetime(2026, 1, 15, 22, 30))
        wait, why = s._hold({"start_at": "", "window_start": "08:00", "window_end": "21:00"})
        self.assertEqual(wait, 9.5 * 3600)
        self.assertEqual(s._hold({"start_at": "2026-01-15T23:00", "window_start": "", "window_end": ""})[0], 1800)
        s.now = NOON
        self.assertEqual(s._hold({"start_at": "", "window_start": "08:00", "window_end": "21:00"})[0], 0)

    def test_a_crash_mid_send_is_never_resent(self):
        cid = self.campaign([("A", "0700000001"), ("B", "0700000002")])
        self.answer(cid)
        self.store.set_status(cid, "running")
        self.store.claim_next(cid)          # the process dies here
        self.store.db.close()
        self.store = Store(self.file, "46", lookup_enabled=True)
        self.store.recover()
        self.assertEqual(self.statuses(cid), ["uncertain", "ready"])
        self.assertEqual(self.store.campaign(cid)["status"], "paused")
        self.store.classify(cid)
        self.assertEqual(self.statuses(cid), ["uncertain", "ready"])


if __name__ == "__main__":
    unittest.main()
