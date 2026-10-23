"""
PC-side helper: auto-click the Quick Share (Windows) 'Accept' button when an incoming file request
pops up. Uses UI Automation via pywinauto.

If this does nothing, the usual cause is one of:
  1. pywinauto is not installed  ->  pip install pywinauto   (the log says 'AUTO-ACCEPT OFF' then)
  2. your Quick Share window/button is named differently than expected -> the diagnostic log line
     'Quick Share auto-accept: saw windows [...]' shows the real titles; add keywords below.
You can always just click Accept yourself; everything else still works.
"""
from __future__ import annotations

import time

# Substrings (lowercase) that identify the Quick Share receive window, and the Accept control.
WINDOW_HINTS = ("quick share", "quickshare", "nearby share", "nearby", "samsung", "직접 공유")
ACCEPT_LABELS = ("accept", "save", "receive", "allow", "수락", "받기")
# words that confirm a window is actually a file-transfer prompt (so we never click a stray 'Accept')
CONTEXT_HINTS = ("share", "quick", "nearby", "file", "receiv", "transfer", "wants to", "samsung")


def _lc(s):
    return (s or "").strip().lower()


def accept_quickshare(timeout=45, poll=0.4, log=lambda m: None, labels=ACCEPT_LABELS) -> bool:
    """Wait up to `timeout`s for the Quick Share Accept control and click it. True if clicked."""
    labels = tuple(_lc(x) for x in labels)
    try:
        from pywinauto import Desktop
    except Exception as e:  # pragma: no cover
        log(f"AUTO-ACCEPT OFF: pywinauto not available ({e}). Run 'pip install pywinauto', "
            "or click Accept on the PC yourself.")
        return False

    def accept_ctrl(text):
        t = _lc(text)
        return bool(t) and any(lbl in t for lbl in labels)

    seen_titles, reported = set(), False
    end = time.time() + timeout
    while time.time() < end:
        try:
            wins = Desktop(backend="uia").windows()
        except Exception:
            wins = []
        for w in wins:
            try:
                title = _lc(w.window_text())
            except Exception:
                continue
            if title:
                seen_titles.add(title[:40])
            # a window is a candidate if its TITLE looks like Quick Share, OR (fallback) it has an
            # Accept-looking control AND file-transfer context text - never a bare 'Accept' elsewhere.
            title_match = any(h in title for h in WINDOW_HINTS)
            try:
                descs = w.descendants()
            except Exception:
                descs = []
            if not title_match:
                blob = title + " " + " ".join(_lc(getattr(d, "window_text", lambda: "")())
                                              for d in descs[:60])
                if not (any(h in blob for h in CONTEXT_HINTS) and any(accept_ctrl(
                        _lc(getattr(d, "window_text", lambda: "")())) for d in descs)):
                    continue
            for d in descs:
                try:
                    if accept_ctrl(d.window_text()):
                        try:
                            d.invoke()
                        except Exception:
                            d.click_input()
                        log(f"clicked Accept on '{title[:40]}'")
                        return True
                except Exception:
                    pass
        if not reported and time.time() - (end - timeout) > 6:
            reported = True              # after ~6s with no hit, show what windows exist (diagnostic)
            log("Quick Share auto-accept: saw windows " + str(sorted(seen_titles))[:300]
                + " - if the Accept prompt is among them, tell me its title to add it")
        time.sleep(poll)
    return False


if __name__ == "__main__":
    print("watching for a Quick Share Accept dialog for 45s...")
    print("accepted:", accept_quickshare(log=print))
