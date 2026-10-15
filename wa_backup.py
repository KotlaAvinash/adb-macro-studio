"""
Read-only WhatsApp backup extraction (the primary full-collection method).

WRITES NOTHING TO THE PHONE. It `adb pull`s the files WhatsApp already keeps on shared storage
(the encrypted `msgstore.db.crypt15` backup and the `Media` folder), decrypts the backup with the
device's 64-character backup key, and reconstructs every chat on the PC as a WhatsApp-export-style
`.zip` (`_chat.txt` in iOS format + its media). That output is identical to "Export chat (with
media)", so the existing offline viewer, the SHA-256 hashing and the CSV manifest all work unchanged.

Why read-only matters: on a seized phone the tool must never create/modify files (tampering) and
must not fill its storage. `adb pull` only reads.

Pipeline:
    pull_backup(adb, dest)          -> local msgstore.db.crypt15 (newest on the phone)
    pull_media(adb, dest)           -> local copy of the Media tree (optional, large)
    decrypt_backup(crypt15, key)    -> msgstore.db (plaintext sqlite)  [needs wa-crypt-tools]
    reconstruct(db, media, out_dir) -> one WhatsApp-export .zip per chat + backup_chats.csv

Freshness: the backup is only as current as WhatsApp's last on-phone backup. Using the existing file
writes nothing; tapping "Back up" first makes it current but is WhatsApp writing a new file - the
investigator's procedure decides which is acceptable.
"""
from __future__ import annotations

import csv
import os
import re
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import bt_transfer as bt

# WhatsApp's shared-storage locations (scoped-storage path first, then the legacy one).
WA_DIRS = ("/sdcard/Android/media/com.whatsapp/WhatsApp",
           "/sdcard/WhatsApp")
_LTR = "‎"                                   # iOS export prefixes attachment lines with this


# --------------------------------------------------------------------------- phone side (read-only)
def _first_existing_dir(adb, candidates):
    for d in candidates:
        try:
            out = adb.shell("ls", "-d", d, quiet=True) or ""
        except Exception:
            out = ""
        if d in out and "No such" not in out:
            return d
    return None


def wa_dir(adb):
    return _first_existing_dir(adb, WA_DIRS)


def newest_backup(adb):
    """Path of the newest msgstore*.crypt1x on the phone, or None. Read-only (ls)."""
    base = wa_dir(adb)
    if not base:
        return None
    dbdir = base + "/Databases"
    try:
        # one device-side command so the shell expands the glob and sorts by time (newest first)
        out = adb.shell("ls -t " + dbdir + "/msgstore*.crypt1* 2>/dev/null", quiet=True) or ""
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.endswith((".crypt15", ".crypt14", ".crypt12")):
            return line
    return None


def pull_backup(adb, dest_dir, emit=print):
    """adb pull the newest backup to dest_dir. Returns (local_path, sha256) or (None, None)."""
    remote = newest_backup(adb)
    if not remote:
        emit("no msgstore backup found on the phone (is WhatsApp installed / has it ever backed up?)")
        return None, None
    os.makedirs(dest_dir, exist_ok=True)
    emit(f"pulling backup (read-only): {remote}")
    adb.pull(remote, Path(dest_dir))
    local = os.path.join(dest_dir, remote.rsplit("/", 1)[-1])
    if not os.path.isfile(local):
        emit("adb pull did not produce the backup file")
        return None, None
    digest = bt.sha256(local)
    emit(f"pulled {os.path.basename(local)} ({os.path.getsize(local)} bytes) sha256={digest[:16]}...")
    return local, digest


def pull_media(adb, dest_dir, emit=print):
    """adb pull the whole Media tree (can be large). Returns the local Media dir, or None."""
    base = wa_dir(adb)
    if not base:
        return None
    os.makedirs(dest_dir, exist_ok=True)
    emit(f"pulling media (read-only, may be large): {base}/Media")
    adb.pull(base + "/Media", Path(dest_dir))
    local = os.path.join(dest_dir, "Media")
    return local if os.path.isdir(local) else None


