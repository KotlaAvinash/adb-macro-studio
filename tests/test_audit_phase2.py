# -*- coding: utf-8 -*-
"""Audit Phase 2 (findings A04, A05, A06, A07): non-destructive acquisitions and derived media.

Each test is the audit's reproduced defect turned into an assertion of the CORRECTED contract.
Standard library only - no device, no real decryptor (a stand-in proves output ownership, which is
what A04 is about), no browser.
    python -m unittest discover -s tests -v
"""
import json
import os

import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.parse
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bt_transfer as bt          # noqa: E402
import chat_viewer as cv          # noqa: E402
import contacts_extract as ce     # noqa: E402
import wa_backup as wab           # noqa: E402

SENTINEL = b"PRE-EXISTING EVIDENCE DATABASE - MUST SURVIVE"


def _write(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def _make_msgstore(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE message(_id INTEGER PRIMARY KEY, text_data TEXT)")
    con.commit()
    con.close()


# ----------------------------------------------------------------------------- A04 decrypt
class TestA04DecryptNeverDestroysEvidence(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.crypt = os.path.join(self.d, "msgstore.db.crypt15")
        _write(self.crypt, b"encrypted-bytes")
        self.out = os.path.join(self.d, "msgstore.db")
        _write(self.out, SENTINEL)                       # an earlier decrypted database
        self.fake_tools = mock.patch.dict(sys.modules,
                                          {"wa_crypt_tools": types.ModuleType("wa_crypt_tools")})
        self.fake_tools.start()
        self.addCleanup(self.fake_tools.stop)

    def run_decrypt(self, behaviour):
        with mock.patch.object(wab.subprocess, "run", behaviour):
            return wab.decrypt_backup(self.crypt, "0" * 64, self.out, emit=lambda m: None)

    def assert_sentinel_intact(self):
        self.assertEqual(_read(self.out), SENTINEL)
        leftovers = [n for n in os.listdir(self.d) if n.startswith(".decrypt-")]
        self.assertEqual(leftovers, [], "the attempt's temp folder must be cleaned up")

    def test_failed_decrypt_leaves_existing_db_untouched(self):
        # audit: a failing decryptor that never touched the file still caused it to be deleted
        fail = lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "MAC check failed")
        self.assertIsNone(self.run_decrypt(fail))
        self.assert_sentinel_intact()

    def test_timeout_leaves_existing_db_untouched(self):
        def hang(cmd, **k):
            raise subprocess.TimeoutExpired(cmd, 1)
        self.assertIsNone(self.run_decrypt(hang))
        self.assert_sentinel_intact()

    def test_launch_error_leaves_existing_db_untouched(self):
        def broken(cmd, **k):
            raise OSError("python not found")
        self.assertIsNone(self.run_decrypt(broken))
        self.assert_sentinel_intact()

    def test_success_publishes_under_a_new_name(self):
        def ok(cmd, **k):
            _make_msgstore(cmd[-1])                      # decryptor writes ITS output path
            return subprocess.CompletedProcess(cmd, 0, "Done", "")
        got = self.run_decrypt(ok)
        self.assertTrue(got and os.path.basename(got) == "msgstore (1).db", got)
        self.assert_sentinel_intact()
        self.assertTrue(wab._looks_like_msgstore(got))

    def test_output_that_is_not_a_whatsapp_db_is_not_published(self):
        def garbage(cmd, **k):
            _write(cmd[-1], b"not sqlite")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        self.assertIsNone(self.run_decrypt(garbage))
        self.assert_sentinel_intact()
        self.assertEqual(sorted(os.listdir(self.d)), ["msgstore.db", "msgstore.db.crypt15"])

    def test_input_equal_to_output_is_refused(self):
        self.assertIsNone(wab.decrypt_backup(self.crypt, "0" * 64, self.crypt, emit=lambda m: None))
        self.assertEqual(_read(self.crypt), b"encrypted-bytes")

    def test_backup_key_is_never_forwarded_to_the_log(self):
        key = "ab" * 32
        logs = []
        leak = lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, f"root key: {key}", "")
        with mock.patch.object(wab.subprocess, "run", leak):
            wab.decrypt_backup(self.crypt, key, self.out, emit=logs.append)
        self.assertFalse(any(key in m for m in logs), logs)

    def test_hash_in_filename_works(self):
        # audit R08: a valid 'source#fragment.db' name broke the SQLite URI
        p = os.path.join(self.d, "case#7 msgstore.db")
        _make_msgstore(p)
        self.assertTrue(wab._looks_like_msgstore(p))


class TestA04PullsNeverOverwrite(unittest.TestCase):
    def test_repeated_backup_pulls_land_in_separate_acquisitions(self):
        d = tempfile.mkdtemp()
        remote = "/sdcard/Android/media/com.whatsapp/WhatsApp/Databases/msgstore.db.crypt15"
        payloads = iter([b"FIRST", b"SECOND"])

        class A:
            def pull(self, src, local_dir):
                _write(os.path.join(str(local_dir), src.rsplit("/", 1)[-1]), next(payloads))
        with mock.patch.object(wab, "newest_backup", lambda adb, pkg="com.whatsapp": remote):
            first, _ = wab.pull_backup(A(), d, emit=lambda m: None)
            second, _ = wab.pull_backup(A(), d, emit=lambda m: None)
        self.assertNotEqual(os.path.dirname(first), os.path.dirname(second))
        self.assertEqual((_read(first), _read(second)), (b"FIRST", b"SECOND"))

    def test_acquisition_dirs_are_unique_within_one_second(self):
        d = tempfile.mkdtemp()
        dirs = {bt.new_acquisition_dir(d, "x") for _ in range(50)}
        self.assertEqual(len(dirs), 50)


# ----------------------------------------------------------------------------- A05 contacts
class FakeContactsAdb:
    """Fake `adb shell content query`: rows, empty, or a permission failure."""
    def __init__(self, mode):
        self.mode = mode

    def shell(self, *args, **k):
        if self.mode == "error":
            return "Error while accessing provider:contacts\njava.lang.SecurityException: Permission Denial"
        if self.mode == "empty":
            return "No result found."
        uri = args[args.index("--uri") + 1]
        if uri.endswith("/phones"):
            return "Row: 0 display_name=Ramesh, data1=+91 98765 43210"
        if uri.endswith("/data"):
            return ("Row: 0 contact_id=11, display_name=Ramesh, data1=+91 98765 43210, "
                    "mimetype=vnd.android.cursor.item/vnd.com.whatsapp.profile")
        return ("Row: 0 contact_id=11, display_name=Ramesh, account_type=com.whatsapp, "
                "sync1=919876543210@s.whatsapp.net, sync2=, sync3=")


class RecordingCase:
    def __init__(self):
        self.recorded = []

    def record_file(self, p, note):
        self.recorded.append((str(p), bt.sha256(str(p))))


class TestA05ContactsKeepEarlierEvidence(unittest.TestCase):
    def test_failed_recollection_leaves_the_earlier_snapshot_valid(self):
        # audit: a failed repeat wrote header-only CSVs over the earlier files (old hash mismatch)
        d = tempfile.mkdtemp()
        case = RecordingCase()
        first = ce.extract_contacts(FakeContactsAdb("rows"), d, case=case)
        snapshot = {p: bt.sha256(p) for p in first["files"]}
        second = ce.extract_contacts(FakeContactsAdb("error"), d, case=case)
        self.assertNotEqual(first["dir"], second["dir"])
        for p, digest in snapshot.items():
            self.assertEqual(bt.sha256(p), digest, p)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(set(second["status"].values()), {"error"})

    def test_permission_denial_is_an_error_not_zero_contacts(self):
        res = ce.extract_contacts(FakeContactsAdb("error"), tempfile.mkdtemp())
        self.assertFalse(any(p.endswith(".csv") for p in res["files"]))   # no misleading CSV
        self.assertTrue(any(p.endswith(".raw.txt") for p in res["files"]))  # raw error kept

    def test_genuinely_empty_is_distinct_from_failure(self):
        res = ce.extract_contacts(FakeContactsAdb("empty"), tempfile.mkdtemp())
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"]["phone_contacts"], "empty")
        self.assertTrue(any(p.endswith("contacts.csv") for p in res["files"]))

    def test_raw_outputs_are_hash_logged_with_the_csvs(self):
        case = RecordingCase()
        res = ce.extract_contacts(FakeContactsAdb("rows"), tempfile.mkdtemp(), case=case)
        logged = {p for p, _ in case.recorded}
        self.assertEqual(logged, set(res["files"]))
        self.assertTrue(any(p.endswith(".raw.txt") for p in logged))

    def test_query_status_classification(self):
        s = ce.query_status
        self.assertEqual(s("Row: 0 a=b"), "ok")
        self.assertEqual(s("No result found."), "empty")
        self.assertEqual(s("__error__: adb died"), "error")
        self.assertEqual(s("java.lang.SecurityException: Permission Denial"), "error")
        self.assertEqual(s(""), "unrecognized")


# ----------------------------------------------------------------------------- A06 attachments
def _media_db(path, rows):
    """rows: (message_id, file_path) for one 1:1 chat."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE jid(_id INTEGER PRIMARY KEY, user TEXT, server TEXT, agent INT, device INT,
                         type INT, raw_string TEXT);
        CREATE TABLE chat(_id INTEGER PRIMARY KEY, jid_row_id INT, subject TEXT);
        CREATE TABLE message(_id INTEGER PRIMARY KEY, chat_row_id INT, from_me INT,
                             sender_jid_row_id INT, timestamp INT, message_type INT, text_data TEXT);
        CREATE TABLE message_media(message_row_id INT, chat_row_id INT, file_path TEXT,
                                   mime_type TEXT, media_caption TEXT);
        INSERT INTO jid VALUES(2,'919876543210','s.whatsapp.net',0,0,0,'919876543210@s.whatsapp.net');
        INSERT INTO chat VALUES(10,2,NULL);
    """)
    for i, (mid, fp) in enumerate(rows):
        con.execute("INSERT INTO message VALUES(?,?,?,?,?,?,?)",
                    (mid, 10, 0, 2, 1_700_000_000_000 + i * 1000, 1, None))
        con.execute("INSERT INTO message_media VALUES(?,?,?,?,?)", (mid, 10, fp, "image/jpeg", ""))
    con.commit()
    con.close()


