/* DouyinDown 前端逻辑：四个页签共用一套轮询状态 */
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);
const state = { secUid: "", nickname: "", works: [], selected: new Set(), status: {}, tab: "up" };

function toast(msg) {
  let t = document.querySelector(".toast");
  if (!t) { t = document.createElement("div"); t.className = "toast"; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2200);
}

async function api(path, opt) {
  const r = await fetch(path, opt ? {
    method: opt.method || "POST",
    headers: { "Content-Type": "application/json" },
    body: opt.body ? JSON.stringify(opt.body) : undefined,
  } : undefined);
  try { return await r.json(); } catch (e) { return {}; }
}

function fmtDur(ms) {
  const s = Math.round((ms || 0) / 1000);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}
function fmtSize(n) {
  if (!n) return "-";
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
  if (n < 1024 ** 3) return (n / 1024 / 1024).toFixed(1) + " MB";
  return (n / 1024 ** 3).toFixed(2) + " GB";
}
function coverUrl(u) { return u ? "/api/cover?url=" + encodeURIComponent(u) : ""; }
function pill(st) {
  const map = { done: "已完成", failed: "失败", pending: "待下载", downloading: "下载中", missing: "缺失" };
  return `<span class="pill ${st}">${map[st] || st}</span>`;
}

/* ---------- 页签 ---------- */
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.remove("active"));
  t.classList.add("active");
  $$(".panel").forEach((p) => p.classList.remove("active"));
  $("#panel-" + t.dataset.tab).classList.add("active");
  state.tab = t.dataset.tab;
  if (t.dataset.tab === "records") loadRecords();
  if (t.dataset.tab === "reclaim") loadReclaims();
  if (t.dataset.tab === "overview") loadLogs();
}));

