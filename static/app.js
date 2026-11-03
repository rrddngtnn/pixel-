(() => {
  const $ = (s) => document.querySelector(s);
  const deviceSel   = $("#device");
  const badgeAdb    = $("#badge-adb");
  const badgeDev    = $("#badge-device");
  const badgeRoot   = $("#badge-root");
  const btnRefresh  = $("#btn-refresh");
  const btnResetG1  = $("#btn-reset-g1");
  const btnScan     = $("#btn-scan");
  const allUrlsCbx  = $("#all-urls");
  const allAccountsCbx = $("#all-accounts");
  const statusHint  = $("#status-hint");
  const summary     = $("#summary");
  const resultsEl   = $("#results");
  const emptyEl     = $("#empty");
  const remoteBase  = $("#remote-base");
  const toast       = $("#toast");
  const btnToggleSettings = $("#btn-toggle-settings");
  const settingsPanel    = $("#settings-panel");
  const adbPathInput     = $("#adb-path");
  const btnSaveAdb       = $("#btn-save-adb");
  const btnClearAdb      = $("#btn-clear-adb");
  const badgeTokens      = $("#badge-tokens");
  const tokensDetail     = $("#tokens-detail");
  const btnPrimeChrome   = $("#btn-prime-chrome");
  const btnRefreshTokens = $("#btn-refresh-tokens");
  const autoClickCbx     = $("#auto-click");
  const claimLogCard     = $("#claim-log-card");
  const claimLogStatus   = $("#claim-log-status");
  const claimLog         = $("#claim-log");

  let tokensState = { has_tokens: false, accounts: [] };
  let claimLogTimer = null;
  let claimLogBusy = false;
  let activeClaimJobId = "";

  let toastTimer = null;
  function showToast(msg, kind = "") {
    toast.className = "toast show " + kind;
    toast.textContent = msg;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("show"), 2200);
  }

  function setBadge(el, label, kind) {
    el.textContent = label;
    el.classList.remove("ok", "bad", "warn");
    if (kind) el.classList.add(kind);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      const err = data.error || `HTTP ${res.status}`;
      throw new Error(err);
    }
    return data;
  }

  function selectedSerial() {
    return deviceSel.value || "";
  }

  async function refreshStatus() {
    btnRefresh.classList.add("loading");
    try {
      const serial = selectedSerial();
      const qs = serial ? `?serial=${encodeURIComponent(serial)}` : "";
      const s = await fetch("/api/status" + qs).then((r) => r.json());

      remoteBase.textContent = s.remote_base || remoteBase.textContent;

      if (typeof s.adb_path === "string" && document.activeElement !== adbPathInput) {
        adbPathInput.value = s.adb_path;
      }

      if (!s.adb) {
        setBadge(badgeAdb, "adb missing", "bad");
        setBadge(badgeDev, "no device", "bad");
        setBadge(badgeRoot, "root unknown", "bad");
        deviceSel.innerHTML = '<option value="">—</option>';
        statusHint.textContent =
          s.error ||
          "Install Android platform-tools and set the full adb path in Settings below, or add it to your system PATH.";
        settingsPanel.hidden = false;
        btnScan.disabled = true;
        return;
      }
      setBadge(badgeAdb, s.adb_path ? "adb (custom)" : "adb ready", "ok");

      const prev = deviceSel.value;
      deviceSel.innerHTML = "";
      if (!s.devices.length) {
        const opt = document.createElement("option");
        opt.value = ""; opt.textContent = "(no device detected)";
        deviceSel.appendChild(opt);
      } else {
        for (const d of s.devices) {
          const opt = document.createElement("option");
          opt.value = d.serial;
          opt.textContent = `${d.serial}  ·  ${d.state}`;
          deviceSel.appendChild(opt);
        }
      }
      const want = serial || s.selected || prev;
      if (want && [...deviceSel.options].some((o) => o.value === want)) {
        deviceSel.value = want;
      }

      if (!s.selected) {
        setBadge(badgeDev, "unselected", "warn");
        setBadge(badgeRoot, "root unknown", "warn");
        statusHint.textContent = s.devices.length
          ? "Multiple devices detected; select one from the dropdown."
          : "No device detected. Connect your phone and authorize USB debugging.";
        btnScan.disabled = true;
        return;
      }

      if (s.device_state !== "device") {
        setBadge(badgeDev, `device: ${s.device_state || "unknown"}`, "bad");
        setBadge(badgeRoot, "root unknown", "warn");
        statusHint.textContent = "Device not ready; check USB debugging authorization on the phone.";
        btnScan.disabled = true;
        return;
      }
      setBadge(badgeDev, "device ready", "ok");

      if (s.root) {
        setBadge(badgeRoot, "root granted", "ok");
        statusHint.textContent = "";
        btnScan.disabled = false;
      } else {
        setBadge(badgeRoot, "root denied", "bad");
        statusHint.textContent =
          "adb shell does not have root access.\nGrant Superuser/root permission to 'Shell' in Magisk / KernelSU / APatch.\n" +
          (s.root_output || "");
        btnScan.disabled = true;
      }
    } catch (e) {
      showToast("Refresh failed: " + e.message, "error");
    } finally {
      btnRefresh.classList.remove("loading");
    }
  }

  function renderResults(data) {
    resultsEl.innerHTML = "";
    const { accounts, total, mode, scanned, total_dirs } = data;
    emptyEl.classList.add("hidden");
    const scopePart = (scanned != null && total_dirs != null)
      ? ` · scanned ${scanned}/${total_dirs} dir(s)`
      : ` · ${accounts.length} account(s)`;
    summary.textContent =
      `${total} link(s)${scopePart} · mode: ${mode === "all" ? "all one.google.com" : "offer"}`;

    if (!accounts.length) {
      emptyEl.classList.remove("hidden");
      return;
    }

    for (const acc of accounts) {
      const card = document.createElement("div");
      card.className = "account-card";

      const head = document.createElement("div");
      head.className = "account-head";
      head.innerHTML = `<h3>Account <code>${escapeHtml(shortId(acc.account))}</code></h3>`;
      const count = document.createElement("span");
      count.className = "count";
      count.textContent = `${acc.urls.length} link(s)`;
      head.appendChild(count);
      card.appendChild(head);

      if (!acc.urls.length) {
        const note = document.createElement("div");
        note.className = "note";
        note.textContent = acc.note || "(none)";
        card.appendChild(note);
      } else {
        const ul = document.createElement("ul");
        ul.className = "url-list";
        for (const u of acc.urls) {
          ul.appendChild(renderUrlRow(u));
        }
        card.appendChild(ul);
      }
      resultsEl.appendChild(card);
    }
  }

  function toPartnerUrl(url) {
    const offerPrefix = "https://one.google.com/offer/";
    const partnerPrefix = "https://one.google.com/partner-eft-onboard/";
    if (url.startsWith(offerPrefix)) {
      return partnerPrefix + url.slice(offerPrefix.length);
    }
    return url;
  }

  function renderUrlRow(url) {
    const li = document.createElement("li");

    const span = document.createElement("span");
    span.className = "url-text";
    span.textContent = url;

    const copy = document.createElement("button");
    copy.className = "link-btn copy";
    copy.textContent = "Copy /offer/";
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(url);
        copy.classList.add("ok");
        copy.textContent = "Copied";
        setTimeout(() => { copy.classList.remove("ok"); copy.textContent = "Copy /offer/"; }, 1200);
      } catch {
        showToast("Copy failed; please copy manually", "error");
      }
    });

    const partnerUrl = toPartnerUrl(url);
    const copyPartner = document.createElement("button");
    copyPartner.className = "link-btn copy";
    copyPartner.textContent = "Copy EFT";
    copyPartner.title = partnerUrl;
    copyPartner.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(partnerUrl);
        copyPartner.classList.add("ok");
        copyPartner.textContent = "Copied EFT";
        setTimeout(() => { copyPartner.classList.remove("ok"); copyPartner.textContent = "Copy EFT"; }, 1200);
      } catch {
        showToast("Copy failed; please copy manually", "error");
      }
    });

    const open = document.createElement("button");
    open.className = "link-btn";
    open.textContent = "Open";
    open.addEventListener("click", async () => {
      try {
        await api("/api/open", { method: "POST", body: JSON.stringify({ url }) });
        showToast("Opened in default browser", "success");
      } catch (e) {
        showToast("Open failed: " + e.message, "error");
      }
    });

    const claim = document.createElement("button");
    claim.className = "link-btn claim-btn";
    claim.textContent = "Auto Claim";
    const hasUsable = (tokensState.accounts || []).some((a) => a.has_oauth_login);
    claim.disabled = !hasUsable;
    claim.title = hasUsable ? "Launch local Chromium with injected cookies and claim" : "Click 'Refresh Tokens' first";
    claim.addEventListener("click", () => autoClaim(url, claim));

    li.appendChild(span);
    li.appendChild(copy);
    if (partnerUrl !== url) {
      li.appendChild(copyPartner);
    }
    li.appendChild(open);
    li.appendChild(claim);
    return li;
  }

  function shortId(id) {
    if (!id || id.length <= 14) return id;
    return id.slice(0, 8) + "…" + id.slice(-4);
  }

  function formatAgo(ts) {
    if (!ts) return "";
    const delta = Math.max(0, Date.now() / 1000 - ts);
    if (delta < 60) return `${Math.floor(delta)}s ago`;
    if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
    if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
    return `${Math.floor(delta / 86400)}d ago`;
  }

  function renderTokensState(s) {
    tokensState = s || { has_tokens: false, accounts: [] };
    const accounts = tokensState.accounts || [];
    const usable = accounts.filter((a) => a.has_oauth_login);

    if (!tokensState.has_tokens) {
      setBadge(badgeTokens, "tokens not loaded", "warn");
      tokensDetail.textContent = "Click 'Prime Chrome' or sign into Chrome on the phone, then click 'Refresh Tokens'";
    } else if (!usable.length) {
      setBadge(badgeTokens, "no usable OAuth", "bad");
      tokensDetail.textContent =
        `${accounts.length} account(s) in .tokens.json, but none have an OAuthLogin token; ` +
        `click 'Prime Chrome' to authorize mobile Chrome`;
    } else {
      setBadge(badgeTokens, `${usable.length}/${accounts.length} account(s) ready`, "ok");
      const age = tokensState.refreshed_at ? formatAgo(tokensState.refreshed_at) : "";
      const stale = tokensState.refreshed_at &&
        Date.now() / 1000 - tokensState.refreshed_at > 45 * 60;
      const emails = usable.map((a) => a.email).join(", ");
      tokensDetail.textContent = `${emails} · refreshed ${age}` + (stale ? " (may be expired)" : "");
    }

    for (const btn of document.querySelectorAll(".claim-btn")) {
      btn.disabled = !usable.length;
      btn.title = usable.length ? "Launch local Chromium with injected cookies and claim" : "Click 'Refresh Tokens' first";
    }
  }

  async function refreshTokensStatus() {
    try {
      const s = await fetch("/api/tokens").then((r) => r.json());
      if (s.ok) renderTokensState(s);
    } catch {}
  }

  async function refreshTokens() {
    btnRefreshTokens.classList.add("loading");
    try {
      const data = await api("/api/refresh-tokens", {
        method: "POST",
        body: JSON.stringify({ serial: selectedSerial() || null }),
      });
      renderTokensState(data);
      const usable = (data.accounts || []).filter((a) => a.has_oauth_login).length;
      showToast(
        usable
          ? `Tokens refreshed: ${usable} account(s) ready`
          : "Tokens pulled, but no OAuthLogin token found",
        usable ? "success" : ""
      );
    } catch (e) {
      showToast("Refresh failed: " + e.message, "error");
    } finally {
      btnRefreshTokens.classList.remove("loading");
    }
  }

  async function primeChrome() {
    btnPrimeChrome.classList.add("loading");
    try {
      const data = await api("/api/prime-chrome", {
        method: "POST",
        body: JSON.stringify({ serial: selectedSerial() || null }),
      });
      renderTokensState(data);
      const usable = (data.accounts || []).filter((a) => a.has_oauth_login).length;
      if (usable) {
        showToast(`Chrome primed: ${usable} account(s) ready`, "success");
      } else {
        const note = data.chrome && data.chrome.note ? `: ${data.chrome.note}` : "";
        showToast("Chrome sign-in attempted, but still no OAuthLogin token" + note, "");
      }
    } catch (e) {
      showToast("Prime Chrome failed: " + e.message, "error");
    } finally {
      btnPrimeChrome.classList.remove("loading");
    }
  }

  async function resetGoogleOne() {
    if (!btnResetG1) return;
    btnResetG1.classList.add("loading");
    btnResetG1.disabled = true;
    try {
      const data = await api("/api/reset-google-one", {
        method: "POST",
        body: JSON.stringify({ serial: selectedSerial() || null }),
      });
      showToast(`Google One app data cleared (${data.serial})`, "success");
    } catch (e) {
      showToast("Reset Google One failed: " + e.message, "error");
    } finally {
      btnResetG1.classList.remove("loading");
      btnResetG1.disabled = false;
    }
  }

  function stopClaimLogPolling() {
    if (claimLogTimer) {
      clearInterval(claimLogTimer);
      claimLogTimer = null;
    }
  }

  async function updateClaimLog(jobId) {
    if (!jobId) return false;
    if (claimLogBusy) return true;
    claimLogBusy = true;
    try {
      const data = await fetch(`/api/auto-claim/${encodeURIComponent(jobId)}`).then((r) => r.json());
      if (!data.ok) throw new Error(data.error || "Failed to read log");
      if (jobId !== activeClaimJobId) return true;

      claimLogCard.hidden = false;
      claimLog.textContent = data.log || "";
      claimLog.scrollTop = claimLog.scrollHeight;

      if (data.running) {
        claimLogStatus.textContent = `Running · PID ${data.pid}`;
      } else {
        const code = data.returncode == null ? "unknown" : data.returncode;
        claimLogStatus.textContent = `Finished · exit code ${code}`;
      }
      return Boolean(data.running);
    } catch (e) {
      claimLogCard.hidden = false;
      claimLogStatus.textContent = "Log read error";
      document.body.classList.add("show-claim-log");
      claimLog.textContent += `\n[Frontend] ${e.message}`;
      return false;
    } finally {
      claimLogBusy = false;
    }
  }

  function startClaimLog(jobId) {
    stopClaimLogPolling();
    activeClaimJobId = jobId;
    document.body.classList.add("show-claim-log");
    claimLogCard.hidden = false;
    claimLogStatus.textContent = `Starting · ${jobId.slice(0, 8)}`;
    claimLog.textContent = "Background process started; waiting for output...\n";

    const tick = async () => {
      const running = await updateClaimLog(jobId);
      if (!running) stopClaimLogPolling();
    };
    tick();
    claimLogTimer = setInterval(tick, 1000);
  }

  async function autoClaim(url, btn) {
    if (btn) {
      btn.disabled = true;
      btn.classList.add("loading");
    }
    try {
      const res = await api("/api/auto-claim", {
        method: "POST",
        body: JSON.stringify({
          url,
          serial: selectedSerial() || null,
          auto_click: autoClickCbx.checked,
        }),
      });
      showToast(`Started auto-claim: ${res.email} · PID ${res.pid}`, "success");
      if (res.job_id) startClaimLog(res.job_id);
      if (btn) {
        btn.textContent = "Started";
        setTimeout(() => {
          btn.textContent = "Auto Claim";
          btn.disabled = !tokensState.accounts.some((a) => a.has_oauth_login);
        }, 3000);
      }
    } catch (e) {
      showToast("Auto-claim failed: " + e.message, "error");
      if (btn) btn.textContent = "Auto Claim";
    } finally {
      if (btn) btn.classList.remove("loading");
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  async function scan() {
    btnScan.classList.add("loading");
    btnScan.disabled = true;
    summary.textContent = "Scanning…";
    resultsEl.innerHTML = "";
    try {
      const data = await api("/api/scan", {
        method: "POST",
        body: JSON.stringify({
          serial: selectedSerial() || null,
          all_urls: allUrlsCbx.checked,
          all_accounts: allAccountsCbx.checked,
        }),
      });
      renderResults(data);
      if (data.total === 0) {
        showToast("No offer links matched; try checking 'Match all one.google.com URLs'", "");
      } else {
        showToast(`Extracted ${data.total} link(s)`, "success");
      }
    } catch (e) {
      summary.textContent = "";
      showToast("Scan failed: " + e.message, "error");
    } finally {
      btnScan.classList.remove("loading");
      btnScan.disabled = false;
    }
  }

  btnRefresh.addEventListener("click", refreshStatus);
  if (btnResetG1) btnResetG1.addEventListener("click", resetGoogleOne);
  btnScan.addEventListener("click", scan);
  deviceSel.addEventListener("change", refreshStatus);

  btnToggleSettings.addEventListener("click", () => {
    settingsPanel.hidden = !settingsPanel.hidden;
    if (!settingsPanel.hidden) adbPathInput.focus();
  });

  async function saveAdbPath(path) {
    btnSaveAdb.classList.add("loading");
    try {
      await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({ adb_path: path }),
      });
      showToast(path ? "Saved custom adb path" : "Cleared custom adb path", "success");
      await refreshStatus();
    } catch (e) {
      showToast("Save failed: " + e.message, "error");
    } finally {
      btnSaveAdb.classList.remove("loading");
    }
  }

  btnSaveAdb.addEventListener("click", () => saveAdbPath(adbPathInput.value.trim()));
  adbPathInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") saveAdbPath(adbPathInput.value.trim());
  });
  btnClearAdb.addEventListener("click", () => {
    adbPathInput.value = "";
    saveAdbPath("");
  });
  btnPrimeChrome.addEventListener("click", primeChrome);
  btnRefreshTokens.addEventListener("click", refreshTokens);

  refreshStatus();
  refreshTokensStatus();
})();
