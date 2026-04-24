"""
Exchange a Google OAuthLogin access token (the `ya29.m.*` value cached by
com.android.chrome in Android's authtokens table, scope
`https://www.google.com/accounts/OAuthLogin`) for a fully-authenticated set of
Google.com web cookies. This is exactly what Chrome does when you "sign into
Chrome" on a new computer.

Steps:
  1) GET accounts.google.com/OAuthLogin?source=ChromiumBrowser&issueuberauth=1
       Authorization: Bearer <oauth_login_token>
     -> response body is a single-use "uberauth" token (plain text)
  2) GET accounts.google.com/MergeSession?uberauth=...&continue=<url>&source=...
     -> follows a chain of 302s that Set-Cookie SID/HSID/SSID/APISID/SAPISID
        and various __Secure-* variants for .google.com

CLI test (after `python extract_tokens.py --save`):
    python google_auth.py                  # test the first account
    python google_auth.py --email foo@...  # test a specific account
    python google_auth.py --save-cookies   # write .cookies/<email>.json

The saved cookies json is consumed by the next step (auto_claim.py), which
launches a Chromium with those cookies pre-loaded and opens the partner
offer URL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import urlencode
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run: pip install -r requirements.txt")

UA_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
SOURCE = "ChromiumBrowser"

KEY_COOKIES = [
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
]


class AuthError(RuntimeError):
    pass


def _oauth_to_uberauth(session: requests.Session, oauth_login_token: str) -> str:
    r = session.get(
        "https://accounts.google.com/OAuthLogin",
        params={"source": SOURCE, "issueuberauth": "1"},
        headers={"Authorization": f"Bearer {oauth_login_token}"},
        allow_redirects=False,
        timeout=30,
    )
    if r.status_code != 200:
        raise AuthError(
            f"OAuthLogin returned HTTP {r.status_code}. Body: {r.text[:200]!r}\n"
            "Likely the OAuthLogin token has expired (these last ~1 hour). "
            "Re-run extract_tokens.py to pull fresh ones."
        )
    body = r.text.strip()
    if not body or "<html" in body.lower() or body.lower().startswith("error"):
        raise AuthError(f"OAuthLogin did not return an uberauth token. Body: {body[:200]!r}")
    return body


def build_merge_session_url(oauth_login_token: str, continue_url: str) -> str:
    """Return a single-use MergeSession URL for the browser to open directly."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA_CHROME,
        "Accept-Language": "en-US,en;q=0.9",
    })
    ubertoken = _oauth_to_uberauth(session, oauth_login_token)
    params = urlencode({
        "uberauth": ubertoken,
        "continue": continue_url,
        "source": SOURCE,
    })
    return f"https://accounts.google.com/MergeSession?{params}"


def mint_cookies(
    oauth_login_token: str,
    continue_url: str = "https://myaccount.google.com/",
) -> tuple[dict, list, requests.Session]:
    """Return (cookie_map_for_dot_google_com, detailed_cookie_list, logged_in_session)."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA_CHROME,
        "Accept-Language": "en-US,en;q=0.9",
    })

    ubertoken = _oauth_to_uberauth(session, oauth_login_token)

    r = session.get(
        "https://accounts.google.com/MergeSession",
        params={
            "uberauth": ubertoken,
            "continue": continue_url,
            "source": SOURCE,
        },
        allow_redirects=True,
        timeout=30,
    )
    if r.status_code >= 400:
        raise AuthError(f"MergeSession HTTP {r.status_code}: {r.text[:200]!r}")

    simple: dict[str, str] = {}
    detailed: list[dict] = []
    for c in session.cookies:
        if not (c.domain or "").lstrip(".").endswith("google.com"):
            continue
        if c.name not in simple:
            simple[c.name] = c.value
        detailed.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "secure": bool(c.secure),
            "httpOnly": bool(c._rest.get("HttpOnly", False)) if hasattr(c, "_rest") else False,
            "expires": int(c.expires) if c.expires else None,
        })

    return simple, detailed, session


def _verify_signed_in(session: requests.Session, expected_email: str) -> tuple[bool, str]:
    """Hit myaccount page and see if the expected email shows up."""
    r = session.get("https://myaccount.google.com/", timeout=30, allow_redirects=True)
    body = r.text or ""
    if expected_email.lower() in body.lower():
        return True, "email found on myaccount page"
    if '"Google Account"' in body and "Sign out" in body:
        return True, "generic signed-in indicators (email not present)"
    if "Sign in" in body and "Sign out" not in body:
        return False, "looks signed-OUT"
    return False, f"inconclusive (HTTP {r.status_code}, len={len(body)})"


def _load_tokens(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"{path} not found. Run first:\n  python extract_tokens.py --save"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _save_cookies(email: str, detailed: list[dict], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = email.replace("@", "_at_").replace("/", "_")
    out = out_dir / f"{safe}.json"
    out.write_text(json.dumps(detailed, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tokens", default=".tokens.json", help="path to tokens file (default: .tokens.json)")
    ap.add_argument("--email", help="which account to test (default: first with an OAuthLogin token)")
    ap.add_argument("--continue-url", default="https://one.google.com/",
                    help="the URL MergeSession will redirect to after minting cookies")
    ap.add_argument("--save-cookies", action="store_true",
                    help="write detailed cookie list to .cookies/<email>.json")
    ap.add_argument("--cookies-dir", default=".cookies")
    ap.add_argument("--show", action="store_true", help="print cookie names + truncated values")
    args = ap.parse_args()

    tokens_path = Path(args.tokens)
    data = _load_tokens(tokens_path)

    accounts = data.get("accounts") or []
    candidates = [a for a in accounts if a.get("oauth_login_token")]
    if not candidates:
        print("No account has an oauth_login_token in tokens file. "
              "Make sure the account was signed into Chrome on the phone "
              "(Chrome is what caches this token).")
        return 1

    if args.email:
        candidates = [a for a in candidates if a["email"] == args.email]
        if not candidates:
            print(f"Email {args.email!r} has no OAuthLogin token.")
            return 1

    acc = candidates[0]
    print(f"Account:      {acc['email']}")
    print(f"continue URL: {args.continue_url}")
    print()
    print("[1/2] OAuthLogin -> uberauth ...")
    try:
        simple, detailed, session = mint_cookies(
            acc["oauth_login_token"],
            continue_url=args.continue_url,
        )
    except AuthError as e:
        print(f"FAILED: {e}")
        return 1
    print("[2/2] MergeSession cookies set.")
    print()

    key_present = [k for k in KEY_COOKIES if k in simple]
    key_missing = [k for k in KEY_COOKIES if k not in simple]
    print(f"Total cookies captured: {len(simple)}  ({len(detailed)} entries across domains)")
    print(f"  Key cookies present:  {key_present or '(none)'}")
    if key_missing:
        print(f"  Key cookies missing:  {key_missing}")

    if args.show:
        for name, val in sorted(simple.items()):
            preview = val if len(val) <= 24 else f"{val[:16]}…{val[-4:]}"
            print(f"    {name} = {preview}   ({len(val)} chars)")

    print()
    print("[verify] fetching myaccount.google.com to confirm login ...")
    ok, detail = _verify_signed_in(session, acc["email"])
    print(f"  -> {'OK' if ok else 'NOT SIGNED IN'}: {detail}")

    if args.save_cookies:
        out = _save_cookies(acc["email"], detailed, Path(args.cookies_dir))
        print(f"\nSaved cookies to {out}  (0600)")

    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
