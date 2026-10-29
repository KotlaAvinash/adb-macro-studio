# -*- coding: utf-8 -*-
"""Audit Phase 1 (findings A13, A01, A02, A10): transfer identity and evidence commit.

Each test is the audit's reproduced defect turned into an assertion of the CORRECTED contract, so
the defect cannot come back unnoticed. Standard library only - no device, no Quick Share, no GUI.
    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bt_transfer as bt          # noqa: E402
import whatsapp_batch as wb       # noqa: E402
from test_regressions import TRANSCRIPT, Case, Ev, FakeClock, mkzip, node   # noqa: E402


def _write(path, data):
    mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
    with open(path, mode) as f:
        f.write(data)


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _tile(name, b):
    return node(name, "", "device_name", b)


# ----------------------------------------------------------------------------- A13 recipient
class TestA13RecipientIdentity(unittest.TestCase):
    def pick(self, nodes, want="LAB"):
        with mock.patch.object(wb, "_nodes", lambda adb: nodes), \
                mock.patch.object(wb, "time", FakeClock()):
            return wb._wait_pc_node(None, want, timeout=2)

    def test_exact_name_is_accepted(self):
        self.assertIsNotNone(self.pick([_tile("LAB", (0, 100, 500, 200))]))

    def test_name_with_a_suffix_is_a_different_device(self):
        # audit: requested 'LAB' matched the sole tile 'LAB phone'
        self.assertIsNone(self.pick([_tile("LAB phone", (0, 100, 500, 200))]))
        self.assertIsNone(self.pick([_tile("LAB(other)", (0, 100, 500, 200))]))
        self.assertIsNone(self.pick([_tile("LABORATORY", (0, 100, 500, 200))]))

    def test_two_separate_tiles_with_the_same_name_fail_closed(self):
        # audit: two distinct 'LAB' tiles -> the first was returned
        self.assertIsNone(self.pick([_tile("LAB", (0, 100, 500, 200)),
                                     _tile("LAB", (0, 300, 500, 400))]))

    def test_one_tile_naming_itself_twice_is_still_one_tile(self):
        container = node("", "LAB", "tile", (0, 100, 500, 300))     # labelled container
        label = node("LAB", "", "name", (20, 120, 300, 160))         # its name text inside it
        self.assertIsNotNone(self.pick([container, label]))

    def test_case_and_spacing_do_not_matter(self):
        self.assertIsNotNone(self.pick([_tile(" lab ", (0, 100, 500, 200))]))


# ----------------------------------------------------------------------------- A01 validation
def _zip(path, members, compression=zipfile.ZIP_STORED):
    with zipfile.ZipFile(path, "w", compression) as z:
        for name, data in members:
            z.writestr(name, data)
    return path


class TestA01ExportValidation(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_valid_export_passes(self):
        p = _zip(os.path.join(self.d, "ok.zip"),
                 [("_chat.txt", TRANSCRIPT), ("IMG-1.jpg", b"\xff\xd8" + b"J" * 200)])
        info = bt.validate_export(p)
        self.assertTrue(info["ok"], info)
        self.assertEqual(info["label"], "with media")

    def test_corrupt_member_is_rejected(self):
        # audit: a byte flipped in a stored member (central directory intact) still returned ok
        payload = b"\xff\xd8" + b"PAYLOAD-BYTES" * 50
        p = _zip(os.path.join(self.d, "bad.zip"), [("_chat.txt", TRANSCRIPT), ("image.jpg", payload)])
        raw = bytearray(_read(p))
        i = raw.find(b"PAYLOAD-BYTES")
        raw[i] ^= 0xFF
        _write(p, raw)
        info = bt.validate_export(p)
        self.assertFalse(info["ok"])
        self.assertIn("image.jpg", info["reason"])

    def test_archive_without_a_transcript_is_rejected(self):
        # audit: a ZIP holding only 'invoice.bin' returned ok
        p = _zip(os.path.join(self.d, "x.zip"), [("invoice.bin", b"\x00" * 100)])
        self.assertFalse(bt.validate_export(p)["ok"])

    def test_text_file_without_dated_lines_is_not_a_transcript(self):
        p = _zip(os.path.join(self.d, "x.zip"), [("_chat.txt", "just some notes")])
        self.assertFalse(bt.validate_export(p)["ok"])

    def test_android_named_transcript_is_recognised(self):
        p = _zip(os.path.join(self.d, "a.zip"),
                 [("WhatsApp Chat with Bob.txt", "06/10/2026, 18:56 - Bob: hi\n")])
        info = bt.validate_export(p)
        self.assertTrue(info["ok"], info)
        self.assertEqual(info["label"], "without media")

    def test_attached_text_document_counts_as_media_not_transcript(self):
        p = _zip(os.path.join(self.d, "a.zip"),
                 [("_chat.txt", TRANSCRIPT), ("notes.txt", "a document the user sent")])
        info = bt.validate_export(p)
        self.assertEqual((info["chat_txt"], info["media"]), ("_chat.txt", 1))

    def test_truncated_and_non_zip_files_are_rejected(self):
        p = os.path.join(self.d, "t.zip")
        mkzip(p)
        data = _read(p)
        _write(p, data[: len(data) // 2])
        self.assertFalse(bt.validate_export(p)["ok"])
        q = os.path.join(self.d, "n.zip")
        _write(q, "not a zip")
        self.assertFalse(bt.validate_export(q)["ok"])

    def test_encrypted_member_is_rejected(self):
        # zipfile clears the encryption bit when writing, so set it in the central directory
        # (flag word at offset 8 of each PK record) - what infolist() reports
        p = _zip(os.path.join(self.d, "e.zip"), [("_chat.txt", TRANSCRIPT)])
        raw = bytearray(_read(p))
        cd = raw.find(b"PK")
        raw[cd + 8] |= 0x1
        _write(p, raw)
        with zipfile.ZipFile(p) as z:
            self.assertTrue(z.infolist()[0].flag_bits & 0x1)            # fixture really encrypted
        info = bt.validate_export(p)
        self.assertFalse(info["ok"])
        # rejected from metadata, BEFORE any member is read/decrypted
        self.assertEqual(info["reason"], "encrypted member")


# ----------------------------------------------------------------------------- A02 attribution
class TestA02ArrivalAttribution(unittest.TestCase):
    def test_filename_attribution(self):
        f = wb.belongs_to_chat
        self.assertTrue(f("WhatsApp Chat with Team A.zip", "Team A"))
        self.assertTrue(f("WhatsApp Chat with Team A (1).zip", "Team A"))     # Windows duplicate
        self.assertTrue(f("WhatsApp-Chat mit Team A.zip", "Team A"))          # localised prefix
        self.assertTrue(f("WhatsApp Chat with +91 98765 43210.zip", "+91 98765 43210"))
        self.assertFalse(f("WhatsApp Chat with Team Alpha.zip", "Team A"))    # previous chat's file
        self.assertFalse(f("unrelated.txt", "Team A"))
        self.assertIsNone(f("WhatsApp Chat with x.zip", "❤️👍"))              # can't attribute
        self.assertIsNone(f("WhatsApp Chat with x.zip", None))

    def watch(self, d, before, chat="Team A", nodes=()):
        with mock.patch.object(wb, "time", FakeClock()), \
                mock.patch.object(wb, "_nodes", lambda adb: list(nodes)):
            return wb._await_late_file(None, d, before, lambda m: None, Ev(), timeout=200,
                                       chat_name=chat)

    def test_unrelated_text_file_never_closes_the_chat(self):
        # audit: a late 'unrelated.txt' became a successful chat export
        d = tempfile.mkdtemp()
        before = bt.snapshot(d)
        _write(os.path.join(d, "unrelated.txt"), "not an export")
        self.assertIsNone(self.watch(d, before))

    def test_previous_chats_late_export_is_not_this_chats_evidence(self):
        d = tempfile.mkdtemp()
        before = bt.snapshot(d)
        mkzip(os.path.join(d, "WhatsApp Chat with Team Alpha.zip"))       # valid, wrong chat
        self.assertIsNone(self.watch(d, before))

    def test_the_matching_late_export_is_accepted_once(self):
        d = tempfile.mkdtemp()
        before = bt.snapshot(d)
        mkzip(os.path.join(d, "WhatsApp Chat with Team Alpha.zip"))       # ignored
        target = os.path.join(d, "WhatsApp Chat with Team A.zip")
        mkzip(target)
        res = self.watch(d, before)
        self.assertEqual(res[0], target)

    def test_two_candidate_exports_are_ambiguous_not_ok(self):
        d = tempfile.mkdtemp()
        before = bt.snapshot(d)
        mkzip(os.path.join(d, "WhatsApp Chat with Team A.zip"))
        mkzip(os.path.join(d, "WhatsApp Chat with Team A (1).zip"))
        self.assertEqual(self.watch(d, before), wb.AMBIGUOUS)

    def test_last_chance_catch_uses_the_same_gate(self):
        d = tempfile.mkdtemp()
        before = bt.snapshot(d)
        _write(os.path.join(d, "unrelated.txt"), "x")
        mkzip(os.path.join(d, "WhatsApp Chat with Someone Else.zip"))
        with mock.patch.object(wb, "time", FakeClock()):
            self.assertIsNone(wb._late_arrived_file(d, before, chat_name="Team A"))

    def test_retry_wrapper_never_reports_ok_for_an_unrelated_arrival(self):
        # the audit's end-to-end A02 probe: send 'fails', unrelated.txt lands, retries run
        inbox = tempfile.mkdtemp()

        def send(*a, **k):
            _write(os.path.join(inbox, "unrelated.txt"), "x")
            return "fail-transfer", None, None, "with media"
        with mock.patch.object(wb, "export_and_send", send), \
                mock.patch.object(wb, "time", FakeClock()), \
                mock.patch.object(wb, "_nodes", lambda adb: []):
            st = wb.export_with_retry(None, Case(), (1, 1), lambda: True, lambda m: None, None,
                                      None, "PC", inbox, lambda: None, chat_name="Team A")
        self.assertNotEqual(st[0], "ok")


# ----------------------------------------------------------------------------- A10 persistence
class TestA10TruthfulPersistence(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.src = os.path.join(self.d, "WhatsApp Chat with Team A.zip")
        mkzip(self.src)
        self.digest = bt.sha256(self.src)

    def test_hash_log_failure_is_not_retained_evidence(self):
        case = Case(os.path.join(self.d, "case"))
        case.record_file = mock.Mock(side_effect=OSError("disk full"))
        with mock.patch.object(wb, "time", FakeClock()):
            self.assertIsNone(wb._record_export(case, self.src, self.digest, "with media",
                                                lambda m: None))
        self.assertTrue(os.path.isfile(self.src))                 # received file untouched

    def test_failed_hash_log_of_the_received_file_is_not_hidden_by_a_good_copy(self):
        case = Case(os.path.join(self.d, "case"))
        src = self.src

        def record(p, note):
            if os.path.abspath(str(p)) == os.path.abspath(src):
                raise OSError("hash log locked")           # the received file's record fails
        case.record_file = mock.Mock(side_effect=record)    # ...the case copy's record succeeds
        with mock.patch.object(wb, "time", FakeClock()):
            self.assertIsNone(wb._record_export(case, src, self.digest, "with media",
                                                lambda m: None))

    def test_copy_hash_mismatch_is_not_retained_evidence(self):
        case = Case(os.path.join(self.d, "case"))
        with mock.patch.object(wb, "time", FakeClock()):
            self.assertIsNone(wb._record_export(case, self.src, "0" * 64, "with media",
                                                lambda m: None))

    def test_an_unrecorded_copy_is_not_left_behind(self):
        case = Case(os.path.join(self.d, "case"))
        case.record_file = mock.Mock(side_effect=OSError("locked"))     # recording the copy fails
        with self.assertRaises(OSError):
            wb.keep_in_case(case, self.src, self.digest, "n")
        self.assertEqual(os.listdir(os.path.join(case.dir, "exports")), [])

    def test_successful_retention_returns_the_case_copy(self):
        case = Case(os.path.join(self.d, "case"))
        kept = wb._record_export(case, self.src, self.digest, "with media", lambda m: None)
        self.assertTrue(str(kept).startswith(os.path.join(case.dir, "exports")))
        self.assertEqual(bt.sha256(kept), self.digest)

    def test_persistence_failure_is_reported_and_never_resent(self):
        # audit: case-log and case-copy failures still returned 'ok'
        calls = []

        def send(*a, **k):
            calls.append(1)
            return "fail-transfer", None, None, "with media"
        case = Case(os.path.join(self.d, "case"))
        case.record_file = mock.Mock(side_effect=OSError("access denied"))
        late = (self.src, os.path.getsize(self.src), self.digest)
        with mock.patch.object(wb, "export_and_send", send), \
                mock.patch.object(wb, "_await_late_file", lambda *a, **k: late), \
                mock.patch.object(wb, "time", FakeClock()):
            st = wb.export_with_retry(None, case, (1, 1), lambda: True, lambda m: None, None,
                                      None, "PC", self.d, lambda: None, chat_name="Team A")
        self.assertEqual(st[0], "fail-persist")
        self.assertEqual(len(calls), 1)                            # never re-exported for it

    def test_new_statuses_are_never_retried_by_resending(self):
        for s in ("fail-persist", "fail-ambiguous"):
            self.assertNotIn(s, wb.SHARE_RETRY_STATUSES)

    def test_csv_write_reports_its_outcome(self):
        ok_path = os.path.join(self.d, "c", "exported_chats.csv")
        self.assertIs(wb.write_csv_atomic(ok_path, [[1, "A", "ok", "", "", "", ""]]), True)
        blocker = os.path.join(self.d, "blocker")
        _write(blocker, "a file, not a folder")
        bad_path = os.path.join(blocker, "exported_chats.csv")
        self.assertIs(wb.write_csv_atomic(bad_path, [[1, "A", "ok", "", "", "", ""]]), False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
