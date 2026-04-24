"""
Local Flask UI for the Google One partner-offer extractor.

Run:
    pip install -r requirements.txt
    python app.py

Then open http://127.0.0.1:7373 in your browser.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from chrome_prime import prime_chrome_signin
from extract_offers import (
    ExtractError,
    ONE_RE,
    PARTNER_RE,
    REMOTE_BASE,
    device_state,
    extract_urls_from_db,
    has_adb,
    check_root,
    list_account_dirs,
    list_devices,
    normalize_offer_url,
    pull_db,
    scan_urls_on_device,
)
from extract_tokens import collect_tokens

app = Flask(__name__, template_folder="templates", static_folder="static")

BASE_DIR = Path(__file__).parent
SETTINGS_FILE = BASE_DIR / ".settings.json"
TOKENS_FILE = BASE_DIR / ".tokens.json"
AUTO_CLAIM_SCRIPT = BASE_DIR / "auto_claim.py"
AUTO_CLAIM_LOG_DIR = BASE_DIR / ".auto_claim_logs"
AUTO_CLAIM_LOG_LIMIT = 128 * 1024
AUTO_CLAIM_JOBS: dict[str, dict] = {}
AUTO_CLAIM_LOCK = threading.Lock()


def _read_log_tail(path: Path, limit: int = AUTO_CLAIM_LOG_LIMIT) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            data = f.read()
    except OSError as e:
        return f"[后端] 读取日志失败：{e}"
    return data.decode("utf-8", errors="replace")


def read_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_settings(data: dict) -> None:
    if not data:
        if SETTINGS_FILE.exists():
            try:
                SETTINGS_FILE.unlink()
            except OSError:
                pass
        return
    SETTINGS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def update_settings(**fields) -> dict:
    """Merge ``fields`` into the on-disk settings (empty/None values remove the key)."""
    cur = read_settings()
    for k, v in fields.items():
        if v is None or v == "":
            cur.pop(k, None)
        else:
            cur[k] = v
    write_settings(cur)
    return cur


def load_settings() -> None:
    """Apply persisted settings to the process env (called at import time)."""
    data = read_settings()
    adb_path = (data.get("adb_path") or "").strip()
    if adb_path and Path(adb_path).is_file():
        os.environ["ADB_PATH"] = adb_path


def _pick_serial(requested: str | None, devices: list[dict]) -> str | None:
    if requested:
        return requested
    ready = [d for d in devices if d["state"] == "device"]
    if len(ready) == 1:
        return ready[0]["serial"]
    return None


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    serial = request.args.get("serial") or None

    current_adb_path = (os.environ.get("ADB_PATH") or "").strip()

    if not has_adb():
        return jsonify(
            adb=False,
            adb_path=current_adb_path,
            devices=[],
            selected=None,
            device_state=None,
            root=False,
            root_output="",
            error=(
                "adb not found. Set ADB_PATH, or add platform-tools to your PATH, "
                "or enter the path to adb.exe below."
            ),
        )

    devices = list_devices()
    selected = _pick_serial(serial, devices)

    state = None
    root_ok = False
    root_out = ""
    if selected:
        state = device_state(selected)
        if state == "device":
            root_ok, root_out = check_root(selected)

    return jsonify(
        adb=True,
        adb_path=current_adb_path,
        devices=devices,
        selected=selected,
        device_state=state,
        root=root_ok,
        root_output=root_out,
        remote_base=REMOTE_BASE,
    )


@app.get("/api/settings")
def api_settings_get():
    data = read_settings()
    return jsonify(ok=True, adb_path=data.get("adb_path") or "")


@app.post("/api/settings")
def api_settings_post():
    data = request.get_json(silent=True) or {}
    updates: dict = {}

    if "adb_path" in data:
        adb_path = (data.get("adb_path") or "").strip().strip('"').strip("'")
        if adb_path:
            if not Path(adb_path).is_file():
                return jsonify(ok=False, error=f"Path not found: {adb_path}"), 400
            os.environ["ADB_PATH"] = adb_path
        else:
            os.environ.pop("ADB_PATH", None)
        updates["adb_path"] = adb_path

    merged = update_settings(**updates)
    return jsonify(ok=True, adb_path=merged.get("adb_path") or "")


@app.post("/api/scan")
def api_scan():
    data = request.get_json(silent=True) or {}
    serial = data.get("serial") or None
    all_urls = bool(data.get("all_urls"))
    all_accounts = bool(data.get("all_accounts"))
    pattern = ONE_RE if all_urls else PARTNER_RE

    if not has_adb():
        return jsonify(ok=False, error="adb not found in PATH"), 400

    devices = list_devices()
    selected = _pick_serial(serial, devices)
    if not selected:
        return jsonify(ok=False, error="No device selected. Multiple or zero devices connected."), 400

    state = device_state(selected)
    if state != "device":
        return jsonify(ok=False, error=f"Device not ready (state: {state or 'unknown'})"), 400

    ok, root_out = check_root(selected)
    if not ok:
        return jsonify(ok=False, error=f"Root (su) unavailable. Grant root to Shell in Magisk.\n{root_out}"), 400

    try:
        accounts = list_account_dirs(selected)
    except ExtractError as e:
        return jsonify(ok=False, error=str(e)), 400

    total_dirs = len(accounts)
    if not all_accounts and accounts:
        accounts = accounts[:1]

    workdir: Path | None = None
    results = []
    try:
        for acc in accounts:
            entry = {"account": acc, "urls": [], "note": None}
            try:
                urls = scan_urls_on_device(selected, acc, pattern)
            except Exception as e:
                if workdir is None:
                    workdir = Path(tempfile.mkdtemp(prefix="gone_offers_"))
                try:
                    db = pull_db(selected, acc, workdir)
                except Exception as pull_e:
                    entry["note"] = f"fast scan failed: {e}; pull failed: {pull_e}"
                    results.append(entry)
                    continue
                if not db:
                    entry["note"] = f"fast scan failed: {e}; no cache db"
                    results.append(entry)
                    continue
                urls = extract_urls_from_db(db, pattern)
            if urls is None:
                entry["note"] = "no cache db"
                results.append(entry)
                continue
            entry["urls"] = urls
            if not entry["urls"]:
                entry["note"] = "no matching urls"
            results.append(entry)
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)

    total = sum(len(a["urls"]) for a in results)
    return jsonify(
        ok=True,
        accounts=results,
        total=total,
        mode="all" if all_urls else "partner",
        scanned=len(results),
        total_dirs=total_dirs,
    )


def _summarize_accounts(accounts: list[dict]) -> list[dict]:
    return [
        {
            "email": a.get("email"),
            "has_master_token": bool(a.get("master_token")),
            "has_oauth_login": bool(a.get("oauth_login_token")),
            "has_subscriptions": bool(a.get("subscriptions_token")),
        }
        for a in accounts
    ]


@app.get("/api/tokens")
def api_tokens_status():
    """Return what's in .tokens.json (if anything) + mtime, without re-scraping the phone."""
    if not TOKENS_FILE.exists():
        return jsonify(ok=True, has_tokens=False, accounts=[])
    try:
        data = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return jsonify(ok=False, error=f"unreadable .tokens.json: {e}"), 500
    return jsonify(
        ok=True,
        has_tokens=True,
        accounts=_summarize_accounts(data.get("accounts") or []),
        android_id=data.get("android_id"),
        refreshed_at=TOKENS_FILE.stat().st_mtime,
    )


