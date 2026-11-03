"""
Auto-claim a Google One partner-offer URL:

    1. Read OAuthLogin token from .tokens.json (produced by extract_tokens.py)
    2. Mint fresh google.com cookies (reuses google_auth.mint_cookies)
    3. Launch Chromium, inject cookies, open the Google One offer URL
    4. Patch the loaded Google One offer UI
    5. Optionally try to click the "Accept / Claim" button

Prereq (one-time):
    pip install -r requirements.txt
    playwright install chromium

Typical usage:
    # already have fresh .tokens.json
    python auto_claim.py --url "https://one.google.com/offer/XXXXX"

    # try to click the Accept button too (pages vary; may need tuning)
    python auto_claim.py --url "..." --auto-click

    # run completely in background:
    python auto_claim.py --url "..." --headless --auto-click
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright
except ImportError:
    sys.exit(
        "Playwright not installed.\n"
        "Run:\n  pip install -r requirements.txt\n  playwright install chromium"
    )

from extract_offers import (
    OFFER_URL_PREFIX,
    PARTNER_URL_PREFIX,
    normalize_offer_url,
    to_partner_url,
)
from extract_tokens import collect_tokens
from google_auth import AuthError, UA_CHROME, mint_cookies


CLAIM_BUTTON_SELECTORS = [
    'button:has-text("Accept")',
    'button:has-text("Claim")',
    'button:has-text("Redeem")',
    'button:has-text("Get started")',
    'button:has-text("Activate")',
    'button:has-text("Continue")',
    'button:has-text("Next")',
    'button:has-text("Subscribe")',
    'button:has-text("Start trial")',
    'button:has-text("Try it")',
    'button:has-text("Join")',
    'button:has-text("Checkout")',
    'button:has-text("Confirm")',
    '[role="button"]:has-text("Accept")',
    '[role="button"]:has-text("Claim")',
    '[role="button"]:has-text("Redeem")',
    '[role="button"]:has-text("Continue")',
    '[role="button"]:has-text("Next")',
    '[role="button"]:has-text("Subscribe")',
    '[role="button"]:has-text("Start trial")',
]


OFFER_PAGE_PATCH = r"""
(() => {
  const completion = typeof window.completion === "function"
    ? window.completion.bind(window)
    : () => {};
  const t = window.default_SubscriptionsManagementUi;
  if (!t) return { ok: false, reason: "default_SubscriptionsManagementUi missing" };
  if (typeof t.sxa !== "function") return { ok: false, reason: "sxa missing" };
  if (typeof t.Ig !== "function") return { ok: false, reason: "Ig missing" };

  const orig = t.sxa;
  t.sxa = function (arg) {
    if (arg === null) return null;
    return orig.apply(this, arguments);
  };

  let patched = 0;
  let errors = [];
  document.querySelectorAll('[jscontroller="ytxTId"]').forEach((el) => {
    const clone = el.cloneNode(true);
    const parent = el.parentNode;
    if (!parent) return;
    parent.replaceChild(clone, el);
    try {
      t.Ig(clone, 'rcuQ6b', undefined, false);
      patched += 1;
    } catch (e) {
      errors.push(e && e.message ? e.message : String(e));
      console.log('Ig err:', e && e.message ? e.message : e);
    }
  });
  completion();
  return { ok: true, patched, errors };
})()
"""

OFFER_UI_READY_SCRIPT = r"""
(() => {
  const t = window.default_SubscriptionsManagementUi;
  const nodes = document.querySelectorAll('[jscontroller="ytxTId"]').length;
  return {
    readyState: document.readyState,
    hasUi: !!t,
    hasSxa: !!(t && typeof t.sxa === "function"),
    hasIg: !!(t && typeof t.Ig === "function"),
    nodes,
    url: location.href,
  };
})()
"""

OFFER_URL_PREFIXES = (
    OFFER_URL_PREFIX,
    PARTNER_URL_PREFIX,
)
CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]
if sys.platform.startswith("linux"):
    CHROME_ARGS += ["--no-sandbox", "--disable-dev-shm-usage"]
TOKEN_STALE_SECONDS = 45 * 60


def alternate_offer_url(url: str) -> str | None:
    """Return the alternate Google One offer URL prefix (/offer/ <-> /partner-eft-onboard/)."""
    if url.startswith(OFFER_URL_PREFIX):
        alt = to_partner_url(url)
        return alt if alt != url else None
    if url.startswith(PARTNER_URL_PREFIX):
        alt = normalize_offer_url(url)
        return alt if alt != url else None
    return None


def cookies_to_playwright(detailed: list[dict]) -> list[dict]:
    """Convert google_auth.mint_cookies detailed output to Playwright's add_cookies format."""
    out: list[dict] = []
    for c in detailed:
        domain = c.get("domain") or ""
        if not domain:
            continue
        ck: dict = {
            "name": c["name"],
            "value": c["value"],
            "domain": domain,
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure")),
            "httpOnly": bool(c.get("httpOnly")),
            "sameSite": "Lax",
        }
        exp = c.get("expires")
        if exp and exp > 0:
            ck["expires"] = float(exp)
        out.append(ck)
    return out

