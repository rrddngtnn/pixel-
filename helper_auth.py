from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

from extract_offers import ExtractError, adb


BASE_DIR = Path(__file__).resolve().parent
HELPER_DIR = BASE_DIR / "oauth_helper"
HELPER_APK = HELPER_DIR / "build" / "pixel-oauth-helper.apk"
HELPER_BUILDER = HELPER_DIR / "build_oauth_helper.py"
HELPER_PACKAGE = "com.pixel.oauthhelper"
HELPER_COMPONENT = f"{HELPER_PACKAGE}/.MainActivity"
HELPER_RESULT = f"/data/data/{HELPER_PACKAGE}/files/token.json"
OAUTH_LOGIN_SCOPE = "oauth2:https://www.google.com/accounts/OAuthLogin"


def ensure_helper_apk() -> Path:
    if HELPER_APK.exists():
        return HELPER_APK
    if not HELPER_BUILDER.exists():
        raise ExtractError(f"OAuth helper builder not found: {HELPER_BUILDER}")
    try:
        subprocess.run(
            [sys.executable, str(HELPER_BUILDER)],
            cwd=str(HELPER_DIR),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.CalledProcessError as e:
        out = ((e.stdout or "") + "\n" + (e.stderr or "")).strip()
        raise ExtractError(f"Failed to build OAuth helper APK:\n{out}")
    if not HELPER_APK.exists():
        raise ExtractError(f"OAuth helper build finished but APK is missing: {HELPER_APK}")
    return HELPER_APK


def install_helper(serial: str | None) -> None:
    apk = ensure_helper_apk()
    remote_apk = "/data/local/tmp/pixel-oauth-helper.apk"
    pushed = adb(["push", str(apk), remote_apk], serial=serial, check=False)
    if pushed.returncode != 0:
        out = ((pushed.stdout or "") + (pushed.stderr or "")).strip()
        raise ExtractError(f"Failed to push OAuth helper APK:\n{out}")

    old_verify = _settings_get(serial, "verifier_verify_adb_installs")
    old_package_verify = _settings_get(serial, "package_verifier_enable")
    try:
        _settings_put(serial, "verifier_verify_adb_installs", "0")
        _settings_put(serial, "package_verifier_enable", "0")
        r = adb(
            ["shell", "su", "-c", f"pm install -r -i com.android.shell {shlex.quote(remote_apk)}"],
            serial=serial,
            check=False,
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if r.returncode != 0 and "INSTALL_FAILED_UPDATE_INCOMPATIBLE" in out:
            adb(["shell", "pm", "uninstall", HELPER_PACKAGE], serial=serial, check=False)
            r = adb(
                ["shell", "su", "-c", f"pm install -r -i com.android.shell {shlex.quote(remote_apk)}"],
                serial=serial,
                check=False,
            )
            out = ((r.stdout or "") + (r.stderr or "")).strip()
        if r.returncode != 0 or "Success" not in out:
            raise ExtractError(f"Failed to install OAuth helper APK:\n{out}")
    finally:
        _settings_restore(serial, "verifier_verify_adb_installs", old_verify)
        _settings_restore(serial, "package_verifier_enable", old_package_verify)
        adb(["shell", "rm", "-f", remote_apk], serial=serial, check=False)

    # Avoid a separate runtime permission dialog when adb can grant the normal
    # dangerous permission directly. Google account consent may still appear.
    adb(
        ["shell", "pm", "grant", HELPER_PACKAGE, "android.permission.GET_ACCOUNTS"],
        serial=serial,
        check=False,
    )


def _settings_get(serial: str | None, key: str) -> str:
    r = adb(["shell", "su", "-c", f"settings get global {shlex.quote(key)}"], serial=serial, check=False)
    return (r.stdout or "").strip()


def _settings_put(serial: str | None, key: str, value: str) -> None:
    adb(
        ["shell", "su", "-c", f"settings put global {shlex.quote(key)} {shlex.quote(value)}"],
        serial=serial,
        check=False,
    )


def _settings_restore(serial: str | None, key: str, value: str) -> None:
    if not value or value == "null":
        cmd = f"settings delete global {shlex.quote(key)}"
    else:
        cmd = f"settings put global {shlex.quote(key)} {shlex.quote(value)}"
    adb(["shell", "su", "-c", cmd], serial=serial, check=False)


def _clear_result(serial: str | None) -> None:
    adb(
        ["shell", "su", "-c", f"rm -f {shlex.quote(HELPER_RESULT)}"],
        serial=serial,
        check=False,
    )


def _read_result(serial: str | None) -> dict | None:
    r = adb(
        ["shell", "su", "-c", f"cat {shlex.quote(HELPER_RESULT)} 2>/dev/null"],
        serial=serial,
        check=False,
    )
    raw = (r.stdout or "").strip()
    if not raw:
        return None
    start = raw.find("{")
    if start > 0:
        raw = raw[start:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ExtractError(f"OAuth helper wrote invalid JSON: {e}\n{raw[:200]}")


def _remove_result(serial: str | None) -> None:
    _clear_result(serial)


def request_oauth_login_token(serial: str | None, email: str | None = None, timeout_s: int = 150) -> dict:
    """Install/launch the Android helper and return {email, oauth_login_token, ...}.

    The helper uses the phone's existing Google account via AccountManager. It
    may show a one-time account chooser or Google authorization prompt on the
    phone, but it does not require signing into mobile Chrome.
    """
    install_helper(serial)
    _clear_result(serial)

    cmd = [
        "shell",
        "am",
        "start",
        "-W",
        "-n",
        HELPER_COMPONENT,
        "--es",
        "scope",
        OAUTH_LOGIN_SCOPE,
    ]
    if email:
        cmd += ["--es", "email", email]
    r = adb(cmd, serial=serial, check=False)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0:
        raise ExtractError(f"Failed to launch OAuth helper:\n{out}")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = _read_result(serial)
        if result:
            _remove_result(serial)
            if not result.get("ok"):
                raise ExtractError(result.get("error") or "OAuth helper failed.")
            token = result.get("oauth_login_token")
            got_email = result.get("email")
            if not token or not got_email:
                raise ExtractError("OAuth helper result did not contain email/token.")
            return result
        time.sleep(1.0)

    raise ExtractError(
        "Timed out waiting for OAuth helper. Check the phone screen for an account chooser or Google authorization prompt."
    )
