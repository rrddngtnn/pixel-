"""
Extract Google One partner-offer links from a rooted Pixel.

Reads:
  /data/user/0/com.google.android.apps.subscriptions.red/files/accounts/<id>/
      SqliteKeyValueCache:PartnerOfferEligibilityCache.db

Requires:
  - ADB in PATH
  - Phone connected with USB debugging on
  - Root (Magisk/KernelSU) granting `su` to shell

Usage:
  python extract_offers.py
  python extract_offers.py --keep       # keep pulled db files for inspection
  python extract_offers.py --all-urls   # print every one.google.com link, not just offer links
  python extract_offers.py --serial XXX # target a specific adb device
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable


def adb_bin() -> str:
    """Return the adb executable to run. Honors ADB_PATH, .settings.json, then PATH."""
    custom = (os.environ.get("ADB_PATH") or "").strip().strip('"').strip("'")
    if custom:
        return custom
    settings_file = Path(__file__).parent / ".settings.json"
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
        configured = (data.get("adb_path") or "").strip().strip('"').strip("'")
        if configured and Path(configured).is_file():
            return configured
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return "adb"

PKG = "com.google.android.apps.subscriptions.red"
REMOTE_BASE = f"/data/user/0/{PKG}/files/accounts"
DB_NAME = "SqliteKeyValueCache:PartnerOfferEligibilityCache.db"

PARTNER_RE = re.compile(rb"https://one\.google\.com/partner-eft-onboard/[A-Za-z0-9_\-\.~:/?#\[\]@!$&'()*+,;=%]+")
ONE_RE = re.compile(rb"https://one\.google\.com/[A-Za-z0-9_\-\.~:/?#\[\]@!$&'()*+,;=%]+")
PARTNER_URL_PREFIX = "https://one.google.com/partner-eft-onboard/"
OFFER_URL_PREFIX = "https://one.google.com/offer/"


def adb(args: list[str], serial: str | None = None, binary: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    cmd = [adb_bin()]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    kwargs: dict = {"check": check}
    if binary:
        kwargs["capture_output"] = True
    else:
        kwargs["capture_output"] = True
        kwargs["text"] = True
        kwargs["encoding"] = "utf-8"
        kwargs["errors"] = "replace"
    return subprocess.run(cmd, **kwargs)


class ExtractError(RuntimeError):
    """Recoverable error raised by helpers; callers decide whether to exit or report."""


def has_adb() -> bool:
    binary = adb_bin()
    if binary != "adb":
        return Path(binary).is_file()
    return shutil.which("adb") is not None


def ensure_adb() -> None:
    if not has_adb():
        raise ExtractError("adb not found in PATH. Install Android platform-tools first.")


def list_devices() -> list[dict]:
    """Return [{serial, state}, ...] via `adb devices`."""
    if not has_adb():
        return []
    r = subprocess.run([adb_bin(), "devices"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "").splitlines()
    devices = []
    for line in out[1:]:
        parts = line.strip().split()
        if len(parts) >= 2:
            devices.append({"serial": parts[0], "state": parts[1]})
    return devices


def device_state(serial: str | None) -> str:
    r = adb(["get-state"], serial=serial, check=False)
    return (r.stdout or "").strip()


def ensure_device(serial: str | None) -> None:
    state = device_state(serial)
    if state != "device":
        raise ExtractError(f"No device ready (adb state: {state or 'unknown'}). Plug in phone and authorize USB debugging.")


def check_root(serial: str | None) -> tuple[bool, str]:
    r = adb(["shell", "su", "-c", "id"], serial=serial, check=False)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return ("uid=0" in out, out)


def ensure_root(serial: str | None) -> None:
    ok, out = check_root(serial)
    if not ok:
        raise ExtractError(
            "Root (su) is not available to adb shell.\n"
            "On Magisk/KernelSU: open the manager app and grant root to 'Shell'.\n"
            f"su output: {out}"
        )


def list_account_dirs(serial: str | None) -> list[str]:
    """List account subdirectories, sorted with the highest numeric folder first.

    The app writes caches into /files/accounts/<N>/ where <N> is a per-session
    sequence number. Only the largest <N> has live data; older folders are
    historical. Non-numeric names (if any) go after, lexically sorted.
    """
    r = adb(["shell", "su", "-c", f"ls '{REMOTE_BASE}'"], serial=serial, check=False)
    if r.returncode != 0:
        raise ExtractError(f"Could not list {REMOTE_BASE}. Is Google One installed and signed in?\n{r.stderr}")
    names = [d.strip() for d in (r.stdout or "").splitlines() if d.strip()]

    def sort_key(name: str) -> tuple:
        try:
            return (0, -int(name), name)
        except ValueError:
            return (1, 0, name)

    names.sort(key=sort_key)
    return names


def pull_db(serial: str | None, account: str, dest_dir: Path) -> Path | None:
    """Copy the cache DB (with : in the name) to /sdcard/ then adb pull it locally."""
    remote = f"{REMOTE_BASE}/{account}/{DB_NAME}"

    check = adb(["shell", "su", "-c", f"test -f '{remote}' && echo OK || echo MISS"], serial=serial, check=False)
    if "OK" not in (check.stdout or ""):
        return None

    staged = f"/sdcard/offer_cache_{account}.db"
    adb(["shell", "su", "-c", f"cp '{remote}' '{staged}' && chmod 644 '{staged}'"], serial=serial, check=False)

    local = dest_dir / f"offer_cache_{account}.db"
    pulled = adb(["pull", staged, str(local)], serial=serial, check=False)
    adb(["shell", "rm", "-f", staged], serial=serial, check=False)

    if not local.exists() or local.stat().st_size == 0:
        print(f"[!] pull failed for account {account}: {pulled.stderr.strip()}", file=sys.stderr)
        return None
    return local


def normalize_offer_url(url: str) -> str:
    """Convert Google One partner onboarding URLs to the public offer URL shape."""
    if url.startswith(PARTNER_URL_PREFIX):
        return OFFER_URL_PREFIX + url[len(PARTNER_URL_PREFIX):]
    return url


def to_partner_url(url: str) -> str:
    """Convert Google One public offer URLs to the partner-eft-onboard URL shape."""
    if url.startswith(OFFER_URL_PREFIX):
        return PARTNER_URL_PREFIX + url[len(OFFER_URL_PREFIX):]
    return url


def format_offer_url(url: str, url_format: str = "offer") -> list[str]:
    """Return URL(s) formatted according to url_format ('offer', 'partner', or 'both')."""
    offer_u = normalize_offer_url(url)
    partner_u = to_partner_url(url)
    if url_format == "partner":
        return [partner_u]
    if url_format == "both":
        return [offer_u, partner_u] if offer_u != partner_u else [offer_u]
    return [offer_u]


def reset_google_one_session(serial: str | None = None) -> str:
    """Force-stop and clear Google One app data on the rooted device between accounts."""
    ensure_adb()
    ensure_device(serial)
    ensure_root(serial)
    cmd = f"am force-stop {shlex.quote(PKG)} && pm clear {shlex.quote(PKG)}"
    r = adb(["shell", "su", "-c", cmd], serial=serial, check=False)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0 or ("Success" not in out and out):
        raise ExtractError(f"Failed to reset {PKG}: {out or r.returncode}")
    return out or "Success"


def _clean_urls(urls: Iterable[str], url_format: str = "offer") -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        stripped = raw.rstrip(".,);:\"'")
        for u in format_offer_url(stripped, url_format=url_format):
            if u not in seen:
                seen.add(u)
                cleaned.append(u)
    return cleaned


def extract_urls_from_bytes(
    data: bytes,
    pattern: re.Pattern[bytes],
    url_format: str = "offer",
) -> list[str]:
    return _clean_urls(
        (m.group(0).decode("utf-8", errors="replace") for m in pattern.finditer(data)),
        url_format=url_format,
    )


def scan_urls_on_device(
    serial: str | None,
    account: str,
    pattern: re.Pattern[bytes],
    url_format: str = "offer",
) -> list[str] | None:
    """Fast path: scan the root-owned cache DB on-device and return only matching URL strings.

    Pulling the whole DB over adb is much slower than letting Android's toybox
    `strings` emit printable chunks and transferring only lines containing
    one.google.com. The final URL extraction still uses the same local regex.
    Returns None when the cache DB does not exist.
    """
    remote = f"{REMOTE_BASE}/{account}/{DB_NAME}"

    check = adb(["shell", "su", "-c", f"test -f {shlex.quote(remote)} && echo OK || echo MISS"], serial=serial, check=False)
    if "OK" not in (check.stdout or ""):
        return None

    cmd = f"/system/bin/strings -n 8 {shlex.quote(remote)} 2>/dev/null | /system/bin/grep 'one.google.com' 2>/dev/null || true"
    r = adb(["shell", "su", "-c", cmd], serial=serial, check=False)
    out = r.stdout or ""
    if r.returncode != 0:
        err = ((r.stderr or "") + (r.stdout or "")).strip()
        raise ExtractError(f"On-device scan failed: {err}")
    return extract_urls_from_bytes(out.encode("utf-8", errors="replace"), pattern, url_format=url_format)


def extract_urls_from_db(
    db_path: Path,
    pattern: re.Pattern[bytes],
    url_format: str = "offer",
) -> list[str]:
    """Scan all TEXT/BLOB values for URLs. Values are usually proto-serialized, so regex on raw bytes is most reliable."""
    urls: list[str] = []

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cur.fetchall()]
        for t in tables:
            try:
                cur.execute(f'SELECT * FROM "{t}"')
                for row in cur.fetchall():
                    for val in row:
                        blob = val if isinstance(val, (bytes, bytearray)) else (str(val).encode("utf-8") if val is not None else b"")
                        for m in pattern.finditer(blob):
                            urls.append(m.group(0).decode("utf-8", errors="replace"))
            except sqlite3.DatabaseError:
                continue
        con.close()
    except sqlite3.DatabaseError:
        pass

    data = db_path.read_bytes()
    for m in pattern.finditer(data):
        urls.append(m.group(0).decode("utf-8", errors="replace"))

    return _clean_urls(urls, url_format=url_format)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", help="adb device serial (when multiple devices)")
    ap.add_argument("--adb", help="path to adb executable (overrides PATH / ADB_PATH)")
    ap.add_argument("--keep", action="store_true", help="keep pulled *.db files instead of deleting them")
    ap.add_argument("--all-urls", action="store_true", help="match any https://one.google.com/... url")
    ap.add_argument("--all-accounts", action="store_true",
                    help="scan every numbered account folder (default: only the newest, which is where the cache lives)")
    ap.add_argument("--format", choices=("offer", "partner", "both"), default="both",
                    help="output URL shape: 'offer' (/offer/...), 'partner' (/partner-eft-onboard/...), or 'both' (default: both)")
    ap.add_argument("--reset-google-one", action="store_true",
                    help="force-stop and clear Google One app data on the device (useful after extracting an account)")
    ap.add_argument("--out-dir", default=None, help="directory to place pulled db files (default: temp)")
    ap.add_argument("--pull-db", action="store_true",
                    help="force the old adb-pull + local sqlite/raw scan path (slower, useful for debugging)")
    args = ap.parse_args()

    if args.adb:
        os.environ["ADB_PATH"] = args.adb

    try:
        ensure_adb()
        ensure_device(args.serial)
        ensure_root(args.serial)
        if args.reset_google_one:
            msg = reset_google_one_session(args.serial)
            print(f"[+] Reset {PKG}: {msg}")
            return 0
    except ExtractError as e:
        sys.exit(str(e))

    pattern = ONE_RE if args.all_urls else PARTNER_RE

    try:
        accounts = list_account_dirs(args.serial)
    except ExtractError as e:
        sys.exit(str(e))
    if not accounts:
        sys.exit(f"No accounts found under {REMOTE_BASE}. Sign into Google One on the phone first.")

    if not args.all_accounts:
        print(f"[i] Scanning newest account dir only: {accounts[0]} (use --all-accounts to scan all)")
        accounts = accounts[:1]
    print(f"[*] Found {len(accounts)} account dir(s): {', '.join(accounts)}")

    use_pull_scan = args.pull_db or args.keep or bool(args.out_dir)
    if use_pull_scan and args.out_dir:
        workdir = Path(args.out_dir).resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        cleanup = False
    elif use_pull_scan:
        workdir = Path(tempfile.mkdtemp(prefix="gone_offers_"))
        cleanup = not args.keep
    else:
        workdir = None
        cleanup = False

    try:
        all_urls: list[tuple[str, str]] = []
        if use_pull_scan:
            print("[i] Using adb pull scan (slower, pulls the full DB for local inspection)")
        else:
            print("[i] Using fast on-device scan (strings/grep, no DB pull)")
        for acc in accounts:
            if use_pull_scan:
                db = pull_db(args.serial, acc, workdir)
                if not db:
                    print(f"[-] account {acc}: no cache db")
                    continue
                urls = extract_urls_from_db(db, pattern, url_format=args.format)
            else:
                try:
                    urls = scan_urls_on_device(args.serial, acc, pattern, url_format=args.format)
                except ExtractError as e:
                    print(f"[!] account {acc}: fast scan failed; falling back to adb pull ({e})", file=sys.stderr)
                    if workdir is None:
                        workdir = Path(tempfile.mkdtemp(prefix="gone_offers_"))
                        cleanup = True
                    db = pull_db(args.serial, acc, workdir)
                    if not db:
                        print(f"[-] account {acc}: no cache db")
                        continue
                    urls = extract_urls_from_db(db, pattern, url_format=args.format)
                if urls is None:
                    print(f"[-] account {acc}: no cache db")
                    continue
            if not urls:
                print(f"[-] account {acc}: no matching urls")
                continue
            print(f"[+] account {acc}: {len(urls)} url(s)")
            for u in urls:
                all_urls.append((acc, u))

        print()
        if not all_urls:
            print("No offer links found. Try --all-urls to widen the search.")
            return 1

        print("=" * 60)
        print("Extracted links:")
        print("=" * 60)
        last = None
        for acc, u in all_urls:
            if acc != last:
                print(f"\n# account {acc}")
                last = acc
            print(u)
        return 0
    finally:
        if cleanup and workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
        elif workdir is not None:
            print(f"\n[i] db files kept in: {workdir}")


if __name__ == "__main__":
    sys.exit(main())