# --------------------------------------------------------------------------- decrypt
def _rm(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def decrypt_backup(crypt_path, key, out_db, emit=print, decrypt_timeout=1800):
    """Decrypt msgstore.db.crypt1x with wa-crypt-tools. `key` is a 64-hex string or a key file path.
    Returns out_db on success, else None. A hung decryptor can't stall the workflow forever
    (decrypt_timeout, default 30 min); a failed/timed-out run leaves no partial output."""
    try:
        import wa_crypt_tools  # noqa: F401
    except Exception:
        emit("wa-crypt-tools is not installed (pip install wa-crypt-tools) - cannot decrypt")
        return None
    cmd = [sys.executable, "-m", "wa_crypt_tools.wadecrypt", key, crypt_path, out_db]
    emit("decrypting backup...")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=decrypt_timeout)
    except subprocess.TimeoutExpired:
        emit(f"decrypt timed out after {decrypt_timeout}s - aborting")
        _rm(out_db)                                # never leave a partial/ambiguous output
        return None
    if r.returncode != 0 or not os.path.isfile(out_db):
        tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
        emit("decrypt failed: " + " | ".join(tail))
        emit("  (a wrong key shows 'MAC check failed'; a corrupt/partial backup fails the same way)")
        _rm(out_db)
        return None
    emit(f"decrypted -> {os.path.basename(out_db)}")
    return out_db


# --------------------------------------------------------------------------- reconstruct
def _has_table(cur, name):
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None


def _cols(cur, table):
    return {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}


def _ts_utc(ms):
    """WhatsApp stores epoch milliseconds. Return a UTC datetime (NOT the analyst's local time - the
    source device timezone is unknown, so converting to the workstation's local zone would silently
    misstate the record). Reconstructed timestamps are therefore UTC; the manifest also keeps the raw
    epoch so the original value is never lost."""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except Exception:
        return None


def _safe_filename(s, maxlen=80):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s or "").strip().strip(".")
    return (s or "unknown")[:maxlen]


class _Resolver:
    """Resolves a jid row to a human name/number, following WhatsApp's LID privacy mapping."""
    def __init__(self, cur, contacts=None):
        self.contacts = {re.sub(r"\D", "", k): v for k, v in (contacts or {}).items()}
        self.jid = {}                 # _id -> (user, server, raw_string)
        for _id, user, server, raw in cur.execute("SELECT _id,user,server,raw_string FROM jid"):
            self.jid[_id] = (user or "", server or "", raw or "")
        self.lid_to_phone = {}        # lid jid-row -> phone jid-row
        if _has_table(cur, "jid_map"):
            for lid_rid, jid_rid in cur.execute("SELECT lid_row_id,jid_row_id FROM jid_map"):
                self.lid_to_phone[lid_rid] = jid_rid
        self.lid_name = {}            # lid jid-row -> display name
        if _has_table(cur, "lid_display_name"):
            for lid_rid, dn in cur.execute("SELECT lid_row_id,display_name FROM lid_display_name"):
                if dn:
                    self.lid_name[lid_rid] = dn

    def number(self, rid):
        user, server, _ = self.jid.get(rid, ("", "", ""))
        if server == "lid":
            prid = self.lid_to_phone.get(rid)
            if prid is not None:
                return self.jid.get(prid, ("", "", ""))[0] or user
        return user

    def display(self, rid):
        if rid is None:
            return "Unknown"
        num = self.number(rid)
        if num and num in self.contacts:
            return self.contacts[num]
        if rid in self.lid_name:
            return self.lid_name[rid]
        user, server, raw = self.jid.get(rid, ("", "", ""))
        if num:
            return "+" + num if num.isdigit() else num
        return raw or "Unknown"

    def is_group(self, rid):
        return self.jid.get(rid, ("", "", ""))[1] == "g.us"


def _media_index(cur):
    """message_row_id -> (basename, rel_path, caption) for messages that reference a stored file."""
    if not _has_table(cur, "message_media"):
        return {}
    cols = _cols(cur, "message_media")
    cap = "media_caption" if "media_caption" in cols else "NULL"
    out = {}
    for mid, fp, caption in cur.execute(
            f"SELECT message_row_id,file_path,{cap} FROM message_media WHERE file_path IS NOT NULL"):
        if fp:
            out[mid] = (fp.rsplit("/", 1)[-1], fp, caption or "")
    return out


