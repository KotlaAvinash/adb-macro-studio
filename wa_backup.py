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
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import bt_transfer as bt

# WhatsApp's shared-storage locations (scoped-storage path first, then the legacy one).
WA_DIRS = ("/sdcard/Android/media/com.whatsapp/WhatsApp",
           "/sdcard/WhatsApp")
WA_BUSINESS_DIRS = ("/sdcard/Android/media/com.whatsapp.w4b/WhatsApp Business",
                    "/sdcard/WhatsApp Business")
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


def wa_dirs(package="com.whatsapp"):
    return WA_BUSINESS_DIRS if package == "com.whatsapp.w4b" else WA_DIRS


def wa_dir(adb, package="com.whatsapp"):
    return _first_existing_dir(adb, wa_dirs(package))


def _sh_quote(path):
    return "'" + path.replace("'", "'\\''") + "'"


def newest_backup(adb, package="com.whatsapp"):
    """Path of the newest msgstore*.crypt1x on the phone, or None. Read-only (ls)."""
    base = wa_dir(adb, package)
    if not base:
        return None
    dbdir = base + "/Databases"
    try:
        # one device-side command so the shell expands the glob and sorts by time (newest first)
        # quoted: WhatsApp Business's folder name has a space; the glob stays outside the quotes
        out = adb.shell("ls -t " + _sh_quote(dbdir) + "/msgstore*.crypt1* 2>/dev/null", quiet=True) or ""
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.endswith((".crypt15", ".crypt14", ".crypt12")):
            return line
    return None


def pull_backup(adb, dest_dir, emit=print, package="com.whatsapp"):
    """adb pull the newest backup into a NEW acquisition folder under dest_dir (never onto an earlier
    pull of the same file). Returns (local_path, sha256) or (None, None)."""
    remote = newest_backup(adb, package)
    if not remote:
        emit("no msgstore backup found on the phone (is WhatsApp installed / has it ever backed up?)")
        return None, None
    acq = bt.new_acquisition_dir(dest_dir, "backup")
    emit(f"pulling backup (read-only): {remote} -> {os.path.basename(acq)}")
    adb.pull(remote, Path(acq))
    local = os.path.join(acq, remote.rsplit("/", 1)[-1])
    if not os.path.isfile(local):
        emit("adb pull did not produce the backup file")
        return None, None
    digest = bt.sha256(local)
    emit(f"pulled {os.path.basename(local)} ({os.path.getsize(local)} bytes) sha256={digest}")
    return local, digest


def pull_media(adb, dest_dir, emit=print, package="com.whatsapp"):
    """adb pull the whole Media tree (can be large) into a NEW acquisition folder under dest_dir.
    Returns the local Media dir, or None."""
    base = wa_dir(adb, package)
    if not base:
        return None
    acq = bt.new_acquisition_dir(dest_dir, "media")
    emit(f"pulling media (read-only, may be large): {base}/Media -> {os.path.basename(acq)}")
    adb.pull(base + "/Media", Path(acq))
    local = os.path.join(acq, "Media")
    return local if os.path.isdir(local) else None


# --------------------------------------------------------------------------- decrypt
_HEX_KEY = re.compile(r"\b[0-9a-fA-F]{64}\b")


def _redact_keys(text):
    """Never forward a 64-hex backup key from decryptor output into logs."""
    return _HEX_KEY.sub("<key redacted>", text or "")


def _ro_uri(path):
    """A read-only SQLite URI for `path` with the filename correctly percent-encoded - a plain
    f'file:{path}' treats a '#' or '?' in a valid Windows filename as URI syntax."""
    return Path(os.path.abspath(path)).as_uri() + "?mode=ro"


def _looks_like_msgstore(path):
    """True if `path` opens as SQLite and has WhatsApp's message table (modern or legacy)."""
    try:
        con = sqlite3.connect(_ro_uri(path), uri=True)
        try:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(names & {"message", "messages"})


def _publish(src, directory, filename):
    """Move `src` to a name in `directory` that is NOT already taken and return it. Uses a hard link
    (fails if the target exists) so an existing file can never be replaced, even in a race; falls
    back to rename on filesystems without hard links."""
    while True:
        dest = _unique_path(directory, filename)
        try:
            os.link(src, dest)
            os.remove(src)
            return dest
        except FileExistsError:
            continue
        except OSError:
            if os.path.exists(dest):
                continue
            os.rename(src, dest)
            return dest


