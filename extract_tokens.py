"""
Extract Google master tokens + GSF Android ID from a rooted Android phone.

These are what `gpsoauth` (later step) needs to mint website session cookies for
a Google account, without needing the user's password or 2FA on the PC side.

Files read from the phone (all require root):
  - /data/system_ce/0/accounts_ce.db
      Android AccountManager "credential-encrypted" store. For every
      `com.google` account there is a `password` column that holds the master
      token (typically prefixed with `aas_et/`).
  - /data/data/com.google.android.gsf/databases/gservices.db
      Google Services Framework settings; contains the device-wide `android_id`
      that Google Play services uses to identify this device. gpsoauth needs
      this to sign the token-exchange request.

Output (when run with --save) is written to ./.tokens.json next to this file,
containing { android_id, accounts: [{email, master_token}, ...] }.

SECURITY: master tokens are effectively the keys to these Google accounts.
Treat .tokens.json like a password file. It is git-ignored by default.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from extract_offers import (
    ExtractError,
    adb,
    ensure_adb,
    ensure_device,
    ensure_root,
)

ACCOUNTS_DB_CANDIDATES = [
    "/data/system_ce/0/accounts_ce.db",
    "/data/system_de/0/accounts_de.db",
    "/data/system/users/0/accounts.db",
    "/data/data/com.google.android.gms/databases/accounts.db",
    "/data/user/0/com.google.android.gms/databases/accounts.db",
    "/data/data/com.google.android.gms/databases/auth.db",
    "/data/user/0/com.google.android.gms/databases/auth.db",
]
GOOGLE_ACCOUNT_TYPES = {
    "com.google",
    "com.google.work",
    "com.google.android.legacyimap",
}
GSERVICES_CANDIDATES = [
    "/data/data/com.google.android.gsf/databases/gservices.db",
    "/data/user/0/com.google.android.gsf/databases/gservices.db",
]

OAUTH_LOGIN_SUFFIX = ":oauth2:https://www.google.com/accounts/OAuthLogin"
SUBSCRIPTIONS_SUFFIX = ":oauth2:https://www.googleapis.com/auth/subscriptions"


def _remote_exists(serial: str | None, path: str) -> bool:
    r = adb(
        ["shell", "su", "-c", f"test -f '{path}' && echo OK || echo MISS"],
        serial=serial,
        check=False,
    )
    return "OK" in (r.stdout or "")


def pull_file(serial: str | None, remote: str, local: Path) -> Path | None:
    """Copy a root-owned file to /sdcard then adb pull. Returns local path or None."""
    if not _remote_exists(serial, remote):
        return None
    staged = f"/sdcard/tmp_pull_{Path(remote).name}"
    cp = adb(
        ["shell", "su", "-c", f"cp '{remote}' '{staged}' && chmod 644 '{staged}'"],
        serial=serial,
        check=False,
    )
    if cp.returncode != 0:
        return None
    local.parent.mkdir(parents=True, exist_ok=True)
    pulled = adb(["pull", staged, str(local)], serial=serial, check=False)
    adb(["shell", "rm", "-f", staged], serial=serial, check=False)
    if not local.exists() or local.stat().st_size == 0:
        sys.stderr.write(f"[!] pull failed for {remote}: {pulled.stderr.strip()}\n")
        return None
    return local


def pull_sqlite(serial: str | None, remote: str, local: Path) -> Path | None:
    """Pull a sqlite db and its -wal / -shm sidecars, so the DB is consistent."""
    main = pull_file(serial, remote, local)
    if not main:
        return None
    for suffix in ("-wal", "-shm"):
        pull_file(serial, remote + suffix, Path(str(local) + suffix))
    return main


def inspect_accounts_db(db_path: Path) -> dict:
    """Return schema + row-counts info for debugging."""
    info: dict = {"path": str(db_path), "tables": {}, "size": db_path.stat().st_size}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            entry: dict = {"columns": [], "row_count": None, "types": None}
            try:
                cur.execute(f"PRAGMA table_info({t})")
                entry["columns"] = [r[1] for r in cur.fetchall()]
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                entry["row_count"] = cur.fetchone()[0]
                if "type" in entry["columns"]:
                    cur.execute(f"SELECT DISTINCT type FROM {t}")
                    entry["types"] = [r[0] for r in cur.fetchall()]
            except sqlite3.DatabaseError as e:
                entry["error"] = str(e)
            info["tables"][t] = entry
    finally:
        con.close()
    return info


def read_accounts_and_tokens(
    db_path: Path,
    extra_types: set[str] | None = None,
) -> list[dict]:
    """Return [{email, type, master_token|None, oauth_login_token|None, subscriptions_token|None,
                  password_null, authtoken_count}] for each Google account in this DB."""
    types = set(GOOGLE_ACCOUNT_TYPES)
    if extra_types:
        types.update(extra_types)

    out: list[dict] = []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in cur.fetchall()}
        if "accounts" not in tables:
            return out

        cur.execute("PRAGMA table_info(accounts)")
        cols = [r[1] for r in cur.fetchall()]
        if not {"_id", "name", "type"}.issubset(cols):
            return out

        has_password = "password" in cols
        sel_cols = "_id, name, type" + (", password" if has_password else "")
        placeholders = ",".join("?" * len(types))
        cur.execute(
            f"SELECT {sel_cols} FROM accounts WHERE type IN ({placeholders})",
            tuple(types),
        )
        accounts_rows = cur.fetchall()

        auths_by_id: dict[int, dict[str, str]] = {}
        if "authtokens" in tables:
            cur.execute("PRAGMA table_info(authtokens)")
            at_cols = {r[1] for r in cur.fetchall()}
            if {"accounts_id", "type", "authtoken"}.issubset(at_cols):
                cur.execute("SELECT accounts_id, type, authtoken FROM authtokens")
                for acc_id, t, v in cur.fetchall():
                    if not v:
                        continue
                    auths_by_id.setdefault(acc_id, {})[t] = v

        for row in accounts_rows:
            if has_password:
                acc_id, name, acc_type, password = row
            else:
                acc_id, name, acc_type = row
                password = None

            tokens_for_acc = auths_by_id.get(acc_id, {})

            oauth_login = None
            subscriptions = None
            for t, v in tokens_for_acc.items():
                if t.endswith(OAUTH_LOGIN_SUFFIX) and oauth_login is None:
                    oauth_login = v
                elif t.endswith(SUBSCRIPTIONS_SUFFIX) and subscriptions is None:
                    subscriptions = v

            out.append({
                "email": name,
                "type": acc_type,
                "master_token": password if password else None,
                "oauth_login_token": oauth_login,
                "subscriptions_token": subscriptions,
                "password_null": not bool(password),
                "authtoken_count": len(tokens_for_acc),
                "_source_db": str(db_path),
            })
    finally:
        con.close()
    return out


def merge_accounts(existing: list[dict], new: list[dict]) -> list[dict]:
    """Merge new entries into existing list, keeping the strongest data per email."""
    by_email: dict[str, dict] = {a["email"]: a for a in existing}
    for n in new:
        e = by_email.get(n["email"])
        if not e:
            by_email[n["email"]] = dict(n)
            continue
        for k in ("master_token", "oauth_login_token", "subscriptions_token"):
            if not e.get(k) and n.get(k):
                e[k] = n[k]
                e["_source_db"] = n["_source_db"]
        e["authtoken_count"] = max(e.get("authtoken_count") or 0, n.get("authtoken_count") or 0)
        e["password_null"] = e.get("password_null", True) and n.get("password_null", True)
    return list(by_email.values())


def read_android_id(db_path: Path) -> str | None:
    """android_id is stored as an int64 hex string without 0x prefix in gservices.db."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        for tbl in tables:
            try:
                cur.execute(f"SELECT value FROM {tbl} WHERE name='android_id'")
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]
            except sqlite3.DatabaseError:
                continue
    finally:
        con.close()
    return None


