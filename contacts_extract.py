"""
Extract the phone's contacts and the WhatsApp-registered contacts (with JID) from a LIVE,
NON-ROOTED device, via the Android ContactsProvider (`adb shell content query`).

Why this works without root: WhatsApp SYNCS its registered contacts into the Android
ContactsProvider under the `com.whatsapp` account (that's how WhatsApp names show up in the
dialer). The `shell` user that `adb` runs as can read the ContactsProvider, so we can pull:

  contacts.csv           every phone contact            -> name, number, normalized_number
  whatsapp_contacts.csv  WhatsApp-registered contacts   -> name, number, jid, jid_source

The JID is taken from the WhatsApp raw-contact sync field when present (authoritative), otherwise
DERIVED from the number as `<digits>@s.whatsapp.net` (marked jid_source=derived, since a number
stored without its country code would derive a wrong JID).

NOTE: this cannot read WhatsApp's private wa.db/msgstore.db from a live non-rooted phone (those
live in /data/data/com.whatsapp and need root). The raw query output is also saved next to the
CSVs so nothing is lost and the parsing can be verified against the exact device output.
"""
from __future__ import annotations

import csv
import os
import re

WA_ACCOUNT = "com.whatsapp"
_JID_RE = re.compile(r"\d+@(?:s\.whatsapp\.net|g\.us)")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _parse_rows(output: str, keys):
    """Parse `content query` text output. Each data row is one line:
        Row: 0 key1=val1, key2=val2, key3=val3
    Values may themselves contain commas and '=', so we locate each known key and take its value
    up to the start of the NEXT known key (`, <key>=`), using the projection's key set."""
    rows = []
    markers = [re.compile(r"(?:^|,\s*)" + re.escape(k) + r"=") for k in keys]
    for line in (output or "").splitlines():
        line = line.strip()
        if not line.startswith("Row:"):
            continue
        m = re.match(r"Row:\s*\d+\s+(.*)$", line)
        if not m:
            continue
        body = m.group(1)
        rec = {}
        for i, k in enumerate(keys):
            km = markers[i].search(body)
            if not km:
                rec[k] = ""
                continue
            start = km.end()
            nxt = len(body)
            for j, k2 in enumerate(keys):
                if k2 == k:
                    continue
                m2 = re.search(r",\s*" + re.escape(k2) + r"=", body[start:])
                if m2:
                    nxt = min(nxt, start + m2.start())
            rec[k] = body[start:nxt].strip()
        rows.append(rec)
    return rows


def _query(adb, uri, projection=None):
    """Run a content query; return the raw text. Never raises.

    NOTE: we deliberately do NOT pass a `--where` clause. `adb shell` space-joins its arguments and
    the DEVICE shell re-tokenizes them, so a selection like `mimetype LIKE '%com.whatsapp%'`
    (spaces + quotes) gets mangled and the query fails or returns wrong rows. Instead we pull the
    rows with a projection and filter in Python - robust across devices and quoting."""
    args = ["content", "query", "--uri", uri]
    if projection:
        args += ["--projection", ":".join(projection)]
    try:
        out = adb.shell(*args, quiet=True, timeout=120) or ""   # big contact tables can be slow
    except Exception as e:
        out = f"__error__: {e}"
    return out


def pull_phone_contacts(adb):
    """Every phone contact with a number: [{name, number, normalized}]. Also returns raw text."""
    raw = _query(adb, "content://com.android.contacts/data/phones",
                 projection=["display_name", "data1"])
    seen, out = set(), []
    for r in _parse_rows(raw, ["display_name", "data1"]):
        name, num = r.get("display_name", ""), r.get("data1", "")
        d = _digits(num)
        key = (name, d)
        if not d or key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "number": num, "normalized": d})
    return out, raw


def pull_whatsapp_contacts(adb):
    """WhatsApp-registered contacts: [{name, number, jid, jid_source}]. Also returns raw texts.

    Both queries are UNFILTERED (see _query) and filtered in Python:
      - data rows: keep those whose mimetype mentions com.whatsapp (the Message/Call action rows),
        which carry the registered number keyed by contact_id;
      - raw_contacts: keep those whose account_type is com.whatsapp; their sync fields may hold the
        explicit JID, keyed by contact_id."""
    DATA_KEYS = ["contact_id", "display_name", "data1", "mimetype"]
    RC_KEYS = ["contact_id", "display_name", "account_type", "sync1", "sync2", "sync3"]
    data_raw = _query(adb, "content://com.android.contacts/data", projection=DATA_KEYS)
    rc_raw = _query(adb, "content://com.android.contacts/raw_contacts", projection=RC_KEYS)

    # explicit JIDs from WhatsApp raw-contact sync fields, keyed by contact_id
    jid_by_cid, wa_rc = {}, []
    for r in _parse_rows(rc_raw, RC_KEYS):
        if WA_ACCOUNT not in (r.get("account_type", "") or ""):
            continue
        wa_rc.append(r)
        cid = r.get("contact_id", "")
        for f in ("sync1", "sync2", "sync3"):
            mm = _JID_RE.search(r.get(f, "") or "")
            if mm:
                jid_by_cid[cid] = mm.group(0)
                break

    seen, out = set(), []
    for r in _parse_rows(data_raw, DATA_KEYS):
        if WA_ACCOUNT not in (r.get("mimetype", "") or ""):
            continue                               # not a WhatsApp action row
        cid, name, num = r.get("contact_id", ""), r.get("display_name", ""), r.get("data1", "")
        d = _digits(num)
        jid = jid_by_cid.get(cid)
        src = "sync" if jid else "derived"
        if not jid:
            if not d:
                continue
            jid = d + "@s.whatsapp.net"
        if jid in seen:
            continue
        seen.add(jid)
        out.append({"name": name, "number": num, "jid": jid, "jid_source": src})

    # include WhatsApp contacts whose JID was only on raw_contacts (no action row)
    for r in wa_rc:
        jid = jid_by_cid.get(r.get("contact_id", ""))
        if jid and jid not in seen:
            seen.add(jid)
            out.append({"name": r.get("display_name", ""), "number": "",
                        "jid": jid, "jid_source": "sync"})
    return out, (data_raw, rc_raw)


