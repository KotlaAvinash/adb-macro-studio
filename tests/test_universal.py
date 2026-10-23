# -*- coding: utf-8 -*-
"""Universal support: any phone language, any phone brand, WhatsApp Business.

The fake phone below mirrors UI trees recorded from a real phone (WhatsApp 2.26.41): the same
resource-ids in the same places. Only the visible LABELS are swapped for made-up words ("L-EXPORT",
"L-CLEAR", ...), standing in for a language the tool has never seen. Nothing here may depend on
English text.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import chat_viewer as cv         # noqa: E402
import wa_backup as wab          # noqa: E402
import whatsapp_batch as wb      # noqa: E402

WA = "com.whatsapp"
BIZ = "com.whatsapp.w4b"


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def strftime(self, fmt, *a):
        return "20261024_120000"


def n(rid, text="", desc="", b=(0, 0, 10, 10), go=None, clickable=True):
    return {"text": text, "desc": desc, "id": rid, "cls": "", "pkg": "", "clickable": clickable,
            "bounds": tuple(b), "go": go}


def W(rid):
    return "com.whatsapp:id/" + rid


def screens():
    """Recorded layout, made-up labels."""
    return {
        "list": [n(W("my_search_bar"), desc="L-SEARCH", b=(50, 400, 1390, 520)),
                 n(W("conversations_row_contact_name"), text="Chat A", b=(250, 700, 900, 760), go="chat"),
                 n(W("menuitem_overflow"), desc="L-OPTIONS", b=(1300, 150, 1400, 250))],
        "chat": [n(W("conversation_root_layout"), b=(0, 0, 1440, 3000), clickable=False),
                 n(W("menuitem_overflow"), desc="L-OPTIONS", b=(1300, 150, 1400, 250), go="menu")],
        "menu": [n(W("title"), text="L-SEARCH-IN-CHAT", b=(950, 200, 1300, 260), clickable=False),
                 n(W("title"), text="L-MEDIA", b=(950, 300, 1300, 360), clickable=False),
                 n("", desc="L-MORE-BUTTON", b=(900, 700, 1400, 800), go="submenu"),
                 n(W("title"), text="L-MORE", b=(950, 720, 1200, 780), clickable=False),
                 n(W("submenuarrow"), b=(1300, 720, 1350, 780), clickable=False)],
        "submenu": [n(W("title"), text="L-CLEAR", b=(950, 200, 1300, 260), go="clear_confirm"),
                    n(W("title"), text="L-EXPORT", b=(950, 300, 1300, 360), go="export_dialog"),
                    n(W("title"), text="L-SHORTCUT", b=(950, 400, 1300, 460), go="shortcut"),
                    n(W("title"), text="L-LIST", b=(950, 500, 1300, 560), go="addlist")],
        "export_dialog": [n("android:id/message", text="L-MEDIA-WARNING", b=(100, 1300, 1340, 1500),
                            clickable=False),
                          n("android:id/button3", text="L-WITHOUT", b=(200, 1600, 700, 1700), go="share"),
                          n("android:id/button1", text="L-INCLUDE", b=(800, 1600, 1300, 1700), go="share")],
        "share": [n("", text="Quick Share", b=(100, 2000, 300, 2200))],
        "too_big": [n("android:id/message", text="L-UNABLE", b=(100, 1300, 1340, 1500), clickable=False),
                    n("android:id/button1", text="L-OK", b=(800, 1600, 1300, 1700), go="chat")],
        "blocked": [n("android:id/message", text="L-CANT", b=(100, 1300, 1340, 1500), clickable=False),
                    n("android:id/button1", text="L-OK", b=(800, 1600, 1300, 1700), go="chat")],
        "anr": [n("android:id/aerr_close", text="L-CLOSE-APP", b=(100, 1500, 1300, 1600), go="closed"),
                n("android:id/aerr_wait", text="L-WAIT", b=(100, 1700, 1300, 1800), go="list")],
        "unreadable": [],
    }


class FakePhone:
    def __init__(self, start, locale="xx-YY", model="Phone-A", installed=(WA,)):
        self.screens, self.cur = screens(), start
        self.taps, self.keys, self.launched = [], [], []
        self.input_gen = 0
        self.props = {"persist.sys.locale": locale, "ro.product.model": model}
        self.installed = list(installed)
        self.focus = "com.whatsapp/com.whatsapp.Conversation"

    def nodes(self):
        return self.screens[self.cur]

    def tap(self, x, y):
        self.input_gen += 1
        hits = [m for m in self.nodes() if m["bounds"][0] <= x < m["bounds"][2]
                and m["bounds"][1] <= y < m["bounds"][3]]
        hits.sort(key=lambda m: (m["bounds"][2] - m["bounds"][0]) * (m["bounds"][3] - m["bounds"][1]))
        labelled = [m for m in hits if m["text"] or m["desc"]]
        self.taps.append((labelled[0]["text"] or labelled[0]["desc"]) if labelled else (x, y))
        target = next((m["go"] for m in hits if m.get("go")), None)
        if target:
            self.cur = target

    def key(self, k):
        self.input_gen += 1
        self.keys.append(k)

    def launch(self, pkg):
        self.launched.append(pkg)
        self.cur = "list"

    def getprop(self, k):
        return self.props.get(k, "")

    def packages(self, contains=""):
        return [p for p in self.installed if contains.lower() in p.lower()]

    def shell(self, *a, **k):
        cmd = " ".join(a)
        if "dumpsys window" in cmd:
            return f"  mCurrentFocus=Window{{1 u0 {self.focus}}}\n"
        if "dumpsys power" in cmd:
            return "mWakefulness=Awake"
        return ""


class PhoneTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.tmp = tempfile.mkdtemp()
        for p in (mock.patch.object(wb, "time", self.clock),
                  mock.patch.object(wb, "_nodes", lambda adb: adb.nodes()),
                  mock.patch.object(wb, "LABEL_FILE", os.path.join(self.tmp, "labels.json")),
                  mock.patch.object(wb, "label_asker", None)):
            p.start()
            self.addCleanup(p.stop)

    def export(self, phone):
        media = {"mode": ""}
        reason = wb._do_export(phone, media, lambda m: None, None, lambda: 1e9)
        return reason, media["mode"]


# --------------------------------------------------------------------------- any phone language
class TestAnyLanguage(PhoneTest):
    def test_chat_list_is_recognised_by_its_ids(self):
        self.assertIs(wb.on_chat_list(FakePhone("list")), True)

    def test_inside_a_chat_is_not_the_chat_list(self):
        self.assertIs(wb.on_chat_list(FakePhone("chat")), False)

    def test_export_reaches_the_share_sheet_with_a_learned_label(self):
        wb.learn_label(FakePhone("chat"), "export_chat", "L-EXPORT")
        phone = FakePhone("chat")
        self.assertEqual(self.export(phone), ("done", "with media"))
        self.assertEqual(phone.taps, ["L-OPTIONS", "L-MORE", "L-EXPORT", "L-INCLUDE"])

    def test_an_unknown_language_asks_the_examiner_once_and_remembers(self):
        asked = []

        def asker(key, options):
            asked.append((key, options))
            return "L-EXPORT"
        with mock.patch.object(wb, "label_asker", asker):
            self.assertEqual(self.export(FakePhone("chat"))[0], "done")
            self.assertEqual(self.export(FakePhone("chat"))[0], "done")
        self.assertEqual(asked, [("export_chat", ["L-CLEAR", "L-EXPORT", "L-SHORTCUT", "L-LIST"])])
        with open(wb.LABEL_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"export_chat": {"xx-YY": ["L-EXPORT"]}})

    def test_an_unknown_language_with_no_answer_taps_nothing_in_the_submenu(self):
        for asker in (None, lambda key, options: None):
            phone = FakePhone("chat")
            with mock.patch.object(wb, "label_asker", asker):
                self.assertEqual(self.export(phone)[0], "fail")
            self.assertFalse({"L-CLEAR", "L-EXPORT", "L-SHORTCUT", "L-LIST"} & set(phone.taps),
                             phone.taps)

    def test_an_answer_that_is_not_a_menu_item_is_never_learned(self):
        phone = FakePhone("chat")
        with mock.patch.object(wb, "label_asker", lambda key, options: "L-NOT-IN-MENU"):
            self.assertEqual(self.export(phone)[0], "fail")
        self.assertFalse(os.path.exists(wb.LABEL_FILE))

    def test_the_media_question_is_not_mistaken_for_a_one_button_notice(self):
        dialog = screens()["export_dialog"]
        self.assertIsNone(wb._notice_dialog(dialog))
        self.assertIsNotNone(wb._media_dialog(dialog))
        self.assertIsNone(wb._media_dialog(screens()["too_big"]))
        self.assertIsNotNone(wb._notice_dialog(screens()["too_big"]))

    def test_a_label_learned_for_one_language_is_not_used_for_another(self):
        wb.learn_label(FakePhone("chat"), "export_chat", "L-EXPORT")
        phone = FakePhone("chat", locale="zz-QQ")
        self.assertEqual(self.export(phone)[0], "fail")
        self.assertNotIn("L-EXPORT", phone.taps)

    def test_media_too_big_falls_back_to_without_media_by_button_ids(self):
        wb.learn_label(FakePhone("chat"), "export_chat", "L-EXPORT")
        phone = FakePhone("chat")
        phone.screens["export_dialog"][2]["go"] = "too_big"     # 'include media' is rejected
        self.assertEqual(self.export(phone), ("done", "without media"))
        self.assertEqual(phone.taps[3:], ["L-INCLUDE", "L-OK", "L-OPTIONS", "L-MORE",
                                          "L-EXPORT", "L-WITHOUT"])

    def test_export_blocked_by_chat_privacy_is_detected_without_english(self):
        wb.learn_label(FakePhone("chat"), "export_chat", "L-EXPORT")
        phone = FakePhone("chat")
        phone.screens["submenu"][1]["go"] = "blocked"
        self.assertEqual(self.export(phone)[0], "blocked")
        self.assertEqual(phone.taps[-1], "L-OK")

    def test_app_not_responding_dialog_is_answered_wait_by_id(self):
        phone = FakePhone("anr")
        self.assertTrue(wb.dismiss_anr(phone))
        self.assertEqual(phone.taps, ["L-WAIT"])


# --------------------------------------------------------------------------- WhatsApp Business
class TestWhatsAppBusiness(PhoneTest):
    def test_installed_whatsapp_apps_are_listed(self):
        self.assertEqual(wb.installed_whatsapp(FakePhone("list", installed=(BIZ,))), [BIZ])
        self.assertEqual(wb.installed_whatsapp(FakePhone("list", installed=(BIZ, WA))), [WA, BIZ])

    def test_getting_back_relaunches_the_app_being_exported(self):
        phone = FakePhone("unreadable")
        phone.wa_package = BIZ
        phone.focus = "com.whatsapp.w4b/com.whatsapp.Conversation"
        self.assertTrue(wb._back_to_list(phone, tries=3))
        self.assertEqual(phone.launched, [BIZ])

    def test_the_other_whatsapp_in_front_counts_as_a_different_app(self):
        phone = FakePhone("list")
        phone.wa_package = BIZ
        phone.focus = "com.whatsapp/com.whatsapp.home.ui.HomeActivity"
        self.assertIs(wb.on_whatsapp_foreground(phone), False)
        phone.focus = "com.whatsapp.w4b/com.whatsapp.home.ui.HomeActivity"
        self.assertIs(wb.on_whatsapp_foreground(phone), True)

    def test_business_and_whatsapp_progress_are_kept_in_separate_csvs(self):
        cases = tempfile.mkdtemp()
        case = os.path.join(cases, "20261024_120000_" + wb.core.device_tag("SER9"))
        os.makedirs(case)
        for name, ts in (("exported_chats.csv", 2_000), ("exported_chats_business.csv", 1_000)):
            with open(os.path.join(case, name), "w", encoding="utf-8") as f:
                f.write("#,chat\n1,x\n")
            os.utime(os.path.join(case, name), (ts, ts))
        self.assertEqual(os.path.basename(wb.latest_csv_for_serial(cases, "SER9", BIZ)),
                         "exported_chats_business.csv")
        self.assertEqual(os.path.basename(wb.latest_csv_for_serial(cases, "SER9")),
                         "exported_chats.csv")

    def test_a_business_run_writes_the_business_csv_and_drives_the_business_app(self):
        phone = FakePhone("list", installed=(BIZ,))
        case = mock.Mock(dir=self.tmp)
        run = wb.RollingBatch(phone, case, (1440, 3120), lambda *a: None, package=BIZ)
        self.assertEqual(os.path.basename(run.csv_path), "exported_chats_business.csv")
        self.assertEqual(phone.wa_package, BIZ)

    def test_business_backups_are_looked_for_in_the_business_folders(self):
        self.assertEqual(wab.wa_dirs(BIZ), ("/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business",
                                            "/sdcard/WhatsApp Business"))
        self.assertEqual(wab.wa_dirs(WA), ("/sdcard/Android/media/com.whatsapp/WhatsApp",
                                           "/sdcard/WhatsApp"))

    def test_backup_folder_with_a_space_is_quoted_for_the_phone_shell(self):
        cmds = []

        class Shell:
            def shell(self, *a, **k):
                cmds.append(" ".join(a))
                if a[:2] == ("ls", "-d"):
                    return a[2]
                return "/sdcard/WhatsApp Business/Databases/msgstore.db.crypt15\n"
        self.assertEqual(wab.newest_backup(Shell(), BIZ),
                         "/sdcard/WhatsApp Business/Databases/msgstore.db.crypt15")
        self.assertIn("'/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business/Databases'/msgstore*",
                      cmds[-1])


# --------------------------------------------------------------------------- any phone brand
class TestAnyBrand(PhoneTest):
    def test_a_learned_quick_share_position_belongs_to_one_phone_model(self):
        a, b = FakePhone("list", model="Phone-A"), FakePhone("list", model="Phone-B")
        self.assertNotEqual(wb._coord_key(a, (1080, 2400)), wb._coord_key(b, (1080, 2400)))
        self.assertEqual(wb._coord_key(a, (1080, 2400)), wb._coord_key(FakePhone("chat", model="Phone-A"),
                                                                        (1080, 2400)))

    def test_a_position_learned_before_this_change_moves_to_the_first_model_only(self):
        coords = {"1440x3120": {"qs": [229, 2908]}}
        with mock.patch.object(wb, "_COORDS", coords), mock.patch.object(wb, "_save_coords", lambda: None):
            self.assertEqual(wb._learned_coords(FakePhone("list", model="Phone-A"), (1440, 3120)),
                             {"qs": [229, 2908]})
            self.assertEqual(wb._learned_coords(FakePhone("list", model="Phone-B"), (1440, 3120)), {})
        self.assertNotIn("1440x3120", coords)


# --------------------------------------------------------------------------- viewer, any language
class TestViewerAnyLanguage(unittest.TestCase):
    def test_dates_in_other_layouts_and_day_period_words_are_parsed(self):
        text = ("2026-10-23 20:30 - Ana: uno\n"
                "23/10/2026, 8:30 अपराह्न - Ravi: दो\n"
                "2026. 10. 23. 오후 8:30 - Minsu: 셋\n"
                "23.10.26, 20:31 - Ana: vier\n")
        msgs = cv.parse_chat_text(text)
        self.assertEqual([(m["sender"], m["text"]) for m in msgs],
                         [("Ana", "uno"), ("Ravi", "दो"), ("Minsu", "셋"), ("Ana", "vier")])

    def test_attachments_are_found_by_their_file_name_in_any_language(self):
        text = ("23/10/2026, 20:30 - Ana: IMG-20261023-WA0001.jpg (L-FILE-ATTACHED)\n"
                "[23/10/2026, 20:31:00] Ana: <L-ATTACHED: PTT-20261023-WA0002.opus>\n")
        msgs = cv.parse_chat_text(text, media_names={"IMG-20261023-WA0001.jpg",
                                                     "PTT-20261023-WA0002.opus"})
        self.assertEqual([m["media_refs"] for m in msgs],
                         [["IMG-20261023-WA0001.jpg"], ["PTT-20261023-WA0002.opus"]])
        self.assertEqual([m["text"] for m in msgs], ["", ""])


if __name__ == "__main__":
    unittest.main(verbosity=2)