def _preview(tok: str) -> str:
    if len(tok) <= 24:
        return "…"
    return f"{tok[:14]}…{tok[-6:]}"


def collect_tokens(serial: str | None, debug: bool = False, extra_types: set[str] | None = None) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix="gone_tokens_"))
    diag: dict = {"dbs_found": [], "dbs_missing": [], "per_db_counts": {}, "inspected": None}
    accounts: list[dict] = []
    try:
        for i, candidate in enumerate(ACCOUNTS_DB_CANDIDATES):
            local = pull_sqlite(serial, candidate, workdir / f"{i:02d}_{Path(candidate).name}")
            if not local:
                diag["dbs_missing"].append(candidate)
                continue
            diag["dbs_found"].append(candidate)
            try:
                new_rows = read_accounts_and_tokens(local, extra_types=extra_types)
            except sqlite3.DatabaseError as e:
                diag["per_db_counts"][candidate] = f"sqlite error: {e}"
                continue
            diag["per_db_counts"][candidate] = len(new_rows)
            if new_rows:
                accounts = merge_accounts(accounts, new_rows)
            if debug and diag["inspected"] is None:
                diag["inspected"] = inspect_accounts_db(local)

        if not diag["dbs_found"]:
            raise ExtractError(
                "Cannot pull any accounts DB. Tried:\n  "
                + "\n  ".join(ACCOUNTS_DB_CANDIDATES)
                + "\nMake sure the phone is unlocked and root is granted."
            )

        gsf_local = None
        for candidate in GSERVICES_CANDIDATES:
            gsf_local = pull_sqlite(serial, candidate, workdir / "gservices.db")
            if gsf_local:
                break
        if not gsf_local:
            raise ExtractError(
                "Could not find gservices.db. Paths tried:\n  "
                + "\n  ".join(GSERVICES_CANDIDATES)
            )

        android_id = read_android_id(gsf_local)
        if not android_id:
            raise ExtractError("gservices.db did not contain an android_id entry.")

        return {
            "android_id": android_id,
            "accounts": accounts,
            "_diag": diag,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--serial", help="adb device serial")
    ap.add_argument("--adb", help="path to adb executable (overrides PATH / ADB_PATH)")
    ap.add_argument(
        "--save",
        action="store_true",
        help="write results to ./.tokens.json (0600 perms)",
    )
    ap.add_argument(
        "--show-full",
        action="store_true",
        help="print full master tokens to stdout (DANGEROUS; for debugging only)",
    )
    ap.add_argument(
        "--debug",
        action="store_true",
        help="always print accounts DB schema / types for troubleshooting",
    )
    ap.add_argument(
        "--include-type",
        action="append",
        default=[],
        help="additional account type to accept (repeatable). Use with --debug first to find the right value.",
    )
    args = ap.parse_args()

    if args.adb:
        os.environ["ADB_PATH"] = args.adb

    try:
        ensure_adb()
        ensure_device(args.serial)
        ensure_root(args.serial)
    except ExtractError as e:
        print(str(e), file=sys.stderr)
        return 1

    try:
        data = collect_tokens(
            args.serial,
            debug=args.debug,
            extra_types=set(args.include_type) if args.include_type else None,
        )
    except ExtractError as e:
        print(str(e), file=sys.stderr)
        return 1

    print(f"Android ID (gservices): {data['android_id']}")
    diag = data.get("_diag") or {}
    print(f"Accounts DBs found:     {len(diag.get('dbs_found') or [])}")
    for db in diag.get("dbs_found") or []:
        count = diag.get("per_db_counts", {}).get(db)
        print(f"  - {db}   -> {count}")
    if args.debug:
        for db in diag.get("dbs_missing") or []:
            print(f"  - (missing) {db}")

    print(f"Google accounts found:  {len(data['accounts'])}")

    inspect = diag.get("inspected")
    if args.debug and inspect:
        print("\n--- DB inspection (first DB) ---")
        print(f"path: {inspect['path']}  size: {inspect['size']} bytes")
        for tbl, meta in inspect["tables"].items():
            print(f"  table {tbl!r}  rows={meta.get('row_count')}")
            print(f"    columns: {meta.get('columns')}")
            if meta.get("types") is not None:
                print(f"    distinct type values: {meta['types']}")
            if meta.get("error"):
                print(f"    error: {meta['error']}")
        print("--- end inspection ---\n")

    for a in data["accounts"]:
        mt = a.get("master_token")
        oauth = a.get("oauth_login_token")
        sub = a.get("subscriptions_token")

        tags = []
        if mt:
            tags.append("master_token")
        if oauth:
            tags.append("OAuthLogin")
        if sub:
            tags.append("subscriptions")
        if not tags:
            tags = ["NOTHING USABLE"]

        print(f"  - {a['email']:<38}  [{', '.join(tags)}]  (authtokens in DB: {a.get('authtoken_count')})")
        if args.show_full:
            if mt:
                print(f"      master_token       = {mt}")
            if oauth:
                print(f"      oauth_login_token  = {oauth}")
            if sub:
                print(f"      subscriptions_tok  = {sub}")
        else:
            if mt:
                print(f"      master_token       ≈ {_preview(mt)}")
            if oauth:
                print(f"      oauth_login_token  ≈ {_preview(oauth)}")
            if sub:
                print(f"      subscriptions_tok  ≈ {_preview(sub)}")

    if args.save:
        out = Path(__file__).parent / ".tokens.json"
        persisted = {"android_id": data["android_id"], "accounts": data["accounts"]}
        out.write_text(json.dumps(persisted, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(out, 0o600)
        except OSError:
            pass
        print(f"\nSaved to {out} (0600).  Treat this file like a password.")
    else:
        print("\nRun again with --save to persist to .tokens.json")

    return 0


if __name__ == "__main__":
    sys.exit(main())