/* ---------- 状态轮询 ---------- */
async function poll() {
  const s = await api("/api/status");
  state.status = s;
  const logged = s.logged || (s.login && s.login.ok);
  $("#loginPill").innerHTML = `<span class="dot ${logged ? "on" : "off"}"></span>` +
    (logged ? "已登录" + (s.login && s.login.nickname ? "：" + s.login.nickname : "") : "未登录");
  const f = s.fetch || {};
  if (f.running) {
    $("#fetchStatus").textContent = `拉取中：已拿到 ${f.count || 0} 条…`;
    $("#btnFetch").disabled = true;
  } else {
    $("#btnFetch").disabled = false;
    if (f.error) $("#fetchStatus").textContent = "拉取失败：" + f.error;
    else if (f.count) $("#fetchStatus").textContent = `上次拉取完成：${f.nickname || ""} 共 ${f.count} 条，新增 ${f.added || 0} 条`;
    else $("#fetchStatus").textContent = "就绪";
  }
  const dl = s.download || {};
  const counts = s.counts || {};
  $("#stats") && ($("#stats").innerHTML = [
    ["作品台账", counts.total || 0], ["已完成", counts.done || 0], ["待下载", counts.pending || 0],
    ["失败", counts.failed || 0], ["缺失", counts.missing || 0], ["下载队列", (dl.queue || 0)],
    ["并发", (s.settings || {}).concurrency || 3],
    ["本次完成", ((dl.stats || {}).done || 0)],
  ].map(([k, v]) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join(""));
  if (logged !== poll._lastLogged) {
    poll._lastLogged = logged;
    if (logged) loadAuthors();
  }
  if (state.status.fetch && !state.status.fetch.running && state.status.fetch.count && !poll._loadedOnce) {
    poll._loadedOnce = true;
    loadAuthors();
  }
}

/* ---------- 登录 ---------- */
$("#btnLogin").addEventListener("click", async () => {
  const r = await api("/api/login", { body: { timeout: 300 } });
  if (r.started) toast("已打开登录窗口，请在弹出的浏览器里扫码");
  else toast("登录流程已在运行");
  setTimeout(poll, 1500);
});

/* ---------- 拉取 ---------- */
$("#btnFetch").addEventListener("click", async () => {
  const target = $("#target").value.trim();
  if (!target) return toast("先粘贴抖音主页链接");
  const max = parseInt($("#maxItems").value || "0", 10) || 0;
  const r = await api("/api/fetch", { body: { target, max_items: max } });
  if (r.started) { toast("开始拉取，滚动翻页中…"); state.selected.clear(); }
  else toast("已有拉取任务在跑");
  poll();
});

$("#btnDownloadAll").addEventListener("click", async () => {
  if (!state.secUid) return toast("先选一个 UP 主");
  const r = await api("/api/download", { body: { sec_uid: state.secUid } });
  toast(r.queued ? `已入队 ${r.queued} 条` : "没有待下载的作品");
  poll();
});

$("#btnClearUp").addEventListener("click", async () => {
  if (!state.secUid) { $("#target").value = ""; state.works = []; renderWorks(); return toast("已清空输入"); }
  if (!confirm("清空该作者的作品台账（不删除已下载文件）？")) return;
  await api("/api/works/delete", { body: { sec_uid: state.secUid } });
  state.works = []; renderWorks(); loadAuthors(); toast("已清空");
});

$("#btnVerify").addEventListener("click", async () => {
  const r = await api("/api/verify", { body: { sec_uid: state.secUid } });
  toast(`比对完成：检查 ${r.checked} 条，缺失 ${r.missing} 条`);
  if (r.missing) loadReclaims();
});

/* ---------- 作者 ---------- */
async function loadAuthors() {
  const r = await api("/api/authors");
  const items = r.items || [];
  $("#authorCount").textContent = `共 ${items.length} 个`;
  if (!items.length) { $("#authors").innerHTML = '<div class="hint">入库后这里会出现 UP 主卡片</div>'; return; }
  $("#authors").innerHTML = items.map((a) => `
    <div class="author ${a.sec_uid === state.secUid ? "active" : ""}" data-sec="${a.sec_uid}"
         data-nick="${(a.nickname || "").replace(/"/g, "&quot;")}">
      <div class="n"><span>${a.nickname || a.sec_uid.slice(-8)}</span>
        <span class="hint">${a.total_posts || 0} 条</span></div>
      <div class="m">${a.unique_id ? "抖音号 " + a.unique_id : ""} ${a.last_fetch ? "· " + new Date(a.last_fetch * 1000).toLocaleString() : ""}</div>
    </div>`).join("");
  $$(".author").forEach((el) => el.addEventListener("click", () => {
    state.secUid = el.dataset.sec; state.nickname = el.dataset.nick;
    $$(".author").forEach((x) => x.classList.remove("active"));
    el.classList.add("active");
    loadWorks();
  }));
  if (!state.secUid && items.length) {
    state.secUid = items[0].sec_uid; state.nickname = items[0].nickname;
    $$(".author").forEach((x) => { if (x.dataset.sec === state.secUid) x.classList.add("active"); });
    loadWorks();
  }
}

/* ---------- 作品网格 ---------- */
async function loadWorks() {
  if (!state.secUid) return;
  const kw = encodeURIComponent($("#kw").value.trim());
  const st = $("#statusSel").value;
  const r = await api(`/api/works?sec_uid=${state.secUid}&keyword=${kw}&status=${st}&limit=1000`);
  state.works = r.items || [];
  $("#workCount").textContent = `共 ${(r.counts || {}).total || 0} 条，当前显示 ${state.works.length} 条`;
  renderWorks();
}

function renderWorks() {
  const box = $("#works");
  if (!state.works.length) { box.innerHTML = ""; $("#worksEmpty").style.display = "block"; return; }
  $("#worksEmpty").style.display = "none";
  box.innerHTML = state.works.map((w) => `
    <div class="work">
      <div class="thumb">
        <img loading="lazy" src="${coverUrl(w.cover)}" onerror="this.style.display='none'">
        <input type="checkbox" class="chk" data-id="${w.aweme_id}"
               ${state.selected.has(w.aweme_id) ? "checked" : ""}>
        <span class="st">${(state.status.counts ? "" : "")}${w.status === "done" ? "已完成" : w.status === "failed" ? "失败" : "待下载"}</span>
        <span class="badge">${w.is_image ? "图集 " + w.image_count + " 张" : fmtDur(w.duration_ms)}</span>
      </div>
      <div class="body">
        <div class="desc" title="${(w.desc || "").replace(/"/g, "&quot;")}">${w.desc || "（无文案）"}</div>
        <div class="meta">
          <span>${new Date((w.create_time || 0) * 1000).toLocaleDateString()}</span>
          <div class="acts">
            <button class="btn mini" data-dl="${w.aweme_id}">下载</button>
          </div>
        </div>
      </div>
    </div>`).join("");
  $$(".chk").forEach((c) => c.addEventListener("change", () => {
    if (c.checked) state.selected.add(c.dataset.id); else state.selected.delete(c.dataset.id);
  }));
  $$("[data-dl]").forEach((b) => b.addEventListener("click", async () => {
    const r = await api("/api/download", { body: { aweme_ids: [b.dataset.dl] } });
    toast(r.queued ? "已入队下载" : "队列未启动");
    poll();
  }));
}

$("#chkAll").addEventListener("change", (e) => {
  if (e.target.checked) state.works.forEach((w) => state.selected.add(w.aweme_id));
  else state.selected.clear();
  $$(".chk").forEach((c) => (c.checked = e.target.checked));
});

$("#btnQueueSel").addEventListener("click", async () => {
  const ids = [...state.selected];
  if (!ids.length) return toast("先勾选要下载的作品");
  const r = await api("/api/download", { body: { aweme_ids: ids } });
  toast(`已入队 ${r.queued} 条`); poll();
});

$("#kw").addEventListener("keydown", (e) => { if (e.key === "Enter") loadWorks(); });
$("#statusSel").addEventListener("change", loadWorks);

/* ---------- 下载记录 ---------- */
async function loadRecords() {
  const kw = encodeURIComponent($("#kw2").value.trim());
  const st = $("#statusSel2").value;
  const r = await api(`/api/works?keyword=${kw}&status=${st}&limit=800`);
  const items = r.items || [];
  $("#recCount").textContent = `当前 ${items.length} 条 / 台账共 ${(r.counts || {}).total || 0} 条`;
  const body = $("#recBody");
  if (!items.length) { body.innerHTML = '<tr><td colspan="6" class="empty">暂无记录</td></tr>'; return; }
  body.innerHTML = items.map((w) => `
    <tr>
      <td><img class="cover" loading="lazy" src="${coverUrl(w.cover)}" onerror="this.style.visibility='hidden'"></td>
      <td><div class="desc1">${w.desc || "（无文案）"}</div>
          <div class="hint">av <a href="https://www.douyin.com/video/${w.aweme_id}" target="_blank">${w.aweme_id}</a></div></td>
      <td>${w.author_name || "-"}</td>
      <td>${w.is_image ? "图集" : fmtDur(w.duration_ms)}</td>
      <td>${pill(w.status)}</td>
      <td>${w.status === "done" ? fmtSize(w.file_size) : "-"}</td>
    </tr>`).join("");
}
$("#btnRefreshRec").addEventListener("click", loadRecords);
$("#kw2").addEventListener("keydown", (e) => { if (e.key === "Enter") loadRecords(); });
$("#statusSel2").addEventListener("change", loadRecords);

/* ---------- 补采 ------------- */
async function loadReclaims() {
  const r = await api("/api/reclaims");
  const items = r.items || [];
  $("#rec2Count").textContent = `共 ${items.length} 条`;
  const body = $("#rec2Body");
  if (!items.length) { body.innerHTML = '<tr><td colspan="6" class="empty">暂无缺失项，一切正常</td></tr>'; return; }
  body.innerHTML = items.map((x) => `
    <tr>
      <td><img class="cover" loading="lazy" src="${coverUrl(x.cover)}" onerror="this.style.visibility='hidden'"></td>
      <td><div class="desc1">${x.desc || "（无文案）"}</div>
          <div class="hint">av ${x.aweme_id}</div></td>
      <td>${x.author_name || "-"}</td>
      <td class="hint">${x.reason || ""}</td>
      <td>${pill(x.status === "waiting" ? "pending" : x.status)}</td>
      <td><a class="btn mini" href="https://www.douyin.com/video/${x.aweme_id}" target="_blank">打开</a></td>
    </tr>`).join("");
}

$("#btnClearReclaim").addEventListener("click", async () => {
  if (!confirm("清空整个补采清单？")) return;
  await api("/api/reclaims/clear", { body: {} });
  loadReclaims(); toast("补采清单已清空");
});

$("#btnPushReclaim").addEventListener("click", async () => {
  const r = await api("/api/reclaims/push", { body: {} });
  toast(r.queued ? `已重新推送 ${r.queued} 条到下载队列` : "没有待推送的缺失项");
  loadReclaims(); poll();
});

/* ---------- 日志 / 设置 ---------- */
async function loadLogs() {
  const r = await api("/api/logs?n=120");
  $("#log").textContent = (r.lines || []).join("\n");
  $("#log").scrollTop = $("#log").scrollHeight;
  const s = state.status.settings || {};
  if (s.download_root && !$("#dlRoot").value) $("#dlRoot").value = s.download_root;
  if (s.concurrency && !$("#conc").value) $("#conc").value = s.concurrency;
}
$("#btnLogRefresh").addEventListener("click", loadLogs);
$("#btnStopDl").addEventListener("click", async () => {
  await api("/api/download/stop", { body: {} }); toast("已停止下载"); poll();
});
$("#btnSaveSet").addEventListener("click", async () => {
  const r = await api("/api/settings", { body: { download_root: $("#dlRoot").value.trim(), concurrency: $("#conc").value } });
  toast("已保存"); poll();
});

/* ---------- 启动 ---------- */
poll();
setInterval(poll, 2500);
setInterval(() => {
  if (state.status.fetch && state.status.fetch.running && state.tab === "up") { }
  if (state.tab === "overview") loadLogs();
}, 4000);
loadAuthors();