def decrypt_backup(crypt_path, key, out_db, emit=print, decrypt_timeout=1800):
    """Decrypt msgstore.db.crypt1x with wa-crypt-tools. `key` is a 64-hex string or a key file path.

    Returns the path of the NEW decrypted database, or None. `out_db` names the wanted file, but an
    existing file there is NEVER touched: the result is published as 'msgstore (1).db' etc. The
    decryptor writes into a private temporary folder owned by this attempt, the result is checked to
    be a WhatsApp database, and on failure, launch error or timeout (decrypt_timeout, default
    30 min) ONLY that temporary folder is removed - an earlier decrypted database is never deleted.
    """
    try:
        import wa_crypt_tools  # noqa: F401
    except Exception:
        emit("wa-crypt-tools is not installed (pip install wa-crypt-tools) - cannot decrypt")
        return None
    if os.path.abspath(crypt_path) == os.path.abspath(out_db):
        emit("refusing to decrypt: the output would overwrite the input backup")
        return None
    out_dir = os.path.dirname(os.path.abspath(out_db))
    os.makedirs(out_dir, exist_ok=True)
    work = tempfile.mkdtemp(prefix=".decrypt-", dir=out_dir)       # owned by this attempt only
    tmp_db = os.path.join(work, "msgstore.db")
    cmd = [sys.executable, "-m", "wa_crypt_tools.wadecrypt", key, crypt_path, tmp_db]
    emit("decrypting backup...")
    try:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=decrypt_timeout)
        except subprocess.TimeoutExpired:
            emit(f"decrypt timed out after {decrypt_timeout}s - aborting (nothing published)")
            return None
        except OSError as e:
            emit(f"could not start the decryptor: {e}")
            return None
        if r.returncode != 0 or not os.path.isfile(tmp_db):
            tail = _redact_keys((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-3:]
            emit("decrypt failed: " + " | ".join(tail))
            emit("  (a wrong key shows 'MAC check failed'; a corrupt/partial backup fails the same way)")
            return None
        if not _looks_like_msgstore(tmp_db):
            emit("decrypt produced a file that is not a WhatsApp database - not published")
            return None
        final = _publish(tmp_db, out_dir, os.path.basename(out_db))
        emit(f"decrypted -> {os.path.basename(final)} sha256={bt.sha256(final)}")
        return final
    finally:
        shutil.rmtree(work, ignore_errors=True)     # only this attempt's temp folder


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
    con = sqlite3.connect(_ro_uri(db_path), uri=True)
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
    locator = _MediaLocator(mroot) if mroot else None     # one name index for the whole run

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
                            copy_media, emit, locator=locator)
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
                 "media_files", "media_copied", "media_missing", "media_errors",
                 "first_utc", "last_utc", "first_ms", "last_ms", "zip", "sha256"]]
    for i, m in enumerate(sorted(manifests, key=lambda x: x["last_ms"] or 0, reverse=True), 1):
        rows_out.append([i, m["title"], m["number"], int(m["is_group"]), m["messages"], m["rows"],
                         m["media"], m["media_files"], m["media_copied"], m["media_missing"],
                         m["media_errors"], m["first_utc"], m["last_utc"], m["first_ms"],
                         m["last_ms"], m["zip"], m["sha256"]])
    _write_rows_atomic(man_csv, rows_out)
    # per-attachment record: which message referenced which source file, under which name inside
    # the zip, and whether it was copied (with its SHA-256), missing, ambiguous or failed.
    att_rows = [a for m in manifests for a in m["attachments"]]
    att_csv = None
    if att_rows:
        att_csv = _unique_path(out_dir, "backup_attachments.csv")
        _write_rows_atomic(att_csv, [["zip", "message_row_id", "source_path", "name_in_zip",
                                      "status", "sha256"]] + [list(a) for a in att_rows])
    missing = sum(m["media_missing"] for m in manifests)
    errors = sum(m["media_errors"] for m in manifests)
    emit(f"reconstructed {len(manifests)} chats -> {out_dir} "
         f"(manifest: {os.path.basename(man_csv)} sha256={bt.sha256(man_csv)})")
    if copy_media and mroot and (missing or errors):
        emit(f"media: {missing} attachment file(s) missing or ambiguous, {errors} copy error(s) - "
             f"see {os.path.basename(att_csv)}")
    return manifests


