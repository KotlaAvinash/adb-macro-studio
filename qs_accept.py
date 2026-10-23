"""
PC-side helper: auto-click the Quick Share (Windows) 'Accept' button when an incoming file request
pops up. Uses UI Automation via pywinauto.

How the receiver window is identified (observed on the receiving PC): Google's Quick Share for
Windows runs as nearby_share.exe, and its top-level window has the UIA class
'Google.Windows.NearbyShare' and the title 'Quick Share'. The Accept button appears inside that
window only while a transfer request is pending.

The window is matched by that CLASS, or by an EXACT title - never by a substring. A substring match
once clicked inside a browser tab whose title happened to contain "quickshare". Only controls whose
label contains "accept" are clicked, so Quick Share's own settings (e.g. "Receive from") are never
touched.

If this does nothing: if the log says pywinauto could not load and you installed it while the app was
running, restart the app; otherwise run `pip install pywinauto`. You can always click Accept yourself.
"""
from __future__ import annotations

import os
import subprocess
import time

# Google Quick Share for Windows. Starting it again while it runs in the tray just brings its
# existing window forward (single instance) - observed on the receiving PC.
QS_EXE_PATHS = tuple(os.path.join(os.environ.get(v, ""), "Google", "NearbyShare", "nearby_share.exe")
                     for v in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(v))
QS_WINDOW_CLASSES = ("google.windows.nearbyshare",)   # Google Quick Share for Windows
QS_WINDOW_TITLES = ("quick share", "nearby share")    # exact (case-insensitive) top-level titles
ACCEPT_LABELS = ("accept",)


def _lc(s):
    return (s or "").strip().lower()


def _is_quick_share_window(w):
    try:
        cls = _lc(getattr(w.element_info, "class_name", ""))
        title = _lc(w.window_text())
    except Exception:
        return False
    return cls in QS_WINDOW_CLASSES or title in QS_WINDOW_TITLES


def _accept_control(w, labels):
    """The Accept control inside a Quick Share window (buttons first), or None."""
    def has_label(ctrl):
        try:
            text = _lc(ctrl.window_text())
        except Exception:
            return False
        return any(lbl in text for lbl in labels)
    for control_type in ("Button", None):
        try:
            ctrls = w.descendants(control_type=control_type) if control_type else w.descendants()
        except Exception:
            ctrls = []
        for c in ctrls:
            if has_label(c):
                return c
    return None


def _open_quick_share_window(log):
    """Quick Share minimized to the tray has no window, so its Accept button can't appear anywhere
    clickable. Starting the app again brings its existing window forward."""
    for exe in QS_EXE_PATHS:
        if os.path.isfile(exe):
            try:
                subprocess.Popen([exe])
            except OSError as e:
                log(f"Quick Share auto-accept: could not open the Quick Share window ({e})")
                return False
            log("Quick Share auto-accept: opened the Quick Share window (it was only in the tray)")
            return True
    return False


def accept_quickshare(timeout=45, poll=0.4, log=lambda m: None, labels=ACCEPT_LABELS) -> bool:
    """Wait up to `timeout`s for the Quick Share Accept control and click it. True if clicked."""
    labels = tuple(_lc(x) for x in labels)
    try:
        from pywinauto import Desktop
    except Exception as e:  # pragma: no cover
        log(f"AUTO-ACCEPT OFF: pywinauto could not load ({e}). If you installed it while this app "
            "was running, RESTART the app (a running Python cannot pick it up); otherwise run "
            "'pip install pywinauto'. Until then, click Accept on the PC yourself.")
        return False

    start = time.time()
    end = start + timeout
    reported = opened = False
    while time.time() < end:
        try:
            wins = Desktop(backend="uia").windows()
        except Exception:
            wins = []
        qs_windows = [w for w in wins if _is_quick_share_window(w)]
        if not qs_windows and not opened:
            opened = True
            _open_quick_share_window(log)
        for w in qs_windows:
            ctrl = _accept_control(w, labels)
            if ctrl is None:
                continue
            try:
                try:
                    ctrl.invoke()
                except Exception:
                    ctrl.click_input()
                log("clicked Accept in the Quick Share window")
                return True
            except Exception:
                pass
        if not reported and time.time() - start > 5:
            reported = True          # one diagnostic, without recording other apps' window titles
            log("Quick Share auto-accept: " + (
                "the Quick Share window is open but shows no Accept button yet"
                if qs_windows else
                "no Quick Share window is open on this PC (start Quick Share and sign in)"))
        time.sleep(poll)
    return False


if __name__ == "__main__":
    print("watching for a Quick Share Accept dialog for 45s...")
    print("accepted:", accept_quickshare(log=print))
