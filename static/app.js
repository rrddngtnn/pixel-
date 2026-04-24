(() => {
  const $ = (s) => document.querySelector(s);
  const deviceSel   = $("#device");
  const badgeAdb    = $("#badge-adb");
  const badgeDev    = $("#badge-device");
  const badgeRoot   = $("#badge-root");
  const btnRefresh  = $("#btn-refresh");
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
        setBadge(badgeAdb, "adb 未找到", "bad");
        setBadge(badgeDev, "无设备", "bad");
        setBadge(badgeRoot, "root 未知", "bad");
        deviceSel.innerHTML = '<option value="">—</option>';
        statusHint.textContent =
          s.error ||
          "请安装 Android platform-tools，并通过下方『设置』里填写 adb.exe 的完整路径，或把它加到系统 PATH。";
        settingsPanel.hidden = false;
        btnScan.disabled = true;
        return;
      }
      setBadge(badgeAdb, s.adb_path ? "adb (自定义)" : "adb 已就绪", "ok");

      const prev = deviceSel.value;
      deviceSel.innerHTML = "";
      if (!s.devices.length) {
        const opt = document.createElement("option");
        opt.value = ""; opt.textContent = "（未检测到设备）";
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
        setBadge(badgeDev, "未选择设备", "warn");
        setBadge(badgeRoot, "root 未知", "warn");
        statusHint.textContent = s.devices.length
          ? "检测到多台设备，请从下拉框选一台。"
          : "没有检测到设备。请插上手机并在提示里允许 USB 调试。";
        btnScan.disabled = true;
        return;
      }

      if (s.device_state !== "device") {
        setBadge(badgeDev, `设备: ${s.device_state || "未知"}`, "bad");
        setBadge(badgeRoot, "root 未知", "warn");
        statusHint.textContent = "设备未就绪，请检查 USB 调试授权。";
        btnScan.disabled = true;
        return;
      }
      setBadge(badgeDev, "设备已就绪", "ok");

      if (s.root) {
        setBadge(badgeRoot, "root 已授权", "ok");
        statusHint.textContent = "";
        btnScan.disabled = false;
      } else {
        setBadge(badgeRoot, "root 未授权", "bad");
        statusHint.textContent =
          "adb shell 没有 root 权限。\n请在 Magisk / KernelSU 管理器里把 root 授权给「Shell」。\n" +
          (s.root_output || "");
        btnScan.disabled = true;
      }
    } catch (e) {
      showToast("刷新失败：" + e.message, "error");
    } finally {
      btnRefresh.classList.remove("loading");
    }
  }

  function renderResults(data) {
    resultsEl.innerHTML = "";
    const { accounts, total, mode, scanned, total_dirs } = data;
    emptyEl.classList.add("hidden");
    const scopePart = (scanned != null && total_dirs != null)
      ? ` · 扫描 ${scanned}/${total_dirs} 个目录`
      : ` · ${accounts.length} 个账户`;
    summary.textContent =
      `共 ${total} 条链接${scopePart} · 模式: ${mode === "all" ? "all one.google.com" : "offer"}`;

    if (!accounts.length) {
      emptyEl.classList.remove("hidden");
      return;
    }

    for (const acc of accounts) {
      const card = document.createElement("div");
      card.className = "account-card";

      const head = document.createElement("div");
      head.className = "account-head";
      head.innerHTML = `<h3>账户 <code>${escapeHtml(shortId(acc.account))}</code></h3>`;
      const count = document.createElement("span");
      count.className = "count";
      count.textContent = `${acc.urls.length} 条`;
      head.appendChild(count);
      card.appendChild(head);

      if (!acc.urls.length) {
        const note = document.createElement("div");
        note.className = "note";
        note.textContent = acc.note || "（无）";
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

  function renderUrlRow(url) {
    const li = document.createElement("li");

    const span = document.createElement("span");
    span.className = "url-text";
    span.textContent = url;

    const copy = document.createElement("button");
    copy.className = "link-btn copy";
    copy.textContent = "复制";
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(url);
        copy.classList.add("ok");
        copy.textContent = "已复制";
        setTimeout(() => { copy.classList.remove("ok"); copy.textContent = "复制"; }, 1200);
      } catch {
        showToast("复制失败，请手动选中", "error");
      }
    });

    const open = document.createElement("button");
    open.className = "link-btn";
    open.textContent = "打开";
    open.addEventListener("click", async () => {
      try {
        await api("/api/open", { method: "POST", body: JSON.stringify({ url }) });
        showToast("已在默认浏览器打开", "success");
      } catch (e) {
        showToast("打开失败：" + e.message, "error");
      }
    });

    const claim = document.createElement("button");
    claim.className = "link-btn claim-btn";
    claim.textContent = "自动领取";
    const hasUsable = (tokensState.accounts || []).some((a) => a.has_oauth_login);
    claim.disabled = !hasUsable;
    claim.title = hasUsable ? "用本机 Chromium 打开并自动登录领取" : "先点『刷新 tokens』";
    claim.addEventListener("click", () => autoClaim(url, claim));

    li.appendChild(span);
    li.appendChild(copy);
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
    if (delta < 60) return `${Math.floor(delta)} 秒前`;
    if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
    if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
    return `${Math.floor(delta / 86400)} 天前`;
  }

  function renderTokensState(s) {
    tokensState = s || { has_tokens: false, accounts: [] };
    const accounts = tokensState.accounts || [];
    const usable = accounts.filter((a) => a.has_oauth_login);

    if (!tokensState.has_tokens) {
      setBadge(badgeTokens, "tokens 未加载", "warn");
      tokensDetail.textContent = "先点『准备 Chrome』或手动登录手机 Chrome，再点『刷新 tokens』";
    } else if (!usable.length) {
      setBadge(badgeTokens, "无可用 OAuth", "bad");
      tokensDetail.textContent =
        `${accounts.length} 个账户在 .tokens.json，但没有 OAuthLogin token；` +
        `点『准备 Chrome』自动完成手机端 Chrome 授权`;
    } else {
      setBadge(badgeTokens, `${usable.length}/${accounts.length} 账户可用`, "ok");
      const age = tokensState.refreshed_at ? formatAgo(tokensState.refreshed_at) : "";
      const stale = tokensState.refreshed_at &&
        Date.now() / 1000 - tokensState.refreshed_at > 45 * 60;
      const emails = usable.map((a) => a.email).join(", ");
      tokensDetail.textContent = `${emails} · 刷新于 ${age}` + (stale ? " (可能已过期)" : "");
    }

    for (const btn of document.querySelectorAll(".claim-btn")) {
      btn.disabled = !usable.length;
      btn.title = usable.length ? "用本机 Chromium 打开并自动登录领取" : "先点『刷新 tokens』";
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
          ? `已刷新 tokens：${usable} 个账号可用`
          : "已抠到 tokens，但没有可用的 OAuthLogin",
        usable ? "success" : ""
      );
    } catch (e) {
      showToast("刷新失败：" + e.message, "error");
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
        showToast(`Chrome 已准备好：${usable} 个账号可用`, "success");
      } else {
        const note = data.chrome && data.chrome.note ? `：${data.chrome.note}` : "";
        showToast("Chrome 已尝试授权，但仍没有 OAuthLogin" + note, "");
      }
    } catch (e) {
      showToast("准备 Chrome 失败：" + e.message, "error");
    } finally {
      btnPrimeChrome.classList.remove("loading");
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
      if (!data.ok) throw new Error(data.error || "日志读取失败");
      if (jobId !== activeClaimJobId) return true;

      claimLogCard.hidden = false;
      claimLog.textContent = data.log || "";
      claimLog.scrollTop = claimLog.scrollHeight;

      if (data.running) {
        claimLogStatus.textContent = `运行中 · PID ${data.pid}`;
      } else {
        const code = data.returncode == null ? "未知" : data.returncode;
        claimLogStatus.textContent = `已结束 · 退出码 ${code}`;
      }
      return Boolean(data.running);
    } catch (e) {
      claimLogCard.hidden = false;
      claimLogStatus.textContent = "日志读取失败";
      document.body.classList.add("show-claim-log");
      claimLog.textContent += `\n[前端] ${e.message}`;
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
    claimLogStatus.textContent = `启动中 · ${jobId.slice(0, 8)}`;
    claimLog.textContent = "后台进程已启动，正在等待日志...\n";

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
      showToast(`已后台启动领取流程：${res.email} · PID ${res.pid}`, "success");
      if (res.job_id) startClaimLog(res.job_id);
      if (btn) {
        btn.textContent = "已启动";
        setTimeout(() => {
          btn.textContent = "自动领取";
          btn.disabled = !tokensState.accounts.some((a) => a.has_oauth_login);
        }, 3000);
      }
    } catch (e) {
      showToast("领取失败：" + e.message, "error");
      if (btn) btn.textContent = "自动领取";
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
    summary.textContent = "正在扫描…";
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
        showToast("没有匹配到链接，试试勾选『匹配所有 one.google.com』", "");
      } else {
        showToast(`提取到 ${data.total} 条链接`, "success");
      }
    } catch (e) {
      summary.textContent = "";
      showToast("扫描失败：" + e.message, "error");
    } finally {
      btnScan.classList.remove("loading");
      btnScan.disabled = false;
    }
  }

  btnRefresh.addEventListener("click", refreshStatus);
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
      showToast(path ? "已保存 adb 路径" : "已清除 adb 路径", "success");
      await refreshStatus();
    } catch (e) {
      showToast("保存失败：" + e.message, "error");
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