def pick_account(accounts: list[dict], email: str | None) -> dict:
    usable = [a for a in accounts if a.get("oauth_login_token")]
    if not usable:
        raise SystemExit(
            "No usable OAuthLogin token found in .tokens.json.\n"
            "Click 'Prime Chrome' or 'Refresh Tokens' in the Web UI first."
        )
    if email:
        for a in usable:
            if a["email"].lower() == email.lower():
                return a
        raise SystemExit(f"Account {email} does not have a usable OAuthLogin token.")
    if len(usable) > 1:
        print("Multiple usable accounts detected; defaulting to the first one (use --email to specify):")
        for a in usable:
            print(f"  - {a['email']}")
    return usable[0]


def load_tokens(tokens_path: Path) -> dict:
    return json.loads(tokens_path.read_text(encoding="utf-8"))


def save_tokens(tokens_path: Path, data: dict) -> dict:
    persisted = {
        "android_id": data["android_id"],
        "accounts": data["accounts"],
    }
    tokens_path.write_text(json.dumps(persisted, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tokens_path, 0o600)
    except OSError:
        pass
    return persisted


def refresh_tokens(tokens_path: Path, serial: str | None) -> dict:
    result = collect_tokens(serial)
    return save_tokens(tokens_path, result)


def token_file_is_stale(tokens_path: Path) -> bool:
    try:
        return time.time() - tokens_path.stat().st_mtime > TOKEN_STALE_SECONDS
    except OSError:
        return True


def auth_error_is_badauth(error: Exception) -> bool:
    return "badauth" in str(error).lower()


def try_auto_click(page, timeout_ms: int = 6000) -> tuple[bool, str]:
    for sel in CLAIM_BUTTON_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            loc.wait_for(state="visible", timeout=timeout_ms)
            loc.scroll_into_view_if_needed(timeout=timeout_ms)
            loc.click()
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except PWTimeout:
                pass
            return True, sel
        except (PWTimeout, Exception):
            continue
    return False, ""


def set_chrome_zoom_50(page, headless: bool, native_win_zoom: bool = False) -> tuple[bool, str]:
    """Apply 50% zoom cross-platform via CSS/DOM (works in headless & all OSes without hijacking mouse).

    If native_win_zoom=True on Windows with a visible window, falls back to Win32 SendInput Ctrl+wheel.
    """
    try:
        applied = page.evaluate(
            """() => {
                if (!document.documentElement) return false;
                document.documentElement.style.zoom = '0.5';
                if (document.body) {
                    document.body.style.zoom = '0.5';
                }
                return document.documentElement.style.zoom === '0.5';
            }"""
        )
        if applied and (headless or sys.platform != "win32" or not native_win_zoom):
            return True, "Applied cross-platform 50% CSS/DOM zoom (no mouse hijack, headless-compatible)"
    except Exception as css_err:
        if headless or sys.platform != "win32" or not native_win_zoom:
            return False, f"CSS zoom failed: {css_err}"

    if headless:
        return False, "Headless mode enabled; skipping native Win32 mouse zoom"
    if sys.platform != "win32":
        return False, "Non-Windows platform; skipping native Win32 mouse zoom"

    try:
        import ctypes

        input_mouse = 0
        input_keyboard = 1
        keyeventf_keyup = 0x0002
        mouseeventf_wheel = 0x0800
        vk_control = 0x11

        class MouseInput(ctypes.Structure):
            _fields_ = [
                ("dx", ctypes.c_long),
                ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class KeyboardInput(ctypes.Structure):
            _fields_ = [
                ("wVk", ctypes.c_ushort),
                ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class InputUnion(ctypes.Union):
            _fields_ = [("mi", MouseInput), ("ki", KeyboardInput)]

        class Input(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ulong), ("union", InputUnion)]

        def send_key(vk: int, keyup: bool = False) -> None:
            extra = ctypes.c_ulong(0)
            event = Input(
                type=input_keyboard,
                union=InputUnion(
                    ki=KeyboardInput(
                        vk,
                        0,
                        keyeventf_keyup if keyup else 0,
                        0,
                        ctypes.pointer(extra),
                    )
                ),
            )
            ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event))

        def send_wheel(delta: int) -> None:
            extra = ctypes.c_ulong(0)
            event = Input(
                type=input_mouse,
                union=InputUnion(
                    mi=MouseInput(
                        0,
                        0,
                        ctypes.c_ulong(delta & 0xFFFFFFFF).value,
                        mouseeventf_wheel,
                        0,
                        ctypes.pointer(extra),
                    )
                ),
            )
            ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event))

        page.bring_to_front()
        page.mouse.move(480, 360)
        page.wait_for_timeout(600)

        send_key(vk_control)
        try:
            for _ in range(5):
                send_wheel(-120)
                page.wait_for_timeout(90)
        finally:
            send_key(vk_control, keyup=True)

        page.wait_for_timeout(600)
        dpr = page.evaluate("window.devicePixelRatio")
        zoom = round(float(dpr) * 100)
        if 45 <= zoom <= 55:
            return True, f"Chrome zoom set to ~{zoom}%"
        return False, f"Zoom input sent, but detected ~{zoom}%"
    except Exception as e:
        return False, str(e)


