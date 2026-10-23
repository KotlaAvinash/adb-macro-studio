# -*- coding: utf-8 -*-
"""Regression suite: one test per bug that has bitten this tool in the field, so a later change
that brings any of them back FAILS here (and in CI) instead of on an investigation phone.

Runs with the standard library only (no device, no installs):
    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import adb_core as core          # noqa: E402
import bt_transfer as bt         # noqa: E402
import contacts_extract as ce    # noqa: E402
import whatsapp_batch as wb      # noqa: E402

CID = wb.CONTACT_ID


# ----------------------------------------------------------------------------- helpers
def mkzip(path, media=True, size=2000):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("_chat.txt", "hello")
        if media:
            z.writestr("IMG-0001.jpg", b"\xff\xd8" + b"0" * size)


def node(text="", desc="", rid="x", b=(0, 0, 100, 50)):
    return {"text": text, "desc": desc, "id": "com.whatsapp:id/" + rid, "cls": "", "pkg": "",
            "clickable": True, "bounds": tuple(b)}


class FakeClock:
    """Virtual time: sleep() advances instantly and fires scheduled events (e.g. 'a file lands')."""
    def __init__(self):
        self.t = 1_000_000.0
        self.hooks = []

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s
        for h in list(self.hooks):
            if self.t >= h[0]:
                self.hooks.remove(h)
                h[1]()

    def at(self, delay, fn):
        self.hooks.append((self.t + delay, fn))


class Ev:
    def __init__(self, v=False):
        self.v = v

    def set(self):
        self.v = True

    def clear(self):
        self.v = False

    def is_set(self):
        return self.v


class Case:
    def __init__(self, d=None):
        self.dir = d or tempfile.mkdtemp()

    def record_file(self, *a, **k):
        pass


# ----------------------------------------------------------------------------- chat names
class TestNames(unittest.TestCase):
    def test_non_latin_names_have_a_fingerprint(self):
        # BUG: [0-9a-z]-only fingerprint collapsed Telugu/Hindi/Arabic names to "" -> mass-skipped
        for nm in ("రిజర్వ్ బ్యాంక్", "भारतीय रिज़र्व", "مرحبا"):
            self.assertNotEqual(wb._fingerprint(nm), "", nm)
        self.assertNotEqual(wb._fingerprint("రిజర్వ్"), wb._fingerprint("బ్యాంక్"))

    def test_emoji_only_name_never_a_duplicate_key(self):
        self.assertEqual(wb._fingerprint("❤️👍"), "")

    def test_norm_name_strips_marks_anywhere_and_unifies_spaces(self):
        # BUG: marks were only stripped at the ends; NBSP vs space read as different chats
        self.assertEqual(wb._norm_name("‎Bob‏"), "Bob")
        self.assertEqual(wb._norm_name("Bo‪b‬"), "Bob")
        self.assertEqual(wb._norm_name("+91 98765 43210"), "+91 98765 43210")
        self.assertEqual(wb._norm_name("a  \t b"), "a b")

    def test_norm_name_keeps_zwj_and_normalizes_nfc(self):
        self.assertIn("‍", wb._norm_name("👨‍👩"))           # emoji / Indic need ZWJ
        self.assertEqual(wb._norm_name("é"), wb._norm_name("é"))


# ----------------------------------------------------------------------------- screen reading
def _xml(*rows):
    body = "".join(
        f'<node text="{t}" content-desc="{d}" resource-id="com.whatsapp:id/{rid}" class="" '
        f'package="com.whatsapp" clickable="true" bounds="[{b[0]},{b[1]}][{b[2]},{b[3]}]" />'
        for t, d, rid, b in rows)
    return f"<?xml version='1.0' encoding='UTF-8'?><hierarchy rotation=\"0\">{body}</hierarchy>"


class TestScreenReading(unittest.TestCase):
    def setUp(self):
        wb._NODE_CACHE.update(adb=None, gen=None, t=0.0, nodes=None)

    def test_truncated_dump_is_not_complete(self):
        self.assertTrue(core._complete_dump("<hierarchy><node/></hierarchy>"))
        self.assertFalse(core._complete_dump("<hierarchy><node text='a'"))   # killed mid-write

    def test_ui_dump_retries_a_truncated_dump(self):
        # BUG: a dump cut short was accepted -> parse failed -> whole screen read as EMPTY
        full = _xml(("Alice", "", CID, (0, 100, 500, 160))).encode()
        outs = [b"<hierarchy><node text='Al", full]

        class FakeAdb(core.Adb):
            def run(self, *args, **k):
                if "uiautomator" in args and "exec-out" in args:
                    return outs.pop(0)
                if "cat" in args:
                    return b"<hierarchy><node"
                return b""
        with mock.patch.object(core.time, "sleep", lambda s: None):
            text = FakeAdb("adb").ui_dump()
        self.assertTrue(core._complete_dump(text))

    def test_one_bad_character_does_not_blank_the_screen(self):
        # BUG: an XML-illegal char in ONE contact name made ET.fromstring throw -> 0 nodes
        xml = _xml(("Ra\x01mesh", "", CID, (0, 100, 500, 160)), ("Bob", "", CID, (0, 200, 500, 260)))
        names = [n["text"] for n in core.parse_ui_nodes(xml)]
        self.assertEqual(names, ["Ramesh", "Bob"])

    def test_invalid_char_reference_is_dropped(self):
        xml = _xml(("Ann&#1;e", "", CID, (0, 100, 500, 160)))
        self.assertEqual(core.parse_ui_nodes(xml)[0]["text"], "Anne")

    def test_malformed_xml_falls_back_to_regex(self):
        xml = _xml(("Tom & Jerry", "", CID, (0, 100, 500, 160)), ("Bob", "", CID, (0, 200, 500, 260)))
        names = [n["text"] for n in core.parse_ui_nodes(xml)]    # bare '&' is invalid XML
        self.assertEqual(names, ["Tom & Jerry", "Bob"])

    def test_name_read_from_content_desc_when_text_empty(self):
        # BUG: rows exposing the name only in content-desc were never identified
        rows = wb._chat_rows([node("", "Alice", CID, (0, 100, 500, 160))])
        self.assertEqual([r["name"] for r in rows], ["Alice"])
        with mock.patch.object(wb, "_nodes", lambda adb: [node("", "A", CID, (0, 1, 9, 50)),
                                                          node("", "B", CID, (0, 60, 9, 110))]):
            self.assertTrue(wb.on_chat_list(None))

    def test_clipped_row_is_not_tappable(self):
        rows = wb._chat_rows([node("Alice", "", CID, (0, 100, 500, 160)),
                              node("Bob", "", CID, (0, 2290, 500, 2300))])   # 10px sliver at edge
        full = {r["name"]: r["full"] for r in rows}
        self.assertTrue(full["Alice"])
        self.assertFalse(full["Bob"])

    def test_find_chat_node_prefers_the_unclipped_row(self):
        nodes = [node("Bob", "", CID, (0, 0, 500, 8)), node("Bob", "", CID, (0, 500, 500, 560))]
        with mock.patch.object(wb, "_nodes", lambda adb: nodes):
            self.assertEqual(wb._find_chat_node(None, "bob")["bounds"], (0, 500, 500, 560))

    def test_dump_cache_only_reuses_within_a_step(self):
        class A:
            input_gen = 0
            dumps = 0

            def ui_dump(self):
                A.dumps += 1
                return _xml()
        a = A()
        wb._nodes(a); wb._nodes(a)
        self.assertEqual(A.dumps, 1)                     # back-to-back, no input -> reused
        a.input_gen += 1
        wb._nodes(a)
        self.assertEqual(A.dumps, 2)                     # after input -> fresh
        time.sleep(wb._NODE_TTL + 0.05)
        wb._nodes(a)
        self.assertEqual(A.dumps, 3)                     # poll interval > TTL -> fresh


# ----------------------------------------------------------------------------- evidence CSV
class TestEvidenceCSV(unittest.TestCase):
    def test_atomic_write_creates_dirs_and_never_erases(self):
        # BUG: CSV write failed when the case folder was missing; an empty run could erase data
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a", "b", "exported_chats.csv")
        wb.write_csv_atomic(p, [[1, "Alice", "ok", "with media", "a.zip", "h", "t"]])
        size = os.path.getsize(p)
        self.assertGreater(size, 0)
        wb.write_csv_atomic(p, [])                       # an empty table must NOT overwrite data
        self.assertEqual(os.path.getsize(p), size)
        self.assertFalse(os.path.exists(p + ".tmp"))

    def test_resume_csv_names_match_live_reads(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x.csv")
        wb.write_csv_atomic(p, [[1, wb._csv_safe("‎Alice"), "ok", "", "", "", ""],
                                [2, wb._csv_safe("+91 99999 11111"), "fail-transfer", "", "", "", ""]])
        st, order, _, _ = wb.load_progress(p)
        self.assertEqual(order, ["Alice", "+91 99999 11111"])
        self.assertEqual(st["Alice"], "ok")

    def test_same_device_continues_its_own_csv(self):
        cases = tempfile.mkdtemp()
        for folder, txt in (("20260101_000000_DEVA", "old"), ("20260202_000000_DEVA", "new"),
                            ("20260101_000000_DEVB", "b"), ("20260101_000000_DEVC", "")):
            os.makedirs(os.path.join(cases, folder))
            with open(os.path.join(cases, folder, "exported_chats.csv"), "w") as f:
                f.write(txt)
        old = os.path.join(cases, "20260101_000000_DEVA", "exported_chats.csv")
        os.utime(old, (time.time() - 1000, time.time() - 1000))
        self.assertIn("20260202_000000_DEVA", wb.latest_csv_for_serial(cases, "DEVA"))
        self.assertIsNone(wb.latest_csv_for_serial(cases, "DEVC"))   # empty csv ignored
        self.assertIsNone(wb.latest_csv_for_serial(cases, "NOPE"))


# ----------------------------------------------------------------------------- Quick Share transfer
class TestQuickShareNoDuplicates(unittest.TestCase):
    def test_phone_says_sent_and_slow_file_lands_late(self):
        # BUG (field): phone showed 'Sent', the slow file landed ~40s later, the tool had already
        # given up and RE-SENT -> duplicate (1)(2)(3).zip. Must wait and accept the late file.
        clock, d = FakeClock(), tempfile.mkdtemp()
        before = bt.snapshot(d)
        target = os.path.join(d, "WhatsApp Chat with Santhi.zip")
        clock.at(40, lambda: mkzip(target))
        with mock.patch.object(wb, "time", clock), \
                mock.patch.object(wb, "_nodes", lambda adb: [node("Sent")]):
            res = wb._await_late_file(None, d, before, lambda m: None, Ev(), timeout=200)
        self.assertIsNotNone(res)
        self.assertEqual(res[0], target)

    def test_nothing_arriving_gives_up_so_real_failures_retry(self):
        clock, d = FakeClock(), tempfile.mkdtemp()
        start = clock.t
        with mock.patch.object(wb, "time", clock), mock.patch.object(wb, "_nodes", lambda adb: []):
            res = wb._await_late_file(None, d, bt.snapshot(d), lambda m: None, Ev(), timeout=200)
        self.assertIsNone(res)
        self.assertLess(clock.t - start, 60)

    def test_corrupt_partial_zip_is_never_accepted(self):
        clock, d = FakeClock(), tempfile.mkdtemp()
        before = bt.snapshot(d)
        with open(os.path.join(d, "WhatsApp Chat with LTI.zip"), "wb") as f:
            f.write(b"PK\x03\x04" + b"\x00" * 64)                      # truncated transfer
        with mock.patch.object(wb, "time", clock), mock.patch.object(wb, "_nodes", lambda adb: []):
            self.assertIsNone(wb._await_late_file(None, d, before, lambda m: None, Ev(), 200))

    def _retry(self, send, late=None, reopen=lambda: True):
        with mock.patch.object(wb, "export_and_send", send), \
                mock.patch.object(wb, "_await_late_file", lambda *a, **k: late):
            return wb.export_with_retry(None, Case(), (1080, 2400), reopen, lambda m: None, None,
                                        None, "PC", tempfile.mkdtemp(), lambda: None)

    def test_late_file_is_accepted_without_a_second_send(self):
        calls = []

        def send(*a, **k):
            calls.append(1)
            return "fail-transfer", None, None, "with media"
        st = self._retry(send, late=("late.zip", 10, "h"))
        self.assertEqual(st[0], "ok")
        self.assertEqual(len(calls), 1)

    def test_persistent_failure_is_retried_exactly_three_times(self):
        calls = []

        def send(*a, **k):
            calls.append(1)
            return "fail-transfer", None, None, "with media"
        self.assertEqual(self._retry(send)[0], "fail-transfer")
        self.assertEqual(len(calls), 1 + wb.SHARE_RETRIES)

    def test_chat_not_on_screen_is_reported_not_sent(self):
        calls = []
        st = self._retry(lambda *a, **k: calls.append(1), reopen=lambda: False)
        self.assertEqual(st[0], "fail-notfound")
        self.assertEqual(calls, [])

    def test_blocked_privacy_is_not_retried(self):
        self.assertNotIn("blocked-privacy", wb.SHARE_RETRY_STATUSES)

    def test_phone_failed_text_never_fast_fails(self):
        import inspect
        self.assertNotIn('return "failed"', inspect.getsource(wb.export_and_send))


# ----------------------------------------------------------------------------- pause
class TestPause(unittest.TestCase):
    def test_pause_wait_blocks_and_reports_duration(self):
        self.assertEqual(wb._pause_wait(None), 0.0)
        p = Ev(True)
        threading.Timer(0.5, p.clear).start()
        self.assertGreaterEqual(wb._pause_wait(p), 0.4)

    def test_stop_breaks_a_pause(self):
        t0 = time.time()
        wb._pause_wait(Ev(True), Ev(True))
        self.assertLess(time.time() - t0, 0.5)

    def test_no_phone_work_while_paused(self):
        # BUG: Pause was only checked at the loop top -> a chat mid-export ignored it for minutes
        pause, calls, out = Ev(True), [], {}

        def send(*a, **k):
            calls.append(1)
            return "ok", "f.zip", "h", "with media"
        with mock.patch.object(wb, "export_and_send", send):
            t = threading.Thread(target=lambda: out.setdefault("r", wb.export_with_retry(
                None, Case(), (1, 1), lambda: True, lambda m: None, None, None, "PC",
                tempfile.mkdtemp(), lambda: None, pause=pause)))
            t.start()
            time.sleep(0.8)
            self.assertEqual(calls, [])                  # paused -> nothing sent
            pause.clear()
            t.join(5)
        self.assertEqual(out["r"][0], "ok")


# ----------------------------------------------------------------------------- run resilience
class TestRunResilience(unittest.TestCase):
    def test_unexpected_error_on_one_chat_does_not_kill_the_run(self):
        class FakeAdb:
            path = "adb"
            input_gen = 0

            def shell(self, *a, **k):
                return "1800000"

            def key(self, *a):
                pass

            def swipe(self, *a):
                pass

            def tap(self, *a):
                pass
        logs, events, calls = [], [], {"n": 0}

        def emit(kind, *a):
            events.append(kind)
            if kind == "log":
                logs.append(a[0])
        b = wb.RollingBatch(FakeAdb(), Case(), (1080, 2400), emit, save_dir=tempfile.mkdtemp())
        b._device_ready = lambda: True
        b._ensure_list = lambda: True
        b._recover_screen = lambda: True

        def boom(nodes):                      # the walk's per-page read of the chat rows
            calls["n"] += 1
            if calls["n"] <= 3:
                raise ValueError("synthetic")
            b._stop.set()
            return []
        with mock.patch.object(wb, "time", FakeClock()), \
                mock.patch.object(wb, "_chat_rows", boom), \
                mock.patch.object(wb, "keep_awake", lambda *a, **k: None), \
                mock.patch.object(wb, "pc_keep_awake", lambda *a, **k: None), \
                mock.patch.object(wb, "dismiss_anr", lambda adb: False), \
                mock.patch.object(wb, "on_share_sheet", lambda adb: False):
            b.run()
        self.assertIn("finished", events)
        self.assertTrue(any("unexpected error (continuing" in m for m in logs))


# ----------------------------------------------------------------------------- contacts + zips
class TestContactsAndZips(unittest.TestCase):
    def test_contacts_extraction_with_jids(self):
        phones = ("Row: 0 display_name=Ramesh, Kumar, data1=+91 98765 43210\n"
                  "Row: 1 display_name=Office, data1=040 1234 5678")
        data = ("Row: 0 contact_id=11, display_name=Ramesh, Kumar, data1=+91 98765 43210, "
                "mimetype=vnd.android.cursor.item/vnd.com.whatsapp.profile\n"
                "Row: 1 contact_id=99, display_name=John, data1=j@x.com, "
                "mimetype=vnd.android.cursor.item/email_v2")
        rc = ("Row: 0 contact_id=11, display_name=Ramesh, Kumar, account_type=com.whatsapp, "
              "sync1=919876543210@s.whatsapp.net, sync2=, sync3=\n"
              "Row: 1 contact_id=99, display_name=John, account_type=com.google, sync1=, sync2=, sync3=")

        class A:
            def shell(self, *args, **k):
                uri = args[args.index("--uri") + 1]
                return phones if uri.endswith("/phones") else data if uri.endswith("/data") else rc
        res = ce.extract_contacts(A(), tempfile.mkdtemp())
        self.assertEqual(res["contacts"], 2)
        self.assertEqual(res["whatsapp"], 1)                     # Google contact excluded

    def test_zip_verification(self):
        d = tempfile.mkdtemp()
        a, b, c = (os.path.join(d, n) for n in ("a.zip", "b.zip", "c.zip"))
        mkzip(a, media=True)
        mkzip(b, media=False)
        with open(c, "wb") as f:
            f.write(b"PK\x03\x04" + b"\x00" * 40)
        self.assertEqual(bt.zip_media_info(a)["label"], "with media")
        self.assertEqual(bt.zip_media_info(b)["label"], "without media")
        self.assertFalse(bt.zip_media_info(c)["ok"])


# ----------------------------------------------------------------------------- read-only backup
def _msgstore(path, with_media_root=None):
    """Build a tiny msgstore.db with the real modern schema subset for the reconstructor."""
    import sqlite3
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE jid(_id INTEGER PRIMARY KEY, user TEXT, server TEXT, agent INT, device INT,
                         type INT, raw_string TEXT);
        CREATE TABLE jid_map(lid_row_id INT, jid_row_id INT, sort_id INT);
        CREATE TABLE lid_display_name(lid_row_id INT, display_name TEXT, username TEXT);
        CREATE TABLE chat(_id INTEGER PRIMARY KEY, jid_row_id INT, subject TEXT);
        CREATE TABLE message(_id INTEGER PRIMARY KEY, chat_row_id INT, from_me INT,
                             sender_jid_row_id INT, timestamp INT, message_type INT, text_data TEXT);
        CREATE TABLE message_media(message_row_id INT, chat_row_id INT, file_path TEXT,
                                   mime_type TEXT, media_caption TEXT);
    """)
    # jids: 1 = me-ish contact phone, 2 = Alice (phone), 3 = Alice's LID, 4 = group, 5 = Bob phone
    c.executemany("INSERT INTO jid VALUES(?,?,?,?,?,?,?)", [
        (2, "919876543210", "s.whatsapp.net", 0, 0, 0, "919876543210@s.whatsapp.net"),
        (3, "111222333", "lid", 0, 0, 0, "111222333@lid"),
        (4, "120363-GRP", "g.us", 0, 0, 0, "120363-GRP@g.us"),
        (5, "919000000005", "s.whatsapp.net", 0, 0, 0, "919000000005@s.whatsapp.net"),
    ])
    c.execute("INSERT INTO jid_map VALUES(3,2,0)")                 # Alice's LID -> her phone row
    c.execute("INSERT INTO lid_display_name VALUES(3,'Alice LID','alice')")
    c.executemany("INSERT INTO chat VALUES(?,?,?)", [
        (10, 3, None),            # 1:1 chat addressed by LID (modern privacy)
        (11, 4, "Project Team"),  # group
        (12, 5, None),            # 1:1, system-only -> should produce NO zip
    ])
    T = 1_700_000_000_000
    c.executemany("INSERT INTO message VALUES(?,?,?,?,?,?,?)", [
        (100, 10, 0, 3, T + 1000, 0, "hi there"),                 # Alice -> me
        (101, 10, 1, None, T + 2000, 0, "hello back"),            # me -> Alice
        (102, 10, 0, 3, T + 3000, 1, None),                       # Alice -> me, image
        (200, 11, 0, 5, T + 5000, 0, "team message"),             # Bob in group
        (300, 12, 0, 5, T + 9000, 7, None),                       # system only (no text/media)
    ])
    c.execute("INSERT INTO message_media VALUES(102,10,'Media/WhatsApp Images/IMG-1.jpg',"
              "'image/jpeg','a caption')")
    c.commit(); c.close()
    if with_media_root:
        d = os.path.join(with_media_root, "Media", "WhatsApp Images")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "IMG-1.jpg"), "wb") as f:
            f.write(b"\xff\xd8" + b"J" * 500)