def reconstruct(db_path, media_root, out_dir, contacts=None, owner="Me",
                only_numbers=None, copy_media=True, emit=print):
    """Rebuild every chat from a decrypted msgstore.db as a WhatsApp-export-style .zip in out_dir.
    `media_root` is the pulled Media dir (or the WhatsApp dir containing Media); None = text only.
    `only_numbers` limits to those phone numbers (digits). Returns a list of per-chat manifest dicts."""
    os.makedirs(out_dir, exist_ok=True)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.cursor()
    res = _Resolver(cur, contacts)
    media = _media_index(cur)

    # chat_row_id -> jid_row_id and subject (deleted chats may have messages but no chat row)
    chat_jid, chat_subject = {}, {}
    if _has_table(cur, "chat"):
        ccols = _cols(cur, "chat")
        subj = "subject" if "subject" in ccols else "NULL"
        for cid, jrid, subject in cur.execute(f"SELECT _id,jid_row_id,{subj} FROM chat"):
            chat_jid[cid] = jrid
            if subject:
                chat_subject[cid] = subject

    only = {re.sub(r"\D", "", n) for n in only_numbers} if only_numbers else None
    mroot = _resolve_media_root(media_root)

    # stream all messages grouped by chat (ordered), flush one .zip per chat
    q = """SELECT _id, chat_row_id, from_me, sender_jid_row_id, timestamp, message_type, text_data
           FROM message ORDER BY chat_row_id, timestamp, _id"""
    manifests, cur_cid, buf = [], None, []

    def flush(cid, rows):
        if not rows:
            return
        jrid = chat_jid.get(cid)
        number = res.number(jrid) if jrid is not None else ""
        if only is not None and number not in only:
            return
        is_group = (jrid is not None and res.is_group(jrid))
        title = chat_subject.get(cid) or (res.display(jrid) if jrid is not None else f"chat {cid}")
        m = _write_chat_zip(out_dir, title, number, is_group, rows, res, media, mroot, owner,
                            copy_media, emit)
        if m:
            manifests.append(m)

    for row in cur.execute(q):
        cid = row[1]
        if cid != cur_cid:
            flush(cur_cid, buf)
            cur_cid, buf = cid, []
        buf.append(row)
    flush(cur_cid, buf)
    con.close()

    # evidence manifest for the whole run. NEVER overwrite an existing manifest (it may be the
    # record of a prior collection), and write it atomically so a crash can't leave it half-written.
    man_csv = _unique_path(out_dir, "backup_chats.csv")
    rows_out = [["#", "chat", "number", "is_group", "messages", "rows", "media",
                 "first_utc", "last_utc", "first_ms", "last_ms", "zip", "sha256"]]
    for i, m in enumerate(sorted(manifests, key=lambda x: x["last_ms"] or 0, reverse=True), 1):
        rows_out.append([i, m["title"], m["number"], int(m["is_group"]), m["messages"], m["rows"],
                         m["media"], m["first_utc"], m["last_utc"], m["first_ms"], m["last_ms"],
                         m["zip"], m["sha256"]])
    tmp = man_csv + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows_out)
    os.replace(tmp, man_csv)
    emit(f"reconstructed {len(manifests)} chats -> {out_dir} "
         f"(manifest: {os.path.basename(man_csv)} sha256={bt.sha256(man_csv)[:16]}...)")
    return manifests


def _unique_path(directory, filename):
    """A path in `directory` that does not already exist: 'name.ext', then 'name (1).ext', ... so an
    existing file (e.g. a prior run's manifest - potential evidence) is never overwritten."""
    base, ext = os.path.splitext(filename)
    cand, n = os.path.join(directory, filename), 1
    while os.path.exists(cand):
        cand = os.path.join(directory, f"{base} ({n}){ext}")
        n += 1
    return cand


def _resolve_media_root(media_root):
    if not media_root:
        return None
    media_root = str(media_root)
    # accept either the Media dir itself or a parent that contains it
    if os.path.isdir(os.path.join(media_root, "Media")):
        return media_root
    if os.path.basename(media_root.rstrip("/\\")) == "Media":
        return os.path.dirname(media_root.rstrip("/\\"))
    return media_root


