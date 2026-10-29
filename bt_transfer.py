"""
PC-side helper for Bluetooth (or any) file transfer: watch a destination folder and
detect when a new file has fully arrived (size stable), then hash it.

Runs on the PC (where the received files land). No adb here.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path


def snapshot(folder) -> dict:
    """Map current filename -> size for a folder (non-recursive)."""
    out = {}
    try:
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            if os.path.isfile(p):
                try:
                    out[name] = os.path.getsize(p)
                except OSError:
                    pass
    except FileNotFoundError:
        pass
    return out


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def wait_for_new_file(folder, before: dict, timeout=120, stable_secs=2.0, poll=0.5, abort=None,
                      confirm=None, fail_grace=10.0):
    """Wait until a NEW file (not in `before`) appears in `folder` and the transfer COMPLETES.
    Returns (path, size, sha256) or None on timeout.

    `before` is a snapshot() taken just before the transfer was triggered.
    `abort`, if given, is called each poll and returns "stop" (bail immediately), "failed" (the
    sender's screen shows a transfer 'Failed'), or falsey.
    `confirm`, if given, returns True once the phone's Quick Share screen shows the transfer
    'Completed'/'Done'. When the phone confirms Completed AND a file is present we accept it right
    away (that's the definitive done signal); otherwise we wait for the file size to stop growing
    for `stable_secs`. Either way we do NOT return mid-transfer.
    THE RECEIVED FILE ALWAYS WINS: a "failed" abort is honoured ONLY when no new file is arriving
    and after `fail_grace` seconds - the Quick Share screen lists ALL nearby devices, so a stray
    'Failed' from a DIFFERENT device must never cancel a transfer that actually succeeded."""
    folder = str(folder)
    start = time.time()
    deadline = start + timeout
    stable_since = {}
    last_size = {}

    def _finalize(name):
        path = os.path.join(folder, name)
        time.sleep(0.8)                        # let the last bytes flush before hashing
        try:
            return path, os.path.getsize(path), sha256(path)
        except OSError:
            return None

    while time.time() < deadline:
        now = snapshot(folder)
        got_new = False
        newest = None
        for name, size in now.items():
            if name in before and before[name] == size:
                continue                       # unchanged pre-existing file
            got_new = True                     # a new or growing file is present -> transfer alive
            newest = name
            if last_size.get(name) == size and size > 0:
                stable_since.setdefault(name, time.time())
                if time.time() - stable_since[name] >= stable_secs:
                    done = _finalize(name)
                    if done:
                        return done
            else:
                stable_since.pop(name, None)   # size changed -> reset stability timer
            last_size[name] = size
        # phone says the transfer COMPLETED and a file is here -> accept it now (definitive)
        if got_new and newest and now.get(newest, 0) > 0 and confirm is not None and confirm():
            done = _finalize(newest)
            if done:
                return done
        if abort is not None:
            a = abort()
            if a == "stop":
                return None                    # user stop -> immediate
            if a == "failed" and not got_new and (time.time() - start) > fail_grace:
                return None                    # real failure: no file arriving, grace elapsed
        time.sleep(poll)
    return None


_TEXT_EXTS = (".txt",)


def zip_media_info(path) -> dict:
    """Inspect a received WhatsApp export .zip to VERIFY its media content (authoritative over
    the UI-flow inference) and summarise what's inside. A "with media" export bundles media files
    next to _chat.txt; a "without media" export is just the _chat.txt.
    Returns {ok, files, txt, media, has_media, label, media_bytes, chat_txt} or {ok: False, error}."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
    except Exception as e:
        return {"ok": False, "error": str(e)}
    txt = [i for i in infos if i.filename.lower().endswith(_TEXT_EXTS)]
    media = [i for i in infos if not i.filename.lower().endswith(_TEXT_EXTS)]
    return {
        "ok": True,
        "files": len(infos),
        "txt": len(txt),
        "media": len(media),
        "has_media": bool(media),
        "label": "with media" if media else "without media",
        "media_bytes": sum(i.file_size for i in media),
        "chat_txt": txt[0].filename if txt else None,
    }


# Limits checked from ZIP metadata BEFORE anything is decompressed (zip-bomb / runaway guard).
EXPORT_MAX_MEMBERS = 100_000
EXPORT_MAX_TOTAL_BYTES = 8 * 1024 ** 3          # 8 GiB uncompressed in total
EXPORT_MAX_RATIO = 1000                         # per-member expansion ratio...
EXPORT_RATIO_MIN_BYTES = 100 * 1024 ** 2        # ...enforced only for members over 100 MiB
_TRANSCRIPT_PROBE_BYTES = 64 * 1024
# a WhatsApp transcript line carries a numeric date (any locale's digit order / separator)
_DATE_TOKEN = re.compile(r"\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}")


def _pick_transcript(z, infos):
    """The chat transcript member: `_chat.txt` (iOS / this tool's backup exports) if present, else
    the top-level .txt whose opening lines carry a message date. Other .txt files are attachments."""
    top = [i for i in infos if "/" not in i.filename and i.filename.lower().endswith(".txt")]
    named = [i for i in top if i.filename.lower() == "_chat.txt"]
    for i in named + [i for i in top if i not in named]:
        with z.open(i) as f:
            head = f.read(_TRANSCRIPT_PROBE_BYTES).decode("utf-8", "replace")
        if _DATE_TOKEN.search(head):
            return i
    return None


def validate_export(path) -> dict:
    """The ONE validator every receipt path uses (normal arrival, last-chance catch, late retry).

    A WhatsApp export counts as valid only when: it is a readable ZIP; no member is encrypted;
    member count and sizes are within limits (checked from metadata before decompressing); EVERY
    member decompresses with a correct CRC (`testzip`); and it holds a readable chat transcript.
    Only reading the ZIP index - the previous check - accepted archives with corrupt members and
    archives with no transcript at all.

    Returns the zip_media_info fields plus ok/reason. Validity does NOT prove the file belongs to
    the requested chat; callers attribute it separately."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            if not infos:
                return {"ok": False, "reason": "empty archive"}
            if len(infos) > EXPORT_MAX_MEMBERS:
                return {"ok": False, "reason": f"too many members ({len(infos)})"}
            if any(i.flag_bits & 0x1 for i in infos):
                return {"ok": False, "reason": "encrypted member"}
            total = sum(i.file_size for i in infos)
            if total > EXPORT_MAX_TOTAL_BYTES:
                return {"ok": False, "reason": f"uncompressed size {total} over limit"}
            for i in infos:
                if (i.file_size > EXPORT_RATIO_MIN_BYTES
                        and i.file_size > EXPORT_MAX_RATIO * max(i.compress_size, 1)):
                    return {"ok": False, "reason": f"member {i.filename!r} expands suspiciously"}
            bad = z.testzip()                     # reads every member, verifies its CRC
            if bad is not None:
                return {"ok": False, "reason": f"corrupt member {bad!r} (CRC mismatch)"}
            transcript = _pick_transcript(z, infos)
            if transcript is None:
                return {"ok": False, "reason": "no readable chat transcript"}
    except Exception as e:                        # truncated / not a zip / damaged headers
        return {"ok": False, "reason": f"not a readable zip ({e})"}
    media = [i for i in infos if i is not transcript]
    return {
        "ok": True,
        "reason": "",
        "files": len(infos),
        "txt": 1,
        "media": len(media),
        "has_media": bool(media),
        "label": "with media" if media else "without media",
        "media_bytes": sum(i.file_size for i in media),
        "chat_txt": transcript.filename,
    }


def default_bt_folders():
    """Likely Windows locations for received Bluetooth files (best-effort guesses)."""
    home = Path(os.path.expanduser("~"))
    return [str(home / "Documents"), str(home / "Downloads"), str(home)]


def _inspect_cli(target):
    """Print the media summary for one .zip, or every .zip in a folder."""
    import glob
    try:
        import sys as _sys
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    zips = [target] if target.lower().endswith(".zip") else sorted(glob.glob(os.path.join(target, "*.zip")))
    if not zips:
        print("no .zip files found at:", target); return
    print(f"{'file':50} {'label':13} {'media':>6} {'MB':>8}")
    print("-" * 82)
    for z in zips:
        info = zip_media_info(z)
        name = os.path.basename(z)[:48]
        if not info.get("ok"):
            print(f"{name:50} ERROR: {info.get('error')}"); continue
        print(f"{name:50} {info['label']:13} {info['media']:>6} {info['media_bytes']/1e6:>8.1f}")


if __name__ == "__main__":
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    # inspect mode: a .zip file, or a folder that contains .zip exports
    if arg.lower().endswith(".zip") or (arg and os.path.isdir(arg) and
                                        __import__("glob").glob(os.path.join(arg, "*.zip"))):
        _inspect_cli(arg)
    else:
        # default: watch a folder for 20s and report the first new stable file
        folder = arg or str(Path(os.path.expanduser("~")) / "Downloads")
        print("watching:", folder)
        base = snapshot(folder)
        res = wait_for_new_file(folder, base, timeout=20)
        print("result:", res)