class TestBackupReconstruct(unittest.TestCase):
    def setUp(self):
        import wa_backup
        self.wab = wa_backup
        self.d = tempfile.mkdtemp()
        self.db = os.path.join(self.d, "msgstore.db")

    def _zip_text(self, out, startswith):
        import glob
        z = [p for p in glob.glob(os.path.join(out, "*.zip"))
             if os.path.basename(p).startswith(startswith)]
        self.assertTrue(z, f"no zip starting with {startswith!r}")
        with zipfile.ZipFile(z[0]) as zf:
            return z[0], zf.read("_chat.txt").decode("utf-8"), zf.namelist()

    def test_reconstructs_only_chats_with_content(self):
        _msgstore(self.db)
        out = os.path.join(self.d, "out")
        mans = self.wab.reconstruct(self.db, None, out, copy_media=False, emit=lambda m: None)
        titles = {m["title"] for m in mans}
        self.assertIn("Project Team", titles)
        self.assertNotIn(None, titles)
        self.assertEqual(len(mans), 2)               # LID 1:1 + group; system-only chat skipped

    def test_lid_chat_resolves_to_phone_number(self):
        _msgstore(self.db)
        out = os.path.join(self.d, "out")
        self.wab.reconstruct(self.db, None, out, copy_media=False, emit=lambda m: None)
        _p, txt, _ = self._zip_text(out, "WhatsApp Chat with Alice LID")
        self.assertIn("919876543210", os.path.basename(_p))   # LID mapped to the real phone number

    def test_contact_name_used_when_available(self):
        _msgstore(self.db)
        out = os.path.join(self.d, "out")
        self.wab.reconstruct(self.db, None, out, contacts={"919876543210": "Alice Kumar"},
                             owner="Investigator", copy_media=False, emit=lambda m: None)
        _p, txt, _ = self._zip_text(out, "WhatsApp Chat with Alice Kumar")
        self.assertIn("Alice Kumar: hi there", txt)
        self.assertIn("Investigator: hello back", txt)       # from_me uses the owner name

    def test_media_message_becomes_attachment_and_is_copied(self):
        _msgstore(self.db, with_media_root=self.d)
        out = os.path.join(self.d, "out")
        self.wab.reconstruct(self.db, self.d, out, copy_media=True, emit=lambda m: None)
        _p, txt, names = self._zip_text(out, "WhatsApp Chat with Alice LID")
        self.assertIn("<attached: IMG-1.jpg>", txt)
        self.assertIn("a caption", txt)
        self.assertIn("IMG-1.jpg", names)                    # the real media file is inside the zip

    def test_output_is_viewer_parseable(self):
        import chat_viewer as cv
        _msgstore(self.db)
        out = os.path.join(self.d, "out")
        self.wab.reconstruct(self.db, None, out, copy_media=False, emit=lambda m: None)
        _p, txt, _ = self._zip_text(out, "WhatsApp Chat with Alice LID")
        msgs = cv.parse_chat_text(txt)
        self.assertGreaterEqual(len(msgs), 3)

    def test_only_numbers_filter(self):
        _msgstore(self.db)
        out = os.path.join(self.d, "out")
        mans = self.wab.reconstruct(self.db, None, out, only_numbers=["+91 98765 43210"],
                                    copy_media=False, emit=lambda m: None)
        self.assertEqual([m["title"] for m in mans], ["Alice LID"])

    def test_reconstruct_never_overwrites_existing_evidence(self):
        _msgstore(self.db)
        out = os.path.join(self.d, "out")
        os.makedirs(out)
        with open(os.path.join(out, "WhatsApp Chat with Project Team.zip"), "w") as f:
            f.write("PRE-EXISTING")
        self.wab.reconstruct(self.db, None, out, copy_media=False, emit=lambda m: None)
        with open(os.path.join(out, "WhatsApp Chat with Project Team.zip")) as f:
            self.assertEqual(f.read(), "PRE-EXISTING")       # untouched; new one saved as (1)
        import glob
        self.assertTrue(glob.glob(os.path.join(out, "*Project Team (1).zip")))

    def test_manifest_has_sha256_per_chat(self):
        _msgstore(self.db)
        out = os.path.join(self.d, "out")
        mans = self.wab.reconstruct(self.db, None, out, copy_media=False, emit=lambda m: None)
        for m in mans:
            self.assertEqual(len(m["sha256"]), 64)
        self.assertTrue(os.path.isfile(os.path.join(out, "backup_chats.csv")))

    def test_newest_backup_uses_one_device_command(self):
        seen = []

        class A:
            def shell(self, *args, **k):
                seen.append(args)
                if "-d" in args:
                    return "/sdcard/Android/media/com.whatsapp/WhatsApp"
                return ("/sdcard/Android/media/com.whatsapp/WhatsApp/Databases/"
                        "msgstore-2026-10-07.1.db.crypt15\n")
        got = self.wab.newest_backup(A())
        self.assertTrue(got.endswith(".crypt15"))
        self.assertTrue(all(len(s) <= 3 for s in seen))      # ls commands, device handles the glob


