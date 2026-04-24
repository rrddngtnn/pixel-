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

from extract_tokens import collect_tokens
from google_auth import AuthError, UA_CHROME, mint_cookies


CLAIM_BUTTON_SELECTORS = [
    'button:has-text("Accept")',
    'button:has-text("Claim")',
    'button:has-text("Redeem")',
    'button:has-text("Get started")',
    'button:has-text("Activate")',
    'button:has-text("接受")',
    'button:has-text("领取")',
    'button:has-text("兑换")',
    'button:has-text("确认")',
    '[role="button"]:has-text("Accept")',
    '[role="button"]:has-text("Claim")',
    '[role="button"]:has-text("Redeem")',
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
    "https://one.google.com/offer/",
    "https://one.google.com/partner-eft-onboard/",
)
CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]
DEFAULT_CLAIM_VIEWPORT = "1600x900"
DEFAULT_CLAIM_WINDOW_SIZE = "1600x1000"
FIXED_DEVICE_SCALE_FACTOR = 1
TOKEN_STALE_SECONDS = 45 * 60


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
            ".tokens.json 里没有可用的 OAuthLogin token。\n"
            "请先点击前端的「准备 Chrome」或「刷新 tokens」。"
        )
    if email:
        for a in usable:
            if a["email"].lower() == email.lower():
                return a
        raise SystemExit(f"账号 {email} 没有可用的 OAuthLogin token")
    if len(usable) > 1:
        print("检测到多个可用账号，默认使用第一个。可用 --email 指定账号：")
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
            loc.click()
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except PWTimeout:
                pass
            return True, sel
        except (PWTimeout, Exception):
            continue
    return False, ""


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


def parse_browser_size(raw: str, option_name: str) -> dict[str, int]:
    text = (raw or "").strip().lower().replace(",", "x")
    if "x" not in text:
        raise ValueError(f"{option_name} must use WIDTHxHEIGHT, got: {raw!r}")
    width_text, height_text = text.split("x", 1)
    try:
        width = int(width_text)
        height = int(height_text)
    except ValueError as exc:
        raise ValueError(f"{option_name} must use numeric WIDTHxHEIGHT, got: {raw!r}") from exc
    if width < 800 or height < 600:
        raise ValueError(f"{option_name} is too small; minimum is 800x600")
    return {"width": width, "height": height}


def build_chrome_args(window_size: dict[str, int]) -> list[str]:
    return [
        *CHROME_ARGS,
        f"--window-size={window_size['width']},{window_size['height']}",
        "--window-position=0,0",
        "--high-dpi-support=1",
        f"--force-device-scale-factor={FIXED_DEVICE_SCALE_FACTOR}",
    ]


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
        parts = ["等待 Google One 页面 UI 就绪超时"]
    elif stage == "execute_patch":
        parts = ["执行页面修补脚本失败"]
    elif stage == "wait_offer_page":
        parts = ["还没有回到 Google One 优惠页"]
        if detail.get("url"):
            parts.append(f"当前页面：{detail['url']}")
    elif stage == "not_offer_page":
        parts = ["当前不是 Google One 优惠页，已停止自动注入"]
        if detail.get("url"):
            parts.append(f"当前页面：{detail['url']}")
    else:
        parts = ["页面处理未完成"]

    load = detail.get("load") or {}
    if load:
        parts.append("load 事件已完成" if load.get("load") else "load 事件未完成")
        parts.append("网络已空闲" if load.get("networkidle") else "网络空闲等待超时")

    ready = detail.get("ready") or {}
    if ready:
        ui = "有" if ready.get("hasUi") else "无"
        sxa = "有" if ready.get("hasSxa") else "无"
        ig = "有" if ready.get("hasIg") else "无"
        nodes = ready.get("nodes", 0)
        parts.append(f"页面对象：UI={ui}，sxa={sxa}，Ig={ig}，节点={nodes}")
        if ready.get("url"):
            parts.append(f"检测时页面：{ready['url']}")
        if ready.get("reason") == "redirected_to_accounts":
            parts.append("页面已跳到 Google 登录/账号选择页，所以没有执行注入")

    if detail.get("error"):
        parts.append(f"错误：{detail['error']}")
    return "；".join(parts)