class TestA06AttachmentIdentity(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.db = os.path.join(self.d, "msgstore.db")
        self.out = os.path.join(self.d, "out")

    def media(self, rel, data):
        _write(os.path.join(self.d, *rel.split("/")), data)

    def rebuild(self, copy_media=True):
        mans = wab.reconstruct(self.db, self.d, self.out, copy_media=copy_media,
                               emit=lambda m: None)
        self.assertEqual(len(mans), 1)
        m = mans[0]
        chat = cv.load_zip(os.path.join(self.out, m["zip"]), 0)          # embed mode
        payloads = [it["data"] for msg in chat["messages"] for it in msg["media"]]
        return m, payloads

    @staticmethod
    def decode(data_uri):
        import base64
        return base64.b64decode(data_uri.split(",", 1)[1]) if data_uri else None

    def test_same_named_files_keep_their_own_bytes(self):
        # audit: Media/A/same.jpg and Media/B/same.jpg collapsed into ONE archived payload
        self.media("Media/A/same.jpg", b"\xff\xd8FIRST-PHOTO")
        self.media("Media/B/same.jpg", b"\xff\xd8SECOND-PHOTO")
        _media_db(self.db, [(1, "Media/A/same.jpg"), (2, "Media/B/same.jpg")])
        m, payloads = self.rebuild()
        self.assertEqual([self.decode(p) for p in payloads],
                         [b"\xff\xd8FIRST-PHOTO", b"\xff\xd8SECOND-PHOTO"])
        self.assertEqual((m["media"], m["media_files"], m["media_copied"]), (2, 2, 2))

    def test_missing_media_is_counted_and_recorded(self):
        # audit: three references reported while only one file was archived; missing not recorded
        self.media("Media/A/a.jpg", b"\xff\xd8A")
        _media_db(self.db, [(1, "Media/A/a.jpg"), (2, "Media/A/gone.jpg")])
        m, _ = self.rebuild()
        self.assertEqual((m["media"], m["media_copied"], m["media_missing"]), (2, 1, 1))
        with open(os.path.join(self.out, "backup_attachments.csv"), encoding="utf-8") as f:
            rows = f.read()
        self.assertIn("Media/A/gone.jpg", rows)
        self.assertIn("missing", rows)

    def test_ambiguous_name_is_never_guessed(self):
        # exact path gone; two different files share the name elsewhere -> do not pick one
        self.media("Media/X/pic.jpg", b"\xff\xd8X")
        self.media("Media/Y/pic.jpg", b"\xff\xd8Y")
        _media_db(self.db, [(1, "Media/Old/pic.jpg")])
        m, payloads = self.rebuild()
        self.assertEqual((m["media_copied"], m["media_missing"]), (0, 1))
        self.assertEqual(payloads, [None])
        with open(os.path.join(self.out, "backup_attachments.csv"), encoding="utf-8") as f:
            self.assertIn("ambiguous name", f.read())

    def test_unique_name_fallback_still_finds_moved_media(self):
        self.media("Media/Moved/only.jpg", b"\xff\xd8ONLY")
        _media_db(self.db, [(1, "Media/Old/only.jpg")])
        m, payloads = self.rebuild()
        self.assertEqual(self.decode(payloads[0]), b"\xff\xd8ONLY")

    def test_repeated_reference_to_one_file_is_one_archived_file(self):
        self.media("Media/A/a.jpg", b"\xff\xd8A")
        _media_db(self.db, [(1, "Media/A/a.jpg"), (2, "Media/A/a.jpg")])
        m, payloads = self.rebuild()
        self.assertEqual((m["media"], m["media_files"]), (2, 1))
        with zipfile.ZipFile(os.path.join(self.out, m["zip"])) as z:
            self.assertEqual(len([n for n in z.namelist() if n != "_chat.txt"]), 1)

    def test_names_differing_only_in_case_stay_distinct(self):
        self.media("Media/A/photo.jpg", b"\xff\xd8lower")
        self.media("Media/B/PHOTO.jpg", b"\xff\xd8UPPER")
        _media_db(self.db, [(1, "Media/A/photo.jpg"), (2, "Media/B/PHOTO.jpg")])
        m, payloads = self.rebuild()
        with zipfile.ZipFile(os.path.join(self.out, m["zip"])) as z:
            names = [n for n in z.namelist() if n != "_chat.txt"]
        self.assertEqual(len({n.lower() for n in names}), 2)
        self.assertEqual([self.decode(p) for p in payloads], [b"\xff\xd8lower", b"\xff\xd8UPPER"])

    def test_text_only_mode_never_claims_media_was_acquired(self):
        self.media("Media/A/a.jpg", b"\xff\xd8A")
        _media_db(self.db, [(1, "Media/A/a.jpg")])
        m, _ = self.rebuild(copy_media=False)
        self.assertEqual((m["media"], m["media_copied"]), (1, 0))
        with open(os.path.join(self.out, "backup_attachments.csv"), encoding="utf-8") as f:
            self.assertIn("not acquired (text only)", f.read())


# ----------------------------------------------------------------------------- A07 viewer media
def _export(path, items):
    """An export zip whose transcript references each (name, bytes) attachment in order."""
    lines = [f"[06/10/2026, 18:56:{i:02d}] Alice: ‎<attached: {name}>"
             for i, (name, _b) in enumerate(items)]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("_chat.txt", "\n".join(lines) + "\n")
        for name, data in items:
            z.writestr(name, data)


class TestA07ViewerMediaIsNeverStale(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.inbox = os.path.join(self.d, "in")
        os.makedirs(self.inbox)
        self.page = os.path.join(self.d, "site", "index.html")
        os.makedirs(os.path.dirname(self.page))

    def build(self):
        return cv.build(self.inbox, self.page, 0, "", mode="folder")

    def media_bytes(self, page=None):
        """{chat name: [bytes of each attachment as the built page would load it]}"""
        if page is None:
            with open(self.page, encoding="utf-8") as f:
                page = f.read()
        marker = "window.__CHATS__ = "
        # decode exactly ONE JSON value - the page's own script follows it ("<\/" is valid JSON)
        data, _end = json.JSONDecoder().raw_decode(page, page.index(marker) + len(marker))
        site = os.path.dirname(self.page)
        out = {}
        for c in data["chats"]:
            out[c["name"]] = [_read(os.path.join(site, urllib.parse.unquote(it["src"])))
                              for m in c["messages"] for it in m["media"] if it.get("src")]
        return out

    def test_a_rebuild_never_changes_what_an_earlier_page_shows(self):
        # a page from an earlier build (e.g. a copy handed to a reviewer) must keep showing the
        # same media: a rebuild may add files but never overwrite media an existing page points at
        _export(os.path.join(self.inbox, "WhatsApp Chat with B.zip"), [("image.jpg", b"B-PHOTO")])
        self.build()
        with open(self.page, encoding="utf-8") as f:
            earlier_page = f.read()
        _export(os.path.join(self.inbox, "WhatsApp Chat with A.zip"), [("image.jpg", b"A-PHOTO")])
        self.build()
        self.assertEqual(self.media_bytes(earlier_page)["B"], [b"B-PHOTO"])

    def test_replacing_an_input_zip_shows_the_new_bytes(self):
        # audit: a rebuild into the same folder kept the OLD attachment bytes
        z = os.path.join(self.inbox, "WhatsApp Chat with A.zip")
        _export(z, [("image.jpg", b"OLD-BYTES")])
        self.build()
        _export(z, [("image.jpg", b"NEW-BYTES")])
        self.build()
        self.assertEqual(self.media_bytes()["A"], [b"NEW-BYTES"])

    def test_adding_a_zip_that_sorts_first_does_not_shift_media(self):
        # audit: media folders were numbered by sorted position, so an insertion re-pointed them
        _export(os.path.join(self.inbox, "WhatsApp Chat with B.zip"), [("image.jpg", b"B-PHOTO")])
        self.build()
        _export(os.path.join(self.inbox, "WhatsApp Chat with A.zip"), [("image.jpg", b"A-PHOTO")])
        self.build()
        got = self.media_bytes()
        self.assertEqual((got["A"], got["B"]), ([b"A-PHOTO"], [b"B-PHOTO"]))

    def test_partial_file_from_a_failed_write_is_repaired(self):
        # audit: after a write failed part-way, a retry skipped the existing partial file
        z = os.path.join(self.inbox, "WhatsApp Chat with A.zip")
        payload = os.urandom(65536)
        _export(z, [("big.jpg", payload)])
        self.build()
        site = os.path.dirname(self.page)
        media_file = [os.path.join(r, f) for r, _d, fs in os.walk(os.path.join(site, "media"))
                      for f in fs][0]
        _write(media_file, payload[:16])                 # what a disk-full write leaves behind
        self.build()
        self.assertEqual(self.media_bytes()["A"], [payload])

    def test_case_differing_names_in_one_archive_keep_their_payloads(self):
        # audit: photo.jpg and PHOTO.jpg both showed the first payload on Windows
        _export(os.path.join(self.inbox, "WhatsApp Chat with A.zip"),
                [("photo.jpg", b"first"), ("PHOTO.jpg", b"second")])
        self.build()
        self.assertEqual(self.media_bytes()["A"], [b"first", b"second"])

    def test_url_special_characters_in_names_resolve(self):
        _export(os.path.join(self.inbox, "WhatsApp Chat with A.zip"),
                [("my photo #1.jpg", b"hash-name")])
        self.build()
        self.assertEqual(self.media_bytes()["A"], [b"hash-name"])

    def test_one_corrupt_archive_does_not_lose_the_others(self):
        _export(os.path.join(self.inbox, "WhatsApp Chat with Good.zip"), [("ok.jpg", b"GOOD")])
        bad = os.path.join(self.inbox, "WhatsApp Chat with Bad.zip")
        _export(bad, [("x.jpg", b"PAYLOAD-BYTES" * 40)])
        raw = bytearray(_read(bad))
        raw[raw.find(b"PAYLOAD-BYTES")] ^= 0xFF          # CRC failure inside the member
        _write(bad, bytes(raw))
        self.assertEqual(self.build(), 2)                # reported as INCOMPLETE, not success
        self.assertEqual(self.media_bytes()["Good"], [b"GOOD"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