# ----------------------------------------------------------------- security-review remediations
class TestSecurityReview(unittest.TestCase):
    def _db(self, extra_media=None):
        import wa_backup  # noqa
        d = tempfile.mkdtemp()
        db = os.path.join(d, "msgstore.db")
        _msgstore(db, with_media_root=(d if extra_media else None))
        return d, db

    # F-01: existing manifest must never be overwritten; new one is atomic + distinct
    def test_f01_manifest_never_overwritten(self):
        import wa_backup
        d, db = self._db()
        out = os.path.join(d, "out"); os.makedirs(out)
        man = os.path.join(out, "backup_chats.csv")
        with open(man, "w") as f:
            f.write("DO-NOT-OVERWRITE")
        wa_backup.reconstruct(db, None, out, copy_media=False, emit=lambda m: None)
        with open(man) as f:
            self.assertEqual(f.read(), "DO-NOT-OVERWRITE")        # prior manifest intact
        import glob
        self.assertTrue(glob.glob(os.path.join(out, "backup_chats (1).csv")))
        self.assertFalse(os.path.exists(man + ".tmp"))           # atomic: no temp left

    # F-02: a DB media path must not escape the media root
    def test_f02_media_path_traversal_blocked(self):
        import wa_backup
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "Media"))
        with open(os.path.join(os.path.dirname(root), "outside.txt"), "w") as f:
            f.write("SECRET")
        self.assertIsNone(wa_backup._find_media_file(root, "../outside.txt", "outside.txt"))
        self.assertIsNone(wa_backup._find_media_file(root, "/etc/passwd", "passwd"))
        # a legitimate in-root file still resolves
        img = os.path.join(root, "Media", "WhatsApp Images")
        os.makedirs(img)
        with open(os.path.join(img, "IMG-1.jpg"), "wb") as f:
            f.write(b"\xff\xd8ok")
        self.assertIsNotNone(
            wa_backup._find_media_file(root, "Media/WhatsApp Images/IMG-1.jpg", "IMG-1.jpg"))

    # F-05: manifest message count = rendered messages (not raw rows incl. system); UTC + epoch kept
    def test_f05_counts_and_utc_timestamps(self):
        import wa_backup, csv as _csv
        d, db = self._db()
        out = os.path.join(d, "out")
        mans = wa_backup.reconstruct(db, None, out, copy_media=False, emit=lambda m: None)
        for m in mans:
            self.assertLessEqual(m["messages"], m["rows"])       # emitted <= raw rows
            self.assertTrue(m["first_utc"].endswith("Z"))        # UTC, unambiguous
            self.assertIsInstance(m["first_ms"], int)            # raw epoch retained
        # the rendered _chat.txt line count equals the manifest 'messages' for a chat
        import glob, zipfile as _zip
        z = glob.glob(os.path.join(out, "*Alice*"))[0]
        with _zip.ZipFile(z) as zf:
            txt = zf.read("_chat.txt").decode("utf-8")
        import chat_viewer as cv
        rendered = len(cv.parse_chat_text(txt))
        m = [x for x in mans if "Alice" in x["title"]][0]
        self.assertEqual(rendered, m["messages"])

    # F-03: viewer rejects a zip-bomb member before decompressing it
    def test_f03_zip_bomb_rejected(self):
        import chat_viewer as cv, zipfile as _zip, io
        d = tempfile.mkdtemp()
        p = os.path.join(d, "bomb.zip")
        with _zip.ZipFile(p, "w", _zip.ZIP_DEFLATED) as z:
            z.writestr("_chat.txt", "[01/01/2026, 00:00:00] A: hi")
            z.writestr("big.bin", b"\x00" * (50 * 1024 * 1024))   # 50MB of zeros -> huge ratio
        old = cv.MAX_ZIP_MEMBER
        try:
            cv.MAX_ZIP_MEMBER = 1 * 1024 * 1024                  # 1MB cap for the test
            self.assertIsNotNone(cv._zip_policy_violation(_zip.ZipFile(p)))
            self.assertIsNone(cv.load_zip(p, 0))                 # skipped, not loaded
        finally:
            cv.MAX_ZIP_MEMBER = old
        # a normal small zip still loads
        ok = os.path.join(d, "ok.zip")
        with _zip.ZipFile(ok, "w") as z:
            z.writestr("_chat.txt", "[01/01/2026, 00:00:00] A: hi")
        self.assertIsNone(cv._zip_policy_violation(_zip.ZipFile(ok)))

    # F-04: branch/wait timeout honours the real wall-clock budget (was ~45% of it)
    def test_f04_branch_timeout_is_wall_clock(self):
        import adb_core as core
        polls = {"n": 0}

        class A:
            input_gen = 0
            def ui_dump(self):
                polls["n"] += 1
                return "<hierarchy></hierarchy>"       # never matches
        clock = {"t": 1000.0}
        r = core.MacroRunner.__new__(core.MacroRunner)
        r.adb = A()
        r._stop = Ev()
        r.sleep = lambda s: (clock.__setitem__("t", clock["t"] + s) or r._stop.is_set())
        with mock.patch.object(core.time, "monotonic", lambda: clock["t"]):
            r._branch_wait("nope", "contains", "", 10.0)
        self.assertGreaterEqual(clock["t"] - 1000.0, 9.9)        # ~10s elapsed, not ~4.5s
        self.assertGreaterEqual(polls["n"], 9)

    # F-06: a macro with an invalid launch package / bad field type is rejected at load
    def test_f06_macro_validation(self):
        import adb_core as core, json
        d = tempfile.mkdtemp()
        def write(obj):
            p = os.path.join(d, "m.json")
            with open(p, "w") as f:
                json.dump(obj, f)
            return p
        with self.assertRaises(ValueError):
            core.load_macro(write({"steps": [{"type": "launch", "package": "evil; rm -rf /"}]}))
        with self.assertRaises(ValueError):
            core.load_macro(write({"steps": [{"type": "tap", "x": "not-a-number", "y": 1}]}))
        with self.assertRaises(ValueError):
            core.load_macro(write({"steps": [{"type": "wait", "seconds": -5}]}))
        # a valid macro loads
        core.load_macro(write({"steps": [{"type": "launch", "package": "com.whatsapp"},
                                         {"type": "tap", "x": 10, "y": 20}]}))

    # F-06/launch guard: Adb.launch refuses a bad package even if constructed directly
    def test_f06_launch_rejects_bad_package(self):
        import adb_core as core
        a = core.Adb("adb", "SER")
        with self.assertRaises(core.AdbError):
            a.launch("evil; reboot")

    # F-07: typed text is redacted in the logged adb command
    def test_f07_input_text_redacted_in_log(self):
        import adb_core as core
        self.assertEqual(core._redact_cmd(["shell", "input", "text", "s3cret"]),
                         ["shell", "input", "text", "<redacted>"])
        self.assertEqual(core._redact_cmd(["shell", "input", "tap", "1", "2"]),
                         ["shell", "input", "tap", "1", "2"])   # non-text untouched

    # after a send we must leave ANY non-WhatsApp foreground (Quick Share 'Completed' screen etc.)
    def test_back_to_list_detects_non_whatsapp_foreground(self):
        def fg(line):
            return type("A", (), {"shell": lambda self, *a, **k: f"  mCurrentFocus=Window{{ {line} }}"})()
        self.assertTrue(wb.on_whatsapp_foreground(fg("u0 com.whatsapp/.HomeActivity")))
        self.assertFalse(wb.on_whatsapp_foreground(
            fg("u0 com.google.android.gms/.quickshare.CompleteActivity")))
        self.assertFalse(wb.on_whatsapp_foreground(
            fg("u0 com.samsung.android.app.sharelive/.ReceiveActivity")))

    # F-10: co-visible same-name chats are surfaced (not silently merged)
    def test_f10_duplicate_names_detected(self):
        self.assertEqual(wb.duplicate_names(["A", "B", "A", "C"]), {"A"})
        self.assertEqual(wb.duplicate_names(["A", "B", "C"]), set())

    # F-08: verify_case recomputes hashes and reports any tamper
    def test_f08_verify_case_detects_tamper(self):
        import adb_core as core
        d = tempfile.mkdtemp()
        case = core.CaseLog(d, "SER")
        f = case.dir / "evidence.txt"
        f.write_text("original")
        case.record_file(f, "note")
        self.assertEqual(core.verify_case(case.dir)["mismatch"], [])   # clean
        f.write_text("TAMPERED")
        r = core.verify_case(case.dir)
        self.assertEqual([m[0] for m in r["mismatch"]], ["evidence.txt"])
        f.unlink()
        self.assertEqual(core.verify_case(case.dir)["missing"], ["evidence.txt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