def wait_for_offer_page_load(page, timeout_ms: int) -> dict:
    """Wait for the document load event, then best-effort network idle."""
    detail: dict = {"load": False, "networkidle": False}
    try:
        page.wait_for_load_state("load", timeout=timeout_ms)
        detail["load"] = True
    except PWTimeout:
        detail["load_timeout"] = True
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
        detail["networkidle"] = True
    except PWTimeout:
        detail["networkidle_timeout"] = True
    return detail


def is_offer_page_url(current_url: str) -> bool:
    return any((current_url or "").startswith(prefix) for prefix in OFFER_URL_PREFIXES)


def wait_for_offer_ui_ready(page, timeout_ms: int = 45000) -> tuple[bool, dict]:
    """Wait until Google One's UI object and target controller nodes exist."""
    deadline = time.time() + timeout_ms / 1000
    last: dict = {"ok": False, "reason": "not attempted"}
    while time.time() < deadline:
        try:
            last = page.evaluate(OFFER_UI_READY_SCRIPT)
            current_url = str(last.get("url") or "")
            if (
                isinstance(last, dict)
                and is_offer_page_url(current_url)
                and last.get("readyState") == "complete"
                and last.get("hasUi")
                and last.get("hasSxa")
                and last.get("hasIg")
                and int(last.get("nodes") or 0) > 0
            ):
                return True, last
            if current_url.startswith("https://accounts.google.com/"):
                last["reason"] = "redirected_to_accounts"
                return False, last
        except Exception as e:
            last = {"ok": False, "reason": str(e)}
        time.sleep(0.5)
    return False, last


def apply_offer_page_patch(page, timeout_ms: int = 45000) -> tuple[bool, dict]:
    """Load completed offer UI, then execute the Google One UI patch once."""
    load_detail = wait_for_offer_page_load(page, timeout_ms)
    ready, ready_detail = wait_for_offer_ui_ready(page, timeout_ms)
    if not ready:
        return False, {
            "stage": "wait_ui_ready",
            "load": load_detail,
            "ready": ready_detail,
        }
    try:
        patch_detail = page.evaluate(OFFER_PAGE_PATCH)
    except Exception as e:
        return False, {
            "stage": "execute_patch",
            "load": load_detail,
            "ready": ready_detail,
            "error": str(e),
        }
    return bool(isinstance(patch_detail, dict) and patch_detail.get("ok")), {
        "stage": "execute_patch",
        "load": load_detail,
        "ready": ready_detail,
        "patch": patch_detail,
    }


