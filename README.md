# Google One Partner Offer Extractor

🔥 Extract Google One partner-offer redemption links directly from a rooted Pixel (or Pixel-spoofed device) and automate browser claiming with OAuth session exchange and Closure UI patching.

Cache database path on the device:

```text
/data/user/0/com.google.android.apps.subscriptions.red/files/accounts/<id>/SqliteKeyValueCache:PartnerOfferEligibilityCache.db
```

Because the database filename contains `:`, Windows cannot save it directly by its original name. When full database pulling is enabled for inspection, the tool stages a copy under `/sdcard/` before running `adb pull`; by default, fast on-device scanning reads matching strings directly without pulling the entire SQLite file.

## What's Updated in This Fork

- 🔗 **Dual URL Format Support (`/offer/` & `/partner-eft-onboard/`)**: `extract_offers.py` now supports `--format {offer,partner,both}` (defaulting to `both`), and the Web UI provides dedicated **Copy /offer/** and **Copy EFT** buttons per link.
- ♻️ **Automatic Partner EFT Fallback**: `auto_claim.py` automatically switches between `https://one.google.com/offer/<token>` and `https://one.google.com/partner-eft-onboard/<token>` when the initial landing page fails to initialize or has an unresponsive CTA.
- 🖥️ **Cross-Platform Headless-Ready 50% Zoom**: Replaced the Windows-only `ctypes.windll.user32.SendInput` mouse wheel hijack with cross-platform CSS/DOM zoom (`document.documentElement.style.zoom = '0.5'`) and a `1440x1600` viewport (retaining `--native-win-zoom` as an optional fallback).
- 🛠️ **Cross-Platform OAuth Helper APK Builder**: Updated `oauth_helper/build_oauth_helper.py` to resolve JDK and Android SDK Build-Tools binaries across Linux, macOS, and Windows instead of hardcoding `.exe`/`.bat` paths.
- 🧹 **One-Click Google One Session Reset**: Added `--reset-google-one` in `extract_offers.py` and a **Reset Google One** button (`/api/reset-google-one`) in the Web UI to run `am force-stop` and `pm clear com.google.android.apps.subscriptions.red` between accounts.
- 🌐 **Full English Interface**: Translated the Flask Web UI, frontend status logs, and CLI output from Chinese to English.

## Prerequisites

- 🐍 **Python 3.8+** with `pip`
- 🔌 **`adb` (Android SDK Platform-Tools)** configured via one of three methods:
  1. **System PATH (permanent)**: Add `platform-tools` to your OS `PATH`.
  2. **Environment variable**: Set `ADB_PATH` to the full path of `adb` / `adb.exe`.
  3. **Web UI Settings**: Enter the `adb` binary path in the Web UI Settings panel (persisted in `.settings.json`).
- 📱 **Rooted Android Device (or Rooted AVD)**:
  - Rooted with **Magisk**, **KernelSU**, or **APatch**, with Superuser permission granted to **Shell** (`com.android.shell`).
  - USB debugging enabled and authorized for your computer.
  - Signed into the **Google One** app (`com.google.android.apps.subscriptions.red`) and opened at least once so the eligibility cache is populated.

## Option 1: Web UI

```bash
pip install -r requirements.txt
playwright install chromium
python app.py
```

The server starts at [http://127.0.0.1:7373](http://127.0.0.1:7373) and opens your browser automatically:

- 🔍 Inspect live `adb`, `device`, and `root` status badges and switch between connected device serials.
- 📦 Click **Scan Offer Links** to list extracted links grouped by account directory, with one-click **Copy /offer/**, **Copy EFT**, **Open**, and **Auto Claim** actions.
- 🔑 Click **Prime Chrome** to open mobile Chrome on the phone, click through account sign-in prompts, and refresh cached `OAuthLogin` tokens.
- ⚡ Click **Reset Google One** after finishing an account to clear Google One app data before signing into the next account.

## Option 2: Command Line

```bash
# Default: print both /offer/ and /partner-eft-onboard/ links for the newest account directory
python extract_offers.py

# Output only /offer/ or only /partner-eft-onboard/ URLs
python extract_offers.py --format offer
python extract_offers.py --format partner

# Target a specific device serial when multiple phones/emulators are connected
python extract_offers.py --serial 1A2B3C4D

# Widen matching to all https://one.google.com/... URLs
python extract_offers.py --all-urls

# Keep pulled SQLite cache files on disk for local inspection
python extract_offers.py --keep --out-dir ./dbs

# Force the full adb-pull + local SQLite/raw-byte scan path
python extract_offers.py --pull-db

# Force-stop and clear Google One app data on the device between accounts
python extract_offers.py --reset-google-one
```

By default, `extract_offers.py` uses the fast on-device scan path (`strings -n 8 | grep one.google.com` over root shell) and applies regex extraction locally so the full SQLite file does not need to be copied over USB on every run.

## Sample Output

```text
[i] Scanning newest account dir only: 1 (use --all-accounts to scan all)
[*] Found 1 account dir(s): 1
[i] Using fast on-device scan (strings/grep, no DB pull)
[+] account 1: 2 url(s)

============================================================
Extracted links:
============================================================

# account 1
https://one.google.com/offer/XXXX
https://one.google.com/partner-eft-onboard/XXXX
```

✅ Copy either link into a browser signed into the same Google account, or run `auto_claim.py` to mint web cookies from the phone's `OAuthLogin` token automatically.

## Automated Claiming

Recommended Web UI workflow:

1. Click **Prime Chrome** so the tool opens Chrome on the phone, accepts the account sign-in prompt, and refreshes `.tokens.json`.
2. Click **Scan Offer Links** to list the extracted URLs for the active account.
3. Click **Auto Claim** next to the target offer URL to launch `auto_claim.py` in the background and stream live progress logs in the UI.

You can also run the token extractor and auto-claimer directly from the terminal:

```bash
# 1. Pull master tokens, OAuthLogin tokens, and GSF Android ID into .tokens.json
python extract_tokens.py --save

# 2. Verify cookie minting via OAuthLogin -> MergeSession
python google_auth.py

# 3. Launch Chromium with injected cookies, patch the Google One UI, and auto-click Claim
python auto_claim.py --url "https://one.google.com/offer/XXXX" --auto-click

# Refresh stale tokens (>45m) automatically and retry once on badauth
python auto_claim.py --url "https://one.google.com/offer/XXXX" --serial 1A2B3C4D --refresh-stale-tokens --retry-badauth --auto-click

# Run headless
python auto_claim.py --url "https://one.google.com/offer/XXXX" --headless --auto-click
```

How `auto_claim.py` works under the hood:

1. Refreshes `.tokens.json` from the phone if the file is older than 45 minutes (when `--refresh-stale-tokens` is enabled).
2. Exchanges the phone's `ya29.m.*` `OAuthLogin` token at `accounts.google.com/OAuthLogin?source=ChromiumBrowser&issueuberauth=1` for a single-use `uberauth` token, then hits `accounts.google.com/MergeSession` to mint authenticated `.google.com` cookies (`SID`, `HSID`, `SSID`, `APISID`, `SAPISID`, `__Secure-1PSID`, `__Secure-3PSID`).
3. Launches an isolated Chromium context (`1440x1600`), injects the cookies, and navigates directly to the offer URL.
4. Applies 50% CSS/DOM zoom (`document.documentElement.style.zoom = '0.5'`) and patches `window.default_SubscriptionsManagementUi.sxa` across `[jscontroller="ytxTId"]` nodes so unresponsive Google One trial buttons become clickable.
5. Automatically falls back between `/offer/<token>` and `/partner-eft-onboard/<token>` if the initial URL fails to ready its controller nodes or has no clickable CTA.

## Troubleshooting

- **`Root (su) is not available`**: Open Magisk, KernelSU, or APatch manager and grant Superuser access to `Shell` (`com.android.shell`).
- **`No accounts found`**: Sign into the Google One app (`com.google.android.apps.subscriptions.red`) on the phone and open the Benefits/Offers tab once so the SQLite cache is written.
- **No matching links found**: The eligibility cache may not have populated yet, or the device environment did not pass Pixel hardware/integrity checks. Refresh the Google One app or run `python extract_offers.py --all-urls` to inspect all cached `one.google.com` URLs.
- **`OAuthLogin returned HTTP 403 / badauth`**: The cached `OAuthLogin` token on the phone has expired (~1 hour lifetime). Click **Prime Chrome** or **Refresh Tokens** in the Web UI, or pass `--refresh-stale-tokens --retry-badauth` to `auto_claim.py`.
- **Browser redirects to Google Sign-In**: The cookies were rejected or the account was not authorized in mobile Chrome. Run **Prime Chrome** on an unlocked phone screen first, then retry.

## Credits

- Original project and core extraction/OAuth architecture by [mxysw](https://github.com/mxysw/pixel-).
- Updated and maintained in this fork by [rrddngtnn](https://github.com/rrddngtnn/pixel-) with English localization, dual `/offer/` & `/partner-eft-onboard/` URL extraction and automatic fallback, cross-platform headless CSS zoom, cross-platform `oauth_helper` APK building, and Google One session reset tooling.