def _write_csv(path, header, rows):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(header)
        for row in rows:
            wr.writerow(row)
    os.replace(tmp, path)


def _save_raw(path, text):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write(text or "")
    except Exception:
        pass


def query_status(raw):
    """What a `content query` actually returned:
      'ok'           - it returned rows;
      'empty'        - it ran and returned no rows ("No result found.");
      'error'        - it failed (adb error, permission denial, provider exception);
      'unrecognized' - no output we can interpret (not counted as a successful empty result).
    Zero contacts from a working query is NOT the same as a permission denial."""
    t = (raw or "").strip()
    if (t.startswith("__error__") or "Permission Denial" in t or "SecurityException" in t
            or "Exception" in t or t.lower().startswith("error")):
        return "error"
    if "Row:" in t:
        return "ok"
    if "No result found" in t:
        return "empty"
    return "unrecognized"


def extract_contacts(adb, out_dir, log=lambda m: None, case=None):
    """Pull phone contacts + WhatsApp contacts (with JID) into a NEW folder
    '<out_dir>/contacts_<time>_<id>' - an earlier collection is never overwritten, even when this
    one fails. Raw query output is always kept (it is the evidence of what the phone returned) and
    hash-logged alongside the CSVs. A CSV is written only for a query that ran: a failed query
    yields no CSV and an explicit 'error' status, never a misleading '0 contacts'.

    Returns {"contacts": n, "whatsapp": m, "files": [...], "dir": folder,
             "status": {query: ok|empty|error|unrecognized}, "ok": bool}"""
    import bt_transfer as bt
    acq = bt.new_acquisition_dir(str(out_dir), "contacts")
    result = {"contacts": 0, "whatsapp": 0, "files": [], "dir": acq, "status": {}, "ok": False}

    def _raw(name, text):
        p = os.path.join(acq, name)
        _save_raw(p, text)
        if os.path.isfile(p):
            result["files"].append(p)
        return p

    try:
        log("extracting phone contacts...")
        phones, praw = pull_phone_contacts(adb)
        raw_name = os.path.basename(_raw("contacts.raw.txt", praw))
        st = result["status"]["phone_contacts"] = query_status(praw)
        if st in ("ok", "empty"):
            p_csv = os.path.join(acq, "contacts.csv")
            _write_csv(p_csv, ["#", "name", "number", "normalized_number"],
                       [[i, c["name"], c["number"], c["normalized"]] for i, c in enumerate(phones, 1)])
            result["contacts"] = len(phones)
            result["files"].append(p_csv)
            log(f"phone contacts: {len(phones)} -> {os.path.basename(p_csv)}")
        else:
            log(f"phone contacts: QUERY {st.upper()} - no CSV written (see {raw_name}); "
                "earlier collections are untouched")
    except Exception as e:
        result["status"]["phone_contacts"] = "error"
        log(f"phone-contacts extraction failed: {e!r}")

    try:
        log("extracting WhatsApp contacts (with JID)...")
        wa, (draw, rraw) = pull_whatsapp_contacts(adb)
        _raw("whatsapp_contacts.data.raw.txt", draw)
        _raw("whatsapp_contacts.raw_contacts.raw.txt", rraw)
        s1 = result["status"]["whatsapp_data"] = query_status(draw)
        s2 = result["status"]["whatsapp_raw_contacts"] = query_status(rraw)
        if s1 in ("ok", "empty") and s2 in ("ok", "empty"):
            w_csv = os.path.join(acq, "whatsapp_contacts.csv")
            _write_csv(w_csv, ["#", "name", "number", "jid", "jid_source"],
                       [[i, c["name"], c["number"], c["jid"], c["jid_source"]]
                        for i, c in enumerate(wa, 1)])
            result["whatsapp"] = len(wa)
            result["files"].append(w_csv)
            n_auth = sum(1 for c in wa if c["jid_source"] == "sync")
            log(f"WhatsApp contacts: {len(wa)} ({n_auth} JIDs from sync, {len(wa) - n_auth} "
                f"derived) -> {os.path.basename(w_csv)}")
        else:
            log(f"WhatsApp contacts: QUERY FAILED (data={s1}, raw_contacts={s2}) - no CSV "
                "written; earlier collections are untouched")
    except Exception as e:
        result["status"].setdefault("whatsapp_data", "error")
        result["status"].setdefault("whatsapp_raw_contacts", "error")
        log(f"whatsapp-contacts extraction failed: {e!r}")

    result["ok"] = bool(result["status"]) and all(
        s in ("ok", "empty") for s in result["status"].values())
    if case is not None:
        from pathlib import Path
        for fp in result["files"]:
            try:
                case.record_file(Path(fp), "contacts extraction (adb content query)")
            except Exception as e:
                result["ok"] = False
                log(f"WARNING: could not hash-log {os.path.basename(fp)} ({e})")
    return result


if __name__ == "__main__":
    # CLI: python contacts_extract.py <serial-or-blank> <out_dir>  (uses adb_core.Adb)
    import sys
    import adb_core as core
    serial = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] not in ("", "-") else None
    out = sys.argv[2] if len(sys.argv) > 2 else "."
    adb = core.Adb(core.find_adb(), serial)
    print(extract_contacts(adb, out, log=print))