def _write_rows_atomic(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    os.replace(tmp, path)


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


def _archive_name(rel, base, taken):
    """A member name for ONE source file. It keeps its own file name unless a DIFFERENT source in the
    same chat already uses that name - compared Unicode-normalised and case-insensitively, so the
    names also stay distinct when extracted on Windows - in which case a short hash of its source
    path is appended: 'IMG-1~3f2a9c1b.jpg'. Two different files can no longer collapse into one."""
    name = os.path.basename(base or "") or "attachment"
    key = unicodedata.normalize("NFC", name).lower()
    if key in taken:
        stem, ext = os.path.splitext(name)
        h = hashlib.sha1((rel or name).encode("utf-8", "replace")).hexdigest()
        for n in range(8, 41, 4):
            name = f"{stem}~{h[:n]}{ext}"
            key = unicodedata.normalize("NFC", name).lower()
            if key not in taken:
                break
        else:
            i = 2
            while key in taken:
                name = f"{stem}~{h}-{i}{ext}"
                key = unicodedata.normalize("NFC", name).lower()
                i += 1
    taken.add(key)
    return name


def _write_chat_zip(out_dir, title, number, is_group, rows, res, media, mroot, owner,
                    copy_media, emit, locator=None):
    lines = []
    sources = {}                                  # source rel path -> (archive name, base)
    taken = set()
    refs = []                                     # (message_row_id, source rel path)
    first_ms = last_ms = None
    emitted = 0                                   # messages actually RENDERED into _chat.txt
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
            if rel not in sources:                # the same source file referenced again keeps
                sources[rel] = (_archive_name(rel, base, taken), base)   # the same archive name
            lines.append(f"{stamp} {who}: {_LTR}<attached: {sources[rel][0]}>")
            if caption or body:
                lines.append(caption or body)
            refs.append((_id, rel))
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
    outcome = {}                                  # source rel path -> (status, sha256 of source)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("_chat.txt", "\n".join(lines) + "\n")
        for rel, (arch, base) in sources.items():
            if not (copy_media and mroot):
                outcome[rel] = ("not acquired (text only)", "")
                continue
            src, how = (locator or _MediaLocator(mroot)).locate(rel, base)
            if not src:
                outcome[rel] = (how, "")          # 'missing' or 'ambiguous name' - never guessed
                continue
            try:
                z.write(src, arch)
                outcome[rel] = ("copied" if how == "found" else "copied (" + how + ")",
                                bt.sha256(src))
            except Exception as e:
                outcome[rel] = (f"copy error: {e}", "")

    def _iso(ms):
        d = _ts_utc(ms)
        return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else ""
    statuses = [s for s, _h in outcome.values()]
    zname = os.path.basename(zip_path)
    return {"title": title, "number": number, "is_group": is_group,
            "messages": emitted,                  # RENDERED messages, matches _chat.txt
            "rows": len(rows),                    # raw DB rows for this chat (incl. system/empty)
            "media": len(refs),                   # attachment REFERENCES in the transcript
            "media_files": len(sources),          # distinct source files referenced
            "media_copied": sum(1 for s in statuses if s.startswith("copied")),
            "media_missing": sum(1 for s in statuses if s in ("missing", "ambiguous name")),
            "media_errors": sum(1 for s in statuses if s.startswith("copy error")),
            "attachments": [(zname, mid, rel, sources[rel][0]) + outcome[rel] for mid, rel in refs],
            "first_utc": _iso(first_ms), "last_utc": _iso(last_ms),
            "first_ms": first_ms or "", "last_ms": last_ms or "",   # raw source epoch, never lost
            "zip": zname, "sha256": bt.sha256(zip_path)}


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


class _MediaLocator:
    """Finds the file behind an attachment. The stored path (message_media.file_path) is UNTRUSTED
    database content: absolute paths and '..' are rejected and the result must stay inside the media
    root. If the exact path is absent, a file is used by NAME only when exactly ONE file under Media
    has that name; several matches are 'ambiguous name' and are never guessed (a basename guess
    could attach a different person's photo). The name index is built once per reconstruction, not
    one folder walk per attachment."""

    def __init__(self, mroot):
        self.mroot = mroot
        self._index = None

    def _build(self):
        idx = {}
        mediadir = os.path.join(self.mroot, "Media")
        for root, _dirs, files in os.walk(mediadir if os.path.isdir(mediadir) else self.mroot):
            for f in files:
                idx.setdefault(f, []).append(os.path.join(root, f))
        self._index = idx

    def locate(self, rel, base):
        """(path, 'found' | 'found by name') or (None, 'missing' | 'ambiguous name')."""
        rel = (rel or "").replace("\\", "/")
        if rel and not rel.startswith("/") and ".." not in rel.split("/"):
            cand = os.path.join(self.mroot, *rel.split("/"))
            if os.path.isfile(cand) and _within(self.mroot, cand):
                return cand, "found"
        safe_base = os.path.basename(base or "")    # never let base carry a path component
        if not safe_base:
            return None, "missing"
        if self._index is None:
            self._build()
        hits = [h for h in self._index.get(safe_base, []) if _within(self.mroot, h)]
        if len(hits) == 1:
            return hits[0], "found by name"
        return None, ("ambiguous name" if hits else "missing")


def _find_media_file(mroot, rel, base):
    """The file for an attachment, or None (missing / ambiguous). See _MediaLocator."""
    return _MediaLocator(mroot).locate(rel, base)[0]


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