@app.post("/api/refresh-tokens")
def api_refresh_tokens():
    """Pull OAuth tokens + Android ID fresh from the phone into .tokens.json."""
    data = request.get_json(silent=True) or {}
    serial = data.get("serial") or None

    if not has_adb():
        return jsonify(ok=False, error="adb not available"), 400

    devices = list_devices()
    selected = _pick_serial(serial, devices)
    if not selected:
        return jsonify(ok=False, error="No device selected."), 400

    state = device_state(selected)
    if state != "device":
        return jsonify(ok=False, error=f"Device not ready (state: {state or 'unknown'})"), 400

    ok, root_out = check_root(selected)
    if not ok:
        return jsonify(ok=False, error=f"Root unavailable.\n{root_out}"), 400

    try:
        result = collect_tokens(selected)
    except ExtractError as e:
        return jsonify(ok=False, error=str(e)), 400

    persisted = {
        "android_id": result["android_id"],
        "accounts": result["accounts"],
    }
    TOKENS_FILE.write_text(json.dumps(persisted, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(TOKENS_FILE, 0o600)
    except OSError:
        pass

    return jsonify(
        ok=True,
        has_tokens=True,
        accounts=_summarize_accounts(result["accounts"]),
        android_id=result["android_id"],
        refreshed_at=TOKENS_FILE.stat().st_mtime,
    )


@app.post("/api/prime-chrome")
def api_prime_chrome():
    """Open mobile Chrome, click common account/sync prompts, then refresh .tokens.json."""
    data = request.get_json(silent=True) or {}
    serial = data.get("serial") or None
    email = (data.get("email") or "").strip() or None

    if not has_adb():
        return jsonify(ok=False, error="adb not available"), 400

    devices = list_devices()
    selected = _pick_serial(serial, devices)
    if not selected:
        return jsonify(ok=False, error="No device selected."), 400

    state = device_state(selected)
    if state != "device":
        return jsonify(ok=False, error=f"Device not ready (state: {state or 'unknown'})"), 400

    chrome_result = prime_chrome_signin(selected, email=email)

    ok, root_out = check_root(selected)
    if not ok:
        return jsonify(ok=False, error=f"Chrome primed, but root unavailable for token refresh.\n{root_out}"), 400

    try:
        result = collect_tokens(selected)
    except ExtractError as e:
        return jsonify(ok=False, error=str(e), chrome=chrome_result), 400

    persisted = {
        "android_id": result["android_id"],
        "accounts": result["accounts"],
    }
    TOKENS_FILE.write_text(json.dumps(persisted, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(TOKENS_FILE, 0o600)
    except OSError:
        pass

    return jsonify(
        ok=True,
        has_tokens=True,
        accounts=_summarize_accounts(result["accounts"]),
        android_id=result["android_id"],
        refreshed_at=TOKENS_FILE.stat().st_mtime,
        chrome=chrome_result,
    )


@app.post("/api/auto-claim")
def api_auto_claim():
    """Spawn auto_claim.py in the background and expose its stdout through a log endpoint."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    email = (data.get("email") or "").strip() or None
    auto_click = bool(data.get("auto_click"))

    url = normalize_offer_url(url)
    if not (
        url.startswith("https://one.google.com/offer/")
        or url.startswith("https://one.google.com/partner-eft-onboard/")
    ):
        return jsonify(ok=False, error="Only Google One offer URLs are allowed."), 400

    if not TOKENS_FILE.exists():
        return jsonify(ok=False, error="No .tokens.json — click 刷新 tokens first."), 400

    try:
        tokens = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return jsonify(ok=False, error=f"bad .tokens.json: {e}"), 500

    usable = [a for a in (tokens.get("accounts") or []) if a.get("oauth_login_token")]
    if not usable:
        return jsonify(ok=False, error="No account has an OAuthLogin token. Refresh tokens."), 400
    if email and not any(a["email"].lower() == email.lower() for a in usable):
        return jsonify(ok=False, error=f"No OAuthLogin token for {email}"), 400

    if not AUTO_CLAIM_SCRIPT.exists():
        return jsonify(ok=False, error=f"auto_claim.py not found at {AUTO_CLAIM_SCRIPT}"), 500

    job_id = uuid.uuid4().hex
    AUTO_CLAIM_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = AUTO_CLAIM_LOG_DIR / f"{job_id}.log"

    cmd = [sys.executable, "-u", str(AUTO_CLAIM_SCRIPT), "--url", url, "--no-prompt"]
    if email:
        cmd += ["--email", email]
    if auto_click:
        cmd += ["--auto-click"]

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    log_fp = log_path.open("w", encoding="utf-8", errors="replace")
    log_fp.write(f"[后端] 任务：{job_id}\n")
    log_fp.write(f"[后端] 链接：{url}\n")
    log_fp.write(f"[后端] 自动点击：{'开启' if auto_click else '关闭'}\n")
    log_fp.flush()

    popen_kwargs: dict = {
        "cwd": str(BASE_DIR),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": log_fp,
        "stderr": subprocess.STDOUT,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except OSError as e:
        log_fp.close()
        return jsonify(ok=False, error=f"启动自动领取失败：{e}"), 500
    finally:
        try:
            log_fp.close()
        except OSError:
            pass

    with AUTO_CLAIM_LOCK:
        AUTO_CLAIM_JOBS[job_id] = {
            "proc": proc,
            "log_path": log_path,
            "started_at": time.time(),
            "url": url,
            "email": email or usable[0]["email"],
        }

    return jsonify(
        ok=True,
        pid=proc.pid,
        job_id=job_id,
        log_url=f"/api/auto-claim/{job_id}",
        email=email or usable[0]["email"],
    )


@app.get("/api/auto-claim/<job_id>")
def api_auto_claim_log(job_id: str):
    if not job_id or len(job_id) > 80 or any(c not in "0123456789abcdefABCDEF_-" for c in job_id):
        return jsonify(ok=False, error="任务 ID 无效。"), 400

    with AUTO_CLAIM_LOCK:
        job = AUTO_CLAIM_JOBS.get(job_id)

    if job:
        proc = job["proc"]
        returncode = proc.poll()
        running = returncode is None
        log_path = job["log_path"]
        return jsonify(
            ok=True,
            job_id=job_id,
            pid=proc.pid,
            running=running,
            returncode=returncode,
            started_at=job["started_at"],
            email=job.get("email"),
            log=_read_log_tail(log_path),
        )

    log_path = AUTO_CLAIM_LOG_DIR / f"{job_id}.log"
    if not log_path.exists():
        return jsonify(ok=False, error="找不到这个自动领取任务。"), 404
    return jsonify(
        ok=True,
        job_id=job_id,
        pid=None,
        running=False,
        returncode=None,
        started_at=None,
        email=None,
        log=_read_log_tail(log_path),
    )


@app.post("/api/open")
def api_open():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url.startswith("https://one.google.com/"):
        return jsonify(ok=False, error="Only one.google.com URLs are allowed."), 400
    webbrowser.open(url, new=2)
    return jsonify(ok=True)


def _open_browser(port: int) -> None:
    webbrowser.open(f"http://127.0.0.1:{port}", new=2)


if __name__ == "__main__":
    load_settings()
    port = 7373
    threading.Timer(1.0, _open_browser, args=[port]).start()
    app.run(host="127.0.0.1", port=port, debug=False)