def launch_claim_context(
    p,
    headless: bool,
    use_system_chrome: bool,
    viewport: dict[str, int],
    window_size: dict[str, int],
):
    launch_kwargs = {
        "headless": headless,
        "args": build_chrome_args(window_size),
    }
    try:
        if use_system_chrome:
            browser = p.chromium.launch(
                channel="chrome",
                **launch_kwargs,
            )
            launched_with = "系统 Chrome"
        else:
            raise RuntimeError("using bundled")
    except Exception:
        browser = p.chromium.launch(**launch_kwargs)
        launched_with = "Playwright 自带 Chromium"

    context = browser.new_context(
        user_agent=UA_CHROME,
        viewport=viewport,
        screen=viewport,
        device_scale_factor=FIXED_DEVICE_SCALE_FACTOR,
        is_mobile=False,
        has_touch=False,
        locale="en-US",
    )
    return browser, context, launched_with


def run(url: str, email: str | None, tokens_path: Path, headless: bool,
        auto_click: bool, keep_open: bool, timeout_s: int, use_system_chrome: bool,
        no_prompt: bool = False, browser_viewport: dict[str, int] | None = None,
        browser_window_size: dict[str, int] | None = None,
        refresh_serial: str | None = None, refresh_stale_tokens: bool = False,
        retry_badauth: bool = False) -> int:
    if not tokens_path.exists():
        if not refresh_stale_tokens:
            raise SystemExit(
                f"找不到 {tokens_path}。\n请先点击前端的「准备 Chrome」或「刷新 tokens」。"
            )
        print("[0/4] 找不到 .tokens.json，正在从手机刷新 tokens ...")
        try:
            data = refresh_tokens(tokens_path, refresh_serial)
        except Exception as e:
            print(f"[0/4] 刷新 tokens 失败：{e}")
            print("处理办法：点前端「准备 Chrome」或「刷新 tokens」拿新的 token 后再试。")
            return 2
    elif refresh_stale_tokens and token_file_is_stale(tokens_path):
        print("[0/4] 当前 tokens 已超过 45 分钟，正在从手机自动刷新 ...")
        try:
            data = refresh_tokens(tokens_path, refresh_serial)
            usable_count = len([a for a in data.get("accounts") or [] if a.get("oauth_login_token")])
            print(f"       -> tokens 已刷新，{usable_count} 个账号有 OAuthLogin token")
        except Exception as e:
            print(f"[0/4] 自动刷新 tokens 失败：{e}")
            print("       -> 继续尝试使用现有 tokens")
            data = load_tokens(tokens_path)
    else:
        data = load_tokens(tokens_path)
    acc = pick_account(data.get("accounts") or [], email)

    print(f"账号：{acc['email']}")
    print(f"链接：{url}")
    print()

    print("[1/4] 正在用手机 OAuthLogin token 生成 google.com Cookie ...")
    try:
        _, detailed, _ = mint_cookies(acc["oauth_login_token"], continue_url=url)
    except AuthError as e:
        if not (retry_badauth and auth_error_is_badauth(e)):
            print(f"失败：{e}")
            print("处理办法：点前端「准备 Chrome」或「刷新 tokens」拿新的 token 后再试。")
            return 2

        print(f"失败：{e}")
        print("       -> OAuthLogin token 已失效，正在从手机刷新 tokens 后重试一次 ...")
        try:
            data = refresh_tokens(tokens_path, refresh_serial)
            acc = pick_account(data.get("accounts") or [], email)
            print(f"       -> tokens 已刷新，重试账号：{acc['email']}")
            _, detailed, _ = mint_cookies(acc["oauth_login_token"], continue_url=url)
        except AuthError as retry_error:
            print(f"重试失败：{retry_error}")
            print("处理办法：点前端「准备 Chrome」后再试；如果手机 Chrome 弹出授权页，需要先让它完成授权。")
            return 2
        except Exception as refresh_error:
            print(f"刷新 tokens 失败：{refresh_error}")
            print("处理办法：点前端「准备 Chrome」或「刷新 tokens」拿新的 token 后再试。")
            return 2
    pw_cookies = cookies_to_playwright(detailed)
    print(f"       -> 已生成 {len(pw_cookies)} 个浏览器 Cookie")

    browser_viewport = browser_viewport or parse_browser_size(DEFAULT_CLAIM_VIEWPORT, "viewport")
    browser_window_size = browser_window_size or parse_browser_size(DEFAULT_CLAIM_WINDOW_SIZE, "window-size")

    print("[2/4] 正在启动 Chromium 隔离上下文 ...")
    with sync_playwright() as p:
        browser, context, launched_with = launch_claim_context(
            p,
            headless=headless,
            use_system_chrome=use_system_chrome,
            viewport=browser_viewport,
            window_size=browser_window_size,
        )
        print(f"       -> 已启动：{launched_with}")
        print(
            "       -> 浏览器尺寸："
            f"视口 {browser_viewport['width']}x{browser_viewport['height']}，"
            f"窗口 {browser_window_size['width']}x{browser_window_size['height']}，DPI=1"
        )

        print("[3/4] 正在注入 Cookie ...")
        context.add_cookies(pw_cookies)
        print("       -> Cookie 已注入，接下来只打开原始优惠链接")

        page = context.pages[0] if context.pages else context.new_page()

        print("[4/4] 正在打开原始优惠链接 ...")
        try:
            page.goto(url, wait_until="load", timeout=timeout_s * 1000)
        except PWTimeout:
            print(f"       页面加载超过 {timeout_s} 秒，继续后续步骤")
        except Exception as e:
            print(f"打开链接失败：{e}")
            if not headless and not no_prompt:
                input("\n按 Enter 关闭浏览器...")
            context.close()
            browser.close()
            return 3

        print(f"       -> 当前链接：{page.url}")
        print(f"       -> 页面标题：{page.title()}")

        if not is_offer_page_url(page.url):
            print("\n[页面处理] 当前不是优惠页，停止 JS 注入。")
            print(f"[页面处理] 当前页面：{page.url}")
            print("[页面处理] 正在关闭自动化 Chrome，避免停留在账号页。")
            context.close()
            browser.close()
            return 4
        else:
            print("\n[页面处理] 已到优惠链接，等待页面加载完毕和核心 UI 出现后，注入 JS ...")
            ok, patch_detail = apply_offer_page_patch(page)

        if ok:
            patch_result = patch_detail.get("patch") or {}
            print(f"[页面处理] 完成：已处理 {patch_result.get('patched', 0)} 个页面节点")
            errors = patch_result.get("errors") or []
            if errors:
                print(f"[页面处理] Ig 调用异常：{errors[:3]}")
        else:
            print(f"[页面处理] 未完成：{describe_patch_failure(patch_detail)}")
            if page.url.startswith("https://accounts.google.com/"):
                print("[页面处理] 页面跳到了 Google 登录/账号选择页，关闭自动化 Chrome。")
                context.close()
                browser.close()
                return 4

        if auto_click and is_offer_page_url(page.url):
            print("\n[自动点击] 正在查找 Accept / Claim / 领取按钮 ...")
            ok, sel = try_auto_click(page)
            if ok:
                print(f"[自动点击] 已点击：{sel}")
                print(f"[自动点击] 点击后链接：{page.url}")
            else:
                print("[自动点击] 没找到匹配按钮，需要在弹出的浏览器里手动点击。")
        elif auto_click:
            print("\n[自动点击] 当前不在优惠页，跳过自动点击。")

        if no_prompt:
            print("\n[等待] 浏览器已打开，可以在 Chromium 窗口里完成后续点击。")
            print("[等待] 关闭 Chromium 窗口后，后台进程会自动退出。")
            sys.stdout.flush()
            try:
                while context.pages:
                    time.sleep(1)
            except (KeyboardInterrupt, Exception):
                pass
            print("[等待] 浏览器已关闭，进程退出。")
        elif keep_open or not headless:
            print("\n浏览器已打开，可以在窗口里完成剩余点击。")
            try:
                input("按 Enter 关闭浏览器并退出...")
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
    ap.add_argument("--url", required=True, help="the one.google.com/offer/... URL")
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
    ap.add_argument(
        "--viewport",
        default=os.environ.get("AUTO_CLAIM_VIEWPORT", DEFAULT_CLAIM_VIEWPORT),
        help=f"fixed page viewport as WIDTHxHEIGHT (default {DEFAULT_CLAIM_VIEWPORT})",
    )
    ap.add_argument(
        "--window-size",
        default=os.environ.get("AUTO_CLAIM_WINDOW_SIZE", DEFAULT_CLAIM_WINDOW_SIZE),
        help=f"fixed Chrome outer window as WIDTHxHEIGHT (default {DEFAULT_CLAIM_WINDOW_SIZE})",
    )
    args = ap.parse_args()
    try:
        browser_viewport = parse_browser_size(args.viewport, "--viewport")
        browser_window_size = parse_browser_size(args.window_size, "--window-size")
    except ValueError as exc:
        ap.error(str(exc))

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
        browser_viewport=browser_viewport,
        browser_window_size=browser_window_size,
        refresh_serial=args.serial,
        refresh_stale_tokens=args.refresh_stale_tokens,
        retry_badauth=args.retry_badauth,
    )


if __name__ == "__main__":
    sys.exit(main())