def _write_chat_zip(out_dir, title, number, is_group, rows, res, media, mroot, owner,
                    copy_media, emit):
    lines, attach = [], []
    first_ms = last_ms = None
    emitted = 0                                   # messages actually RENDERED into _chat.txt
    nmed = 0
    for _id, _cid, from_me, sender_rid, ts, mtype, text in rows:
        dt = _ts_utc(ts)
        if dt is None:
            continue
        who = owner if from_me else res.display(sender_rid)
        stamp = dt.strftime("[%d/%m/%Y, %H:%M:%S]")  # UTC
        body = (text or "").replace("\r\n", "\n").strip()
        mi = media.get(_id)
        if mi:
            base, rel, caption = mi
            lines.append(f"{stamp} {who}: {_LTR}<attached: {base}>")
            if caption or body:
                lines.append(caption or body)
            attach.append((base, rel))
            nmed += 1
        elif body:
            lines.append(f"{stamp} {who}: {body}")
        else:
            continue                              # system/empty row: NOT counted as a message
        emitted += 1
        if first_ms is None:
            first_ms = ts
        last_ms = ts
    if not lines:
        return None

    # append the phone number only for a 1:1 chat with a real digit number (a group's "number" is an
    # internal group id, not a phone number)
    suffix = f" ({number})" if (number and number.isdigit() and not is_group) else ""
    prefix = "WhatsApp Chat with " + _safe_filename(title + suffix)
    zip_path = os.path.join(out_dir, prefix + ".zip")
    n = 1
    while os.path.exists(zip_path):               # never overwrite existing evidence
        zip_path = os.path.join(out_dir, f"{prefix} ({n}).zip")
        n += 1
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("_chat.txt", "\n".join(lines) + "\n")
        if copy_media and mroot:
            seen = set()
            for base, rel in attach:
                if base in seen:
                    continue
                src = _find_media_file(mroot, rel, base)
                if src:
                    try:
                        z.write(src, base)
                        seen.add(base)
                    except Exception:
                        pass
    def _iso(ms):
        d = _ts_utc(ms)
        return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else ""
    return {"title": title, "number": number, "is_group": is_group,
            "messages": emitted,                  # RENDERED messages, matches _chat.txt
            "rows": len(rows),                    # raw DB rows for this chat (incl. system/empty)
            "media": nmed,
            "first_utc": _iso(first_ms), "last_utc": _iso(last_ms),
            "first_ms": first_ms or "", "last_ms": last_ms or "",   # raw source epoch, never lost
            "zip": os.path.basename(zip_path), "sha256": bt.sha256(zip_path)}


def _within(root, path):
    """True iff `path` (resolved, symlinks included) is inside `root`. Prevents a database-controlled
    file_path like '../../secret' or an absolute path from pulling a file OUTSIDE the media root into
    an evidence zip (traversal / local-file disclosure)."""
    try:
        root_r = os.path.realpath(root)
        path_r = os.path.realpath(path)
        return os.path.commonpath([root_r, path_r]) == root_r
    except (ValueError, OSError):      # ValueError: different drives on Windows
        return False


def _find_media_file(mroot, rel, base):
    """Locate a media file for an attachment. The stored `rel` (message_media.file_path) is UNTRUSTED
    DB content: reject absolute paths and any '..' traversal, and require the resolved file to stay
    inside the media root. Fallback search is confined to the Media subtree by basename only."""
    rel = (rel or "").replace("\\", "/")
    if rel and not rel.startswith("/") and ".." not in rel.split("/"):
        cand = os.path.join(mroot, *rel.split("/"))
        if os.path.isfile(cand) and _within(mroot, cand):
            return cand
    safe_base = os.path.basename(base or "")        # never let base carry a path component
    if not safe_base:
        return None
    mediadir = os.path.join(mroot, "Media")
    searchdir = mediadir if os.path.isdir(mediadir) else mroot
    for root, _dirs, files in os.walk(searchdir):
        if safe_base in files:
            hit = os.path.join(root, safe_base)
            if _within(mroot, hit):
                return hit
    return None


if __name__ == "__main__":
    # CLI: reconstruct from an ALREADY-decrypted msgstore.db (offline; no device needed)
    #   python wa_backup.py <msgstore.db> <out_dir> [media_root]
    import argparse
    ap = argparse.ArgumentParser(description="Reconstruct WhatsApp chats from a decrypted msgstore.db")
    ap.add_argument("db")
    ap.add_argument("out_dir")
    ap.add_argument("media_root", nargs="?", default=None)
    ap.add_argument("--no-media", action="store_true")
    ap.add_argument("--owner", default="Me")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    reconstruct(a.db, a.media_root, a.out_dir, owner=a.owner, copy_media=not a.no_media)