def describe_patch_failure(detail: dict) -> str:
    stage = detail.get("stage")
    if stage == "wait_ui_ready":
        parts = ["Timed out waiting for Google One offer UI to become ready"]
    elif stage == "execute_patch":
        parts = ["Failed to execute offer page UI patch"]
    elif stage == "wait_offer_page":
        parts = ["Did not reach Google One offer page yet"]
        if detail.get("url"):
            parts.append(f"current URL: {detail['url']}")
    elif stage == "not_offer_page":
        parts = ["Current page is not a Google One offer URL; skipping JS injection"]
        if detail.get("url"):
            parts.append(f"current URL: {detail['url']}")
    else:
        parts = ["Offer page processing incomplete"]

    load = detail.get("load") or {}
    if load:
        parts.append("load event completed" if load.get("load") else "load event timed out")
        parts.append("network idle" if load.get("networkidle") else "network idle timed out")

    ready = detail.get("ready") or {}
    if ready:
        ui = "yes" if ready.get("hasUi") else "no"
        sxa = "yes" if ready.get("hasSxa") else "no"
        ig = "yes" if ready.get("hasIg") else "no"
        nodes = ready.get("nodes", 0)
        parts.append(f"page state: UI={ui}, sxa={sxa}, Ig={ig}, nodes={nodes}")
        if ready.get("url"):
            parts.append(f"inspected URL: {ready['url']}")
        if ready.get("reason") == "redirected_to_accounts":
            parts.append("redirected to Google sign-in/account chooser; skipped JS injection")

    if detail.get("error"):
        parts.append(f"error: {detail['error']}")
    return "; ".join(parts)


def launch_claim_context(p, headless: bool, use_system_chrome: bool):
    launch_kwargs = {
        "headless": headless,
        "args": CHROME_ARGS,
    }
    try:
        if use_system_chrome:
            browser = p.chromium.launch(
                channel="chrome",
                **launch_kwargs,
            )
            launched_with = "System Chrome"
        else:
            raise RuntimeError("using bundled")
    except Exception:
        browser = p.chromium.launch(**launch_kwargs)
        launched_with = "Playwright Bundled Chromium"

    context = browser.new_context(
        user_agent=UA_CHROME,
        viewport={"width": 1440, "height": 1600},
        locale="en-US",
    )
    return browser, context, launched_with


def _open_and_patch_offer(
    page,
    target_url: str,
    timeout_s: int,
    headless: bool,
    native_win_zoom: bool = False,
) -> tuple[bool, dict, str | None]:
    """Navigate to target_url, apply 50% zoom, and execute the Google One UI patch."""
    try:
        page.goto(target_url, wait_until="load", timeout=timeout_s * 1000)
    except PWTimeout:
        print(f"       Page load exceeded {timeout_s}s; continuing with UI inspection")
    except Exception as e:
        return False, {"error": str(e)}, f"Failed to open URL: {e}"

    print(f"       -> Current URL: {page.url}")
    print(f"       -> Page title:  {page.title()}")

    if not is_offer_page_url(page.url):
        return False, {"stage": "not_offer_page", "url": page.url}, None

    print("\n[Page] Reached offer URL; applying 50% zoom and injecting Google One UI patch ...")
    zoom_ok, zoom_msg = set_chrome_zoom_50(page, headless=headless, native_win_zoom=native_win_zoom)
    if zoom_ok:
        print(f"[Page] {zoom_msg}")
    else:
        print(f"[Page] Browser zoom note: {zoom_msg}")
    ok, patch_detail = apply_offer_page_patch(page)
    return ok, patch_detail, None


