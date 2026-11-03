from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

from extract_offers import ExtractError, adb


CHROME_PACKAGE = "com.android.chrome"
CHROME_ACTIVITY = "com.google.android.apps.chrome.Main"
ACCOUNTS_URL = "https://accounts.google.com/"

POSITIVE_PATTERNS = [
    r"continue as",
    r"continue",
    r"sign in",
    r"use account",
    r"yes,?\s*i'?m in",
    r"i agree",
    r"accept",
    r"allow",
    r"got it",
]

PRIORITY_RESOURCE_IDS = [
    "com.android.chrome:id/signin_fre_continue_button",
    "com.android.chrome:id/button_primary",
    "com.android.chrome:id/positive_button",
]

NEGATIVE_PATTERNS = [
    r"no thanks",
    r"not now",
    r"skip",
]


def _xml_attr(node: ET.Element, name: str) -> str:
    return node.attrib.get(name) or ""


def _node_text(node: ET.Element) -> str:
    parts = [
        _xml_attr(node, "text"),
        _xml_attr(node, "content-desc"),
        _xml_attr(node, "resource-id"),
    ]
    return " ".join(p for p in parts if p).strip()


def _bounds_center(bounds: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = [int(v) for v in m.groups()]
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def _matches(text: str, patterns: list[str]) -> bool:
    low = text.lower()
    return any(re.search(p, low, flags=re.IGNORECASE) for p in patterns)


def _is_clickable(node: ET.Element) -> bool:
    return _xml_attr(node, "clickable") == "true" and _xml_attr(node, "enabled") != "false"


def _is_button(node: ET.Element) -> bool:
    klass = _xml_attr(node, "class")
    return klass.endswith(".Button") or klass == "android.widget.Button"


def _dump_ui(serial: str | None) -> ET.Element | None:
    adb(["shell", "uiautomator", "dump", "/sdcard/gone_window.xml"], serial=serial, check=False)
    r = adb(["shell", "cat", "/sdcard/gone_window.xml"], serial=serial, check=False)
    raw = r.stdout or ""
    start = raw.find("<?xml")
    if start > 0:
        raw = raw[start:]
    if "<hierarchy" not in raw:
        return None
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        return None


def _click_node(serial: str | None, node: ET.Element) -> bool:
    center = _bounds_center(_xml_attr(node, "bounds"))
    if not center:
        return False
    x, y = center
    adb(["shell", "input", "tap", str(x), str(y)], serial=serial, check=False)
    return True


def _find_click_target(root: ET.Element, email: str | None) -> ET.Element | None:
    nodes = list(root.iter("node"))

    for wanted in PRIORITY_RESOURCE_IDS:
        for node in nodes:
            if (
                _xml_attr(node, "resource-id") == wanted
                and _is_clickable(node)
                and _bounds_center(_xml_attr(node, "bounds"))
            ):
                return node

    for node in nodes:
        text = _node_text(node)
        if (
            text
            and _is_button(node)
            and _is_clickable(node)
            and _matches(text, POSITIVE_PATTERNS)
            and not _matches(text, NEGATIVE_PATTERNS)
            and _bounds_center(_xml_attr(node, "bounds"))
        ):
            return node

    if email:
        email_low = email.lower()
        for node in nodes:
            text = _node_text(node)
            if email_low in text.lower() and _is_clickable(node) and _bounds_center(_xml_attr(node, "bounds")):
                return node

    for node in nodes:
        text = _node_text(node)
        if (
            text
            and _is_clickable(node)
            and _matches(text, POSITIVE_PATTERNS)
            and not _matches(text, NEGATIVE_PATTERNS)
            and _bounds_center(_xml_attr(node, "bounds"))
        ):
            return node

    for node in nodes:
        text = _node_text(node)
        if text and _is_clickable(node) and _matches(text, NEGATIVE_PATTERNS) and _bounds_center(_xml_attr(node, "bounds")):
            return node

    return None


def open_chrome_for_signin(serial: str | None) -> None:
    r = adb(
        [
            "shell",
            "am",
            "start",
            "-a",
            "android.intent.action.VIEW",
            "-n",
            f"{CHROME_PACKAGE}/{CHROME_ACTIVITY}",
            "-d",
            ACCOUNTS_URL,
        ],
        serial=serial,
        check=False,
    )
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0:
        raise ExtractError(f"Failed to launch Chrome:\n{out}")


def prime_chrome_signin(serial: str | None, email: str | None = None, timeout_s: int = 55) -> dict:
    """Open Chrome on the phone and click through common Google sign-in prompts.

    This intentionally stays UI-based: Chrome is the registered Google client,
    so once it requests OAuthLogin, extract_tokens.py can read the resulting
    AccountManager token. The phone must be unlocked and visible.
    """
    open_chrome_for_signin(serial)
    deadline = time.time() + timeout_s
    clicks: list[str] = []
    last_text = ""

    while time.time() < deadline:
        root = _dump_ui(serial)
        if root is None:
            time.sleep(1)
            continue

        visible = []
        for node in root.iter("node"):
            text = _node_text(node)
            if text:
                visible.append(text)
        last_text = " | ".join(visible[:12])

        if any("myaccount.google" in t.lower() for t in visible) or any("google account" in t.lower() for t in visible):
            return {"ok": True, "clicks": clicks, "note": "Chrome reached a Google account page."}

        target = _find_click_target(root, email)
        if target is None:
            time.sleep(1)
            continue

        label = _node_text(target)
        if _click_node(serial, target):
            clicks.append(label[:120])
            time.sleep(2)

        if len(clicks) >= 8:
            break

    return {
        "ok": bool(clicks),
        "clicks": clicks,
        "note": "Timed out before a clear signed-in page was detected.",
        "last_text": last_text,
    }
