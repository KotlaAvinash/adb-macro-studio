# -*- coding: utf-8 -*-
"""Run integrity: every chat is exported, on any screen size / density / font size / brand.

FakeList is a scrollable WhatsApp chat list laid out like the real one (rows recorded from a real
phone: same resource-ids, an Archived row at the top of the list, filter chips above it). Its
geometry, swipe overshoot (fling) and swallowed swipes are configurable, so each guarantee is checked
on a small phone, a large phone and a large-font phone - not tuned to one device.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from xml.sax.saxutils import quoteattr
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import adb_core as core          # noqa: E402
import whatsapp_batch as wb      # noqa: E402

W = "com.whatsapp:id/"

# (screen w, h, row height, list top, list bottom): measured phone / small phone / large font
GEOMETRIES = {
    "large phone": (1440, 3120, 285, 1000, 2900),
    "small phone": (720, 1600, 150, 520, 1450),
    "large font": (1080, 2400, 400, 760, 2200),
}


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def strftime(self, fmt, *a):
        return "20261024_120000"


class FakeList:
    def __init__(self, chats, archived=(), geometry="large phone", overshoot=1.0, swallow=(),
                 start_offset=0, hidden_by_filter=(), stretch=False):
        self.w, self.h, self.row, self.top, self.bottom = GEOMETRIES[geometry]
        self.sections = {"chats": list(chats), "archived": list(archived)}
        self.section = "chats"
        self.offsets = {"chats": start_offset, "archived": 0}
        self.overshoot = overshoot
        self.swallow = set(swallow)            # swipe numbers (1-based) the phone ignores
        self.swipes = 0
        self.dumps = 0
        self.hidden = set(hidden_by_filter)    # chats hidden while a filter other than All is on
        self.filter_all = not self.hidden
        self.opened = []
        self.stretch = stretch       # Android 12+ overscroll: the list stretches past an end, more
        self.stretch_px = 0          # for a fast swipe than a slow one, until the next read settles
        self.input_gen = 0
        self.wa_package = "com.whatsapp"

    # --- list model
    def items(self):
        names = [n for n in self.sections[self.section] if self.filter_all or n not in self.hidden]
        if self.section == "chats" and self.sections["archived"]:
            return ["<archived>"] + names
        return names

    def max_offset(self):
        return max(0, len(self.items()) * self.row - (self.bottom - self.top))

    def ui_dump(self):
        """What `uiautomator dump` returns; the production parser and screen-read cache run on it."""
        self.dumps += 1
        rows = "".join(
            "<node text=%s resource-id=%s content-desc=%s class=\"\" package=\"com.whatsapp\" "
            "clickable=\"%s\" bounds=\"[%d,%d][%d,%d]\"/>"
            % (quoteattr(n["text"]), quoteattr(n["id"]), quoteattr(n["desc"]),
               "true" if n["clickable"] else "false", *n["bounds"])
            for n in self.nodes())
        return "<?xml version='1.0' ?><hierarchy rotation=\"0\">" + rows + "</hierarchy>"

    def nodes(self):
        out = [{"text": "", "desc": "", "id": "android:id/list", "cls": "", "pkg": "", "clickable": False,
                "bounds": (0, self.top, self.w, self.bottom)}]
        if self.section == "chats":
            out.append({"text": "", "desc": "L-SEARCH", "id": W + "my_search_bar", "cls": "", "pkg": "",
                        "clickable": True, "bounds": (40, self.top - 240, self.w - 40, self.top - 140)})
            out.append({"text": "", "desc": "L-ALL", "id": W + "conversations_filter_debug_view_id_all",
                        "cls": "", "pkg": "", "clickable": True,
                        "bounds": (40, self.top - 120, 240, self.top - 20)})
        else:
            out.append({"text": "", "desc": "L-BACK", "id": W + "wds_toolbar_nav_button", "cls": "",
                        "pkg": "", "clickable": True, "bounds": (0, 150, 150, 300)})
        off = self.offsets[self.section] + self.stretch_px
        self.stretch_px = 0
        for i, name in enumerate(self.items()):
            y1, y2 = self.top + i * self.row - off, self.top + (i + 1) * self.row - off
            v1, v2 = max(y1, self.top), min(y2, self.bottom)
            if v2 <= v1:
                continue
            if name == "<archived>":
                out.append({"text": "L-ARCHIVED", "desc": "", "id": W + "archived_row", "cls": "",
                            "pkg": "", "clickable": True, "bounds": (self.w // 5, v1, self.w // 2, v2)})
                continue
            out.append({"text": "", "desc": "", "id": W + "contact_row_container", "cls": "", "pkg": "",
                        "clickable": True, "bounds": (0, v1, self.w, v2)})
            ny1, ny2 = y1 + self.row // 5, y1 + self.row // 2
            nv1, nv2 = max(ny1, self.top), min(ny2, self.bottom)
            if nv2 > nv1:
                out.append({"text": name, "desc": "", "id": W + "conversations_row_contact_name", "cls": "",
                            "pkg": "", "clickable": False, "bounds": (self.w // 5, nv1, self.w * 3 // 4, nv2)})
        return out

    # --- adb surface
    def swipe(self, x1, y1, x2, y2, ms=300):
        self.input_gen += 1
        self.swipes += 1
        if self.swipes in self.swallow:
            return
        for y in (y1, y2):
            assert self.top <= y <= self.bottom, f"swipe at y={y} leaves the list {self.top}..{self.bottom}"
        dy = (y1 - y2) * self.overshoot
        sec = self.section
        want = self.offsets[sec] + dy
        self.offsets[sec] = int(min(max(want, 0), self.max_offset()))
        if self.stretch and want != self.offsets[sec]:
            speed = abs(y1 - y2) / max(ms, 1)                     # px per ms
            phase = (1.0, 0.55, 0.8)[self.swipes % 3]             # the read lands at a different
            self.stretch_px = int(max(-1.5, min(1.5, (want - self.offsets[sec]) / self.row))  # moment
                                  * self.row * min(1.0, speed) * phase)  # of the stretch animation

    def tap(self, x, y):
        self.input_gen += 1
        for n in reversed(self.nodes()):
            b = n["bounds"]
            if b[0] <= x < b[2] and b[1] <= y < b[3]:
                if n["id"].endswith("conversations_filter_debug_view_id_all"):
                    self.filter_all = True
                    return
                if n["id"].endswith("archived_row"):
                    self.section = "archived"
                    return
                if n["id"].endswith("contact_row_container") or n["id"].endswith("contact_name"):
                    row = (y - self.top + self.offsets[self.section]) // self.row
                    self.opened.append(self.items()[row])
                    return

    def key(self, k):
        self.input_gen += 1
        if k == "BACK" and self.section == "archived":
            self.section = "chats"

    def launch(self, pkg):
        self.section = "chats"

    def getprop(self, k):
        return {"ro.product.model": "Phone-" + str(self.h)}.get(k, "")

    def shell(self, *a, **k):
        cmd = " ".join(a)
        if "dumpsys window" in cmd:
            act = ("com.whatsapp.conversation.conversationslist.ArchivedConversationsActivity"
                   if self.section == "archived" else "com.whatsapp.home.ui.HomeActivity")
            return f"  mCurrentFocus=Window{{1 u0 com.whatsapp/{act}}}\n"
        return ""


def chats(n, prefix="Chat"):
    return [f"{prefix} {i:03d}" for i in range(1, n + 1)]


class RunTest(unittest.TestCase):
    """Drives the real RollingBatch over a FakeList; the per-chat export is scripted."""

    def setUp(self):
        self.clock = FakeClock()
        self.tmp = tempfile.mkdtemp()
        self.outcomes = {}                     # name -> list of statuses to return, in order
        self.exports = []                      # names actually exported (in order)
        wb._NODE_CACHE.update(adb=None, gen=None, t=0.0, nodes=None)
        for p in (mock.patch.object(wb, "time", self.clock),
                  mock.patch.object(wb, "keep_awake", lambda *a, **k: None),
                  mock.patch.object(wb, "pc_keep_awake", lambda *a, **k: None),
                  mock.patch.object(wb, "wake_unlock", lambda *a, **k: None),
                  mock.patch.object(wb, "screen_is_on", lambda *a, **k: True),
                  mock.patch.object(wb.RollingBatch, "_device_ready", lambda self: True),
                  mock.patch.object(wb, "export_with_retry", self.fake_export)):
            p.start()
            self.addCleanup(p.stop)

    def fake_export(self, adb, case, size, reopen, emit_log, stop, on_runner, pc, save, ensure,
                    pause=None):
        before = len(adb.opened)
        if not reopen() or len(adb.opened) == before:
            return "fail-notfound", None, None, ""
        name = adb.opened[-1]
        self.exports.append(name)
        script = self.outcomes.get(name)
        status = script.pop(0) if script else "ok"
        return status, (None if status != "ok" else os.path.join(self.tmp, name + ".zip")), \
            ("ab" * 32 if status == "ok" else None), "with media"

    def run_batch(self, phone, **kw):
        case = mock.Mock(dir=self.tmp)
        logs = []
        only = kw.pop("only_names", None)
        b = wb.RollingBatch(phone, case, (phone.w, phone.h),
                            lambda kind, *a: logs.append((kind, a)), save_dir=self.tmp, **kw)
        b.only_names = only
        b.run()
        return b, logs


class TestEveryChatIsExported(RunTest):
    def test_every_chat_on_every_screen_geometry(self):
        for geo in GEOMETRIES:
            with self.subTest(geo):
                self.exports.clear()
                phone = FakeList(chats(40), geometry=geo)
                b, _ = self.run_batch(phone)
                self.assertEqual(self.exports, chats(40))
                self.assertTrue(all(b.status[n] == "ok" for n in chats(40)))

    def test_a_run_started_with_the_list_scrolled_down_still_gets_the_top_chats(self):
        phone = FakeList(chats(40), start_offset=20 * 285)
        self.run_batch(phone)
        self.assertEqual(self.exports, chats(40))

    def test_fling_overshoot_never_skips_a_chat(self):
        for geo in GEOMETRIES:
            for overshoot in (1.3, 1.8):
                with self.subTest(geo=geo, overshoot=overshoot):
                    self.exports.clear()
                    self.run_batch(FakeList(chats(40), geometry=geo, overshoot=overshoot))
                    self.assertEqual(self.exports, chats(40))

    def test_ignored_swipes_are_not_mistaken_for_the_bottom(self):
        phone = FakeList(chats(40), swallow={3, 4, 5, 6})
        self.run_batch(phone)
        self.assertEqual(self.exports, chats(40))

    def test_archived_chats_are_exported_after_the_main_list(self):
        phone = FakeList(chats(12), archived=chats(9, "Old"))
        b, _ = self.run_batch(phone)
        self.assertEqual(self.exports, chats(12) + chats(9, "Old"))
        self.assertEqual({b.section_of[n] for n in chats(9, "Old")}, {"archived"})
        self.assertEqual({b.section_of[n] for n in chats(12)}, {"chats"})

    def test_a_filter_tab_left_on_does_not_hide_chats(self):
        phone = FakeList(chats(20), hidden_by_filter=chats(20)[5:])
        self.run_batch(phone)
        self.assertEqual(self.exports, chats(20))

    def test_a_chat_that_kept_failing_is_retried_at_the_end_of_the_run(self):
        self.outcomes = {"Chat 007": ["fail-transfer", "ok"]}
        phone = FakeList(chats(20), archived=chats(3, "Old"))
        b, _ = self.run_batch(phone)
        self.assertEqual(b.status["Chat 007"], "ok")
        self.assertEqual(self.exports.count("Chat 007"), 2)
        self.assertEqual(self.exports[-1], "Chat 007")

    def test_an_archived_chat_that_failed_is_retried_in_the_archive(self):
        self.outcomes = {"Old 002": ["fail-pcpick", "ok"]}
        phone = FakeList(chats(5), archived=chats(4, "Old"))
        b, _ = self.run_batch(phone)
        self.assertEqual(b.status["Old 002"], "ok")

    def test_privacy_blocked_chats_are_not_retried(self):
        self.outcomes = {"Chat 003": ["blocked-privacy"]}
        b, _ = self.run_batch(FakeList(chats(6)))
        self.assertEqual(self.exports.count("Chat 003"), 1)
        self.assertEqual(b.status["Chat 003"], "blocked-privacy")

    def test_the_csv_records_which_list_each_chat_came_from(self):
        b, _ = self.run_batch(FakeList(chats(3), archived=chats(2, "Old")))
        with open(b.csv_path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("list", text.splitlines()[0])
        self.assertIn("Old 001,ok", text)
        self.assertTrue(any(line.endswith(",archived") for line in text.splitlines()), text)


class TestSingleChat(RunTest):
    def test_exporting_one_chat_finds_it_and_stops(self):
        phone = FakeList(chats(80))
        b, _ = self.run_batch(phone, only_names={"Chat 012"})
        self.assertEqual(self.exports, ["Chat 012"])
        self.assertLess(phone.swipes, 15)

    def test_a_chosen_chat_is_exported_even_if_an_earlier_run_marked_it_ok(self):
        prior = os.path.join(self.tmp, "prior.csv")
        with open(prior, "w", encoding="utf-8") as f:
            f.write("#,chat,status,media,saved_file,sha256,time\n"
                    "1,Chat 001,ok,with media,a.zip,aa,t\n2,Chat 002,ok,with media,b.zip,bb,t\n")
        phone = FakeList(chats(10))
        b, _ = self.run_batch(phone, only_names={"Chat 002"}, resume_from=prior)
        self.assertEqual(self.exports, ["Chat 002"])
        self.assertEqual(b.status["Chat 001"], "ok")

    def test_exporting_one_archived_chat(self):
        phone = FakeList(chats(30), archived=chats(6, "Old"))
        self.run_batch(phone, only_names={"Old 005"})
        self.assertEqual(self.exports, ["Old 005"])


class TestSpeed(RunTest):
    def test_walking_a_long_list_needs_few_screen_reads(self):
        # 120 chats, none chosen for export: the old 0.12-screen swipe needed ~3 reads per chat; a
        # verified page-sized scroll needs about two per page (~5 chats).
        phone = FakeList(chats(120))
        self.outcomes = {}
        b, _ = self.run_batch(phone, only_names=set())
        self.assertLess(phone.dumps, 80)
        self.assertEqual(self.exports, [])


class TestScan(RunTest):
    def test_scan_lists_every_chat_from_anywhere_on_any_geometry(self):
        for geo in GEOMETRIES:
            with self.subTest(geo):
                phone = FakeList(chats(70), archived=chats(4, "Old"), geometry=geo, overshoot=1.5,
                                 start_offset=30 * GEOMETRIES[geo][2])
                names = wb.scan_chats(phone, (phone.w, phone.h))
                self.assertEqual(names, chats(70) + chats(4, "Old"))


class TestAndroidStretch(RunTest):
    def test_overscroll_stretch_at_the_ends_does_not_make_the_scroll_run_forever(self):
        for geo in GEOMETRIES:
            with self.subTest(geo):
                phone = FakeList(chats(30), archived=chats(9, "Old"), geometry=geo, stretch=True,
                                 start_offset=10 * GEOMETRIES[geo][2])
                names = wb.scan_chats(phone, (phone.w, phone.h))
                self.assertEqual(names, chats(30) + chats(9, "Old"))
                self.assertLess(phone.swipes, 80)

    def test_a_short_list_that_fits_one_screen_ends_quickly(self):
        phone = FakeList(chats(4), stretch=True)
        self.assertEqual(wb.scan_chats(phone, (phone.w, phone.h)), chats(4))
        self.assertLess(phone.swipes, 20)


class TestRightChat(unittest.TestCase):
    def setUp(self):
        wb._NODE_CACHE.update(adb=None, gen=None, t=0.0, nodes=None)
        for p in (mock.patch.object(wb, "time", FakeClock()),):
            p.start()
            self.addCleanup(p.stop)

    def test_a_tap_after_a_scroll_waits_for_the_list_to_settle(self):
        phone = FakeList(chats(30), stretch=True, start_offset=5 * 285)
        wb.scroll_to_end(phone, "up")
        node = wb._settled_node(phone, lambda ns: wb.core.find_node(ns, "Chat 003", "exact"))
        phone.tap(*wb.core.node_center(node))
        self.assertEqual(phone.opened, ["Chat 003"])

    def test_the_open_chat_title_must_match_before_exporting(self):
        def header(title):
            return [{"text": title, "desc": "", "id": W + "conversation_contact_name", "cls": "", "pkg": "",
                     "clickable": False, "bounds": (200, 150, 900, 220)},
                    {"text": "", "desc": "", "id": W + "conversation_root_layout", "cls": "", "pkg": "",
                     "clickable": False, "bounds": (0, 0, 1440, 3000)}]
        self.assertIs(wb.opened_chat_matches(header("Chat 003"), "Chat 003"), True)
        self.assertIs(wb.opened_chat_matches(header("Chat 004"), "Chat 003"), False)
        self.assertIs(wb.opened_chat_matches(header("‎Chat  003"), "Chat 003"), True)
        self.assertIsNone(wb.opened_chat_matches(header("Chat 003")[1:], "Chat 003"))   # no title shown


class TestChainOfCustody(unittest.TestCase):
    def test_a_received_export_outside_the_case_folder_is_hashed_into_the_case_log(self):
        root, inbox = tempfile.mkdtemp(), tempfile.mkdtemp()
        case = core.CaseLog(root, "SER123")
        f = Path(inbox) / "WhatsApp Chat with A.zip"
        f.write_bytes(b"PK-test")
        digest = case.record_file(f, "whatsapp export (with media) via Quick Share")
        rows = (case.dir / "hashes.csv").read_text(encoding="utf-8")
        self.assertIn(digest, rows)
        self.assertIn(str(f.resolve()), rows)      # the full path (never a short 8.3 alias)
        self.assertIn(digest, (case.dir / "session.log").read_text(encoding="utf-8"))


class TestOneCaseFolder(unittest.TestCase):
    """Everything for one session lands in that session's case folder."""

    def test_continuing_a_device_reads_the_old_csv_but_writes_into_the_new_case_folder(self):
        cases = tempfile.mkdtemp()
        old = Path(cases) / ("20261023_195808_" + core.device_tag("SER123"))
        old.mkdir()
        (old / "exported_chats.csv").write_text("#,chat,status\n1,A,ok\n", encoding="utf-8")
        new = Path(cases) / ("20261024_090000_" + core.device_tag("SER123"))
        new.mkdir()
        resume, out = wb.plan_csv(new, cases, "SER123")
        self.assertEqual(Path(resume), old / "exported_chats.csv")
        self.assertEqual(Path(out), new / "exported_chats.csv")

    def test_a_picked_csv_is_read_but_progress_is_written_to_the_current_case(self):
        cases, new = tempfile.mkdtemp(), Path(tempfile.mkdtemp())
        picked = Path(tempfile.mkdtemp()) / "exported_chats.csv"
        picked.write_text("#,chat,status\n1,A,ok\n", encoding="utf-8")
        resume, out = wb.plan_csv(new, cases, "SER123", picked=str(picked))
        self.assertEqual((Path(resume), Path(out)), (picked, new / "exported_chats.csv"))
        resume, out = wb.plan_csv(new, cases, "SER123", package="com.whatsapp.w4b")
        self.assertEqual(Path(out), new / "exported_chats_business.csv")

    def test_a_received_export_is_copied_into_the_case_folder_and_hashed(self):
        root, inbox = tempfile.mkdtemp(), Path(tempfile.mkdtemp())
        case = core.CaseLog(root, "SER123")
        f = inbox / "WhatsApp Chat with A.zip"
        f.write_bytes(b"PK-one")
        digest = core.sha256_file(f)
        copy = wb.keep_in_case(case, f, digest, "whatsapp export (with media) via Quick Share")
        self.assertEqual(copy, case.dir / "exports" / "WhatsApp Chat with A.zip")
        self.assertEqual(core.sha256_file(copy), digest)
        self.assertIn("exports/WhatsApp Chat with A.zip", (case.dir / "hashes.csv").read_text(encoding="utf-8"))
        f.write_bytes(b"PK-two")                       # a later export of the same chat: kept too
        copy2 = wb.keep_in_case(case, f, core.sha256_file(f), "again")
        self.assertNotEqual(copy2, copy)
        self.assertEqual(copy.read_bytes(), b"PK-one")

    def test_a_copy_that_does_not_match_the_received_hash_is_not_kept(self):
        case = core.CaseLog(tempfile.mkdtemp(), "SER123")
        f = Path(tempfile.mkdtemp()) / "x.zip"
        f.write_bytes(b"PK")
        self.assertIsNone(wb.keep_in_case(case, f, "0" * 64, "n"))
        self.assertFalse(any((case.dir / "exports").glob("*")) if (case.dir / "exports").exists() else False)


class TestOlderPhones(unittest.TestCase):
    def test_nearby_share_is_accepted_as_the_share_target_on_older_phones(self):
        nodes = [{"text": "Nearby Share", "desc": "", "id": "", "cls": "", "pkg": "", "clickable": True,
                  "bounds": (0, 2000, 300, 2200)}]
        self.assertIsNotNone(wb._share_target(nodes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