def run(url: str, email: str | None, tokens_path: Path, headless: bool,
        auto_click: bool, keep_open: bool, timeout_s: int, use_system_chrome: bool,
        no_prompt: bool = False, refresh_serial: str | None = None, refresh_stale_tokens: bool = False,
        retry_badauth: bool = False, partner_fallback: bool = True,
        native_win_zoom: bool = False) -> int:
    if not tokens_path.exists():
        if not refresh_stale_tokens:
            raise SystemExit(
                f"{tokens_path} not found.\nClick 'Prime Chrome' or 'Refresh Tokens' in the Web UI first."
            )
        print("[0/4] .tokens.json not found; refreshing tokens from phone ...")
        try:
            data = refresh_tokens(tokens_path, refresh_serial)
        except Exception as e:
            print(f"[0/4] Token refresh failed: {e}")
            print(" Remedy: Click 'Prime Chrome' or 'Refresh Tokens' in the Web UI and retry.")
            return 2
    elif refresh_stale_tokens and token_file_is_stale(tokens_path):
        print("[0/4] .tokens.json is older than 45 minutes; auto-refreshing from phone ...")
        try:
            data = refresh_tokens(tokens_path, refresh_serial)
            usable_count = len([a for a in data.get("accounts") or [] if a.get("oauth_login_token")])
            print(f"       -> Tokens refreshed ({usable_count} account(s) with OAuthLogin token)")
        except Exception as e:
            print(f"[0/4] Auto token refresh failed: {e}")
            print("       -> Proceeding with existing cached tokens")
            data = load_tokens(tokens_path)
    else:
        data = load_tokens(tokens_path)
    acc = pick_account(data.get("accounts") or [], email)

    print(f"Account: {acc['email']}")
    print(f"URL:     {url}")
    print()

    print("[1/4] Minting google.com web cookies from phone OAuthLogin token ...")
    try:
        _, detailed, _ = mint_cookies(acc["oauth_login_token"], continue_url=url)
    except AuthError as e:
        if not (retry_badauth and auth_error_is_badauth(e)):
            print(f"Failed: {e}")
            print("Remedy: Click 'Prime Chrome' or 'Refresh Tokens' in the Web UI and retry.")
            return 2

        print(f"Failed: {e}")
        print("       -> OAuthLogin token expired; refreshing tokens from phone and retrying once ...")
        try:
            data = refresh_tokens(tokens_path, refresh_serial)
            acc = pick_account(data.get("accounts") or [], email)
            print(f"       -> Tokens refreshed; retrying account: {acc['email']}")
            _, detailed, _ = mint_cookies(acc["oauth_login_token"], continue_url=url)
        except AuthError as retry_error:
            print(f"Retry failed: {retry_error}")
            print("Remedy: Click 'Prime Chrome' first and complete any authorization prompt on the phone.")
            return 2
        except Exception as refresh_error:
            print(f"Token refresh failed: {refresh_error}")
            print("Remedy: Click 'Prime Chrome' or 'Refresh Tokens' in the Web UI and retry.")
            return 2
    pw_cookies = cookies_to_playwright(detailed)
    print(f"       -> Minted {len(pw_cookies)} browser cookies")

    print("[2/4] Launching isolated Chromium context ...")
    with sync_playwright() as p:
        browser, context, launched_with = launch_claim_context(
            p,
            headless=headless,
            use_system_chrome=use_system_chrome,
        )
        print(f"       -> Launched: {launched_with}")

        print("[3/4] Injecting cookies ...")
        context.add_cookies(pw_cookies)
        print("       -> Cookies injected; navigating directly to offer URL")

        page = context.pages[0] if context.pages else context.new_page()

        print("[4/4] Opening offer URL ...")
        ok, patch_detail, nav_err = _open_and_patch_offer(
            page,
            url,
            timeout_s=timeout_s,
            headless=headless,
            native_win_zoom=native_win_zoom,
        )
        if nav_err:
            print(nav_err)
            if not headless and not no_prompt:
                input("\nPress Enter to close the browser...")
            context.close()
            browser.close()
            return 3

        alt_url = alternate_offer_url(url) if partner_fallback else None
        tried_fallback = False

        if not is_offer_page_url(page.url):
            if alt_url and not page.url.startswith("https://accounts.google.com/"):
                print(f"\n[Fallback] Initial URL redirected away ({page.url}); retrying with alternate prefix: {alt_url}")
                tried_fallback = True
                ok, patch_detail, nav_err = _open_and_patch_offer(
                    page,
                    alt_url,
                    timeout_s=timeout_s,
                    headless=headless,
                    native_win_zoom=native_win_zoom,
                )
            if not is_offer_page_url(page.url):
                print("\n[Page] Current page is not a Google One offer URL; stopping JS injection.")
                print(f"[Page] Current URL: {page.url}")
                print("[Page] Closing automated browser.")
                context.close()
                browser.close()
                return 4

        if not ok and alt_url and not tried_fallback and not page.url.startswith("https://accounts.google.com/"):
            print(f"\n[Page] Initial UI patch incomplete ({describe_patch_failure(patch_detail)})")
            print(f"[Fallback] Switching between /offer/ and /partner-eft-onboard/: {alt_url}")
            tried_fallback = True
            ok, patch_detail, _ = _open_and_patch_offer(
                page,
                alt_url,
                timeout_s=timeout_s,
                headless=headless,
                native_win_zoom=native_win_zoom,
            )

        if ok:
            patch_result = patch_detail.get("patch") or {}
            print(f"[Page] Patch complete: re-bound {patch_result.get('patched', 0)} controller node(s)")
            errors = patch_result.get("errors") or []
            if errors:
                print(f"[Page] Ig call warnings: {errors[:3]}")
        else:
            print(f"[Page] Patch incomplete: {describe_patch_failure(patch_detail)}")
            if page.url.startswith("https://accounts.google.com/"):
                print("[Page] Redirected to Google sign-in/account chooser; closing automated browser.")
                context.close()
                browser.close()
                return 4

        if auto_click and is_offer_page_url(page.url):
            print("\n[Auto-Click] Searching for Accept / Claim / Start trial button ...")
            clicked, sel = try_auto_click(page)
            if not clicked and alt_url and not tried_fallback:
                print(f"[Fallback] No clickable CTA found on {page.url}; trying alternate URL: {alt_url}")
                tried_fallback = True
                ok, patch_detail, _ = _open_and_patch_offer(
                    page,
                    alt_url,
                    timeout_s=timeout_s,
                    headless=headless,
                    native_win_zoom=native_win_zoom,
                )
                if is_offer_page_url(page.url):
                    clicked, sel = try_auto_click(page)
            if clicked:
                print(f"[Auto-Click] Clicked: {sel}")
                print(f"[Auto-Click] Post-click URL: {page.url}")
            else:
                print("[Auto-Click] No matching CTA button found; complete the click manually in the browser window.")
        elif auto_click:
            print("\n[Auto-Click] Not on an offer URL; skipping auto-click.")

        if no_prompt:
            print("\n[Wait] Browser is open; complete any remaining checkout steps in the Chromium window.")
            print("[Wait] The background process will exit automatically once the Chromium window is closed.")
            sys.stdout.flush()
            try:
                while context.pages:
                    time.sleep(1)
            except (KeyboardInterrupt, Exception):
                pass
            print("[Wait] Browser closed; exiting.")
        elif keep_open or not headless:
            print("\nBrowser is open; complete any remaining steps in the window.")
            try:
                input("Press Enter to close the browser and exit...")
            except EOFError:
                pass

        try:
            context.close()
            browser.close()
        except Exception:
            pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="the one.google.com/offer/... or partner-eft-onboard/... URL")
    ap.add_argument("--email", help="which account in .tokens.json to use (default: first with token)")
    ap.add_argument("--tokens", default=".tokens.json")
    ap.add_argument("--headless", action="store_true", help="run without a visible window")
    ap.add_argument("--auto-click", action="store_true",
                    help="try to click the Accept/Claim button automatically")
    ap.add_argument("--keep-open", action="store_true",
                    help="(headless only) wait for Enter before closing")
    ap.add_argument("--no-prompt", action="store_true",
                    help="do not wait for console input; exit when the browser closes")
    ap.add_argument("--timeout", type=int, default=60, help="page load timeout in seconds (default 60)")
    ap.add_argument("--bundled-chromium", action="store_true",
                    help="force Playwright's bundled Chromium even if system Chrome is available")
    ap.add_argument("--serial", help="adb serial used when refreshing stale/expired phone tokens")
    ap.add_argument("--refresh-stale-tokens", action="store_true",
                    help="refresh .tokens.json from the phone first when it is older than 45 minutes")
    ap.add_argument("--retry-badauth", action="store_true",
                    help="if OAuthLogin returns badauth, refresh tokens and retry once")
    ap.add_argument("--no-partner-fallback", action="store_true",
                    help="disable automatic fallback between /offer/ and /partner-eft-onboard/")
    ap.add_argument("--native-win-zoom", action="store_true",
                    help="use legacy Win32 SendInput Ctrl+wheel zoom instead of CSS zoom on Windows")
    args = ap.parse_args()

    return run(
        url=args.url,
        email=args.email,
        tokens_path=Path(args.tokens),
        headless=args.headless,
        auto_click=args.auto_click,
        keep_open=args.keep_open,
        timeout_s=args.timeout,
        use_system_chrome=not args.bundled_chromium,
        no_prompt=args.no_prompt,
        refresh_serial=args.serial,
        refresh_stale_tokens=args.refresh_stale_tokens,
        retry_badauth=args.retry_badauth,
        partner_fallback=not args.no_partner_fallback,
        native_win_zoom=args.native_win_zoom,
    )


if __name__ == "__main__":
    sys.exit(main())
