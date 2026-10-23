const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0; n = Number(n);
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}
function fmtMbps(v) { return `${(v || 0).toFixed(1)} Mb/s`; }

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  return r.json();
}

/* ---------- live status (1 s) ---------- */
function meter(label, pct, valText, extra = "") {
  const p = Math.max(0, Math.min(100, pct || 0));
  return `<div class="meter"><div class="row"><span>${label}</span><span class="val">${valText}</span></div>
    <div class="bar"><div class="fill ${extra}" style="width:${p}%"></div></div></div>`;
}

function renderGpu(g) {
  const box = $("gpu");
  if (!g || !g.available || !g.gpus.length) {
    box.innerHTML = `<p class="muted">${esc(g?.error || "No GPU detected.")}</p>`;
    return;
  }
  box.innerHTML = g.gpus.map((x) => {
    const memPct = x.mem_total_mb ? (x.mem_used_mb / x.mem_total_mb) * 100 : 0;
    const util = x.util_pct ?? 0;
    const temp = x.temp_c ?? 0;
    const tempColor = temp >= 80 ? "var(--fail)" : temp >= 70 ? "var(--warn)" : "var(--accent)";
    const engines = [];
    if (x.enc_pct != null) engines.push(meter("Encoder engine (NVENC)", x.enc_pct, `${x.enc_pct.toFixed(0)}%`));
    if (x.dec_pct != null) engines.push(meter("Decoder engine (NVDEC)", x.dec_pct, `${x.dec_pct.toFixed(0)}%`));
    return `
      <div class="gpu-name">${esc(x.name)} <span class="muted small">#${x.index}</span></div>
      ${meter("Utilization", util, `${util.toFixed(0)}%`)}
      ${meter("VRAM", memPct, `${(x.mem_used_mb / 1024).toFixed(1)} / ${(x.mem_total_mb / 1024).toFixed(1)} GB`, "alt")}
      <div class="meter"><div class="row"><span>Temperature</span><span class="val">${temp.toFixed(0)} °C</span></div>
        <div class="bar"><div class="fill" style="width:${Math.min(temp, 100)}%;background:${tempColor}"></div></div></div>
      ${engines.join("")}`;
  }).join("");
}

/* ---------- encoding: progress, queue, and hardware markers ---------- */
function fmtClock(sec) {
  if (sec == null || !isFinite(sec)) return "—";
  const t = Math.floor(sec);
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = String(t % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

function renderMarkers(view) {
  if (!view) return `<span class="muted">Nothing has been encoded in this session yet.</span>`;
  return view.markers.map((m) => `
    <div class="marker ${esc(m.status)}">
      <div class="m-label">${esc(m.label)}</div>
      <div class="m-value">${esc(m.value)}</div>
      <div class="m-detail">${esc(m.detail)}</div>
    </div>`).join("");
}

function renderEncoding(s) {
  const r = s.render;
  const last = s.last_render;
  const b = s.batch || { total: 0, done: 0, waiting: 0 };

  // queue: drives encoded in this batch, out of the batch total
  const qPct = b.total ? (b.done / b.total) * 100 : 0;
  $("queue-bar").style.width = `${qPct}%`;
  $("queue-label").textContent = b.total
    ? `Queue: ${b.done} of ${b.total} encoded · ${b.waiting} waiting`
    : "Queue empty";
  $("queue-pct").textContent = b.total ? `${qPct.toFixed(0)}%` : "";

  // current drive
  $("render-state").textContent = r ? "encoding" : "idle";
  $("render-state").className = r ? "pill ok" : "pill";
  if (r) {
    const p = r.pct || 0;
    $("enc-label").textContent = `Encoding ${r.stamp}`;
    $("enc-pct").textContent = `${p.toFixed(1)}%`;
    $("enc-bar").style.width = `${p}%`;
    const speed = r.speed != null ? `${r.speed.toFixed(2)}× realtime` : "measuring…";
    $("enc-detail").textContent = `${fmtClock(r.out_sec)} of ${fmtClock(r.dur)} · ${speed}`;
    $("enc-eta").textContent = r.eta != null ? `ETA ${fmtClock(r.eta)}` : "";
  } else {
    $("enc-label").textContent = b.waiting > 0 ? "Next drive queued…" : "Idle";
    $("enc-pct").textContent = "";
    $("enc-bar").style.width = "0%";
    $("enc-detail").textContent = "—";
    $("enc-eta").textContent = "";
  }

  // hardware and speed markers: live, or the last finished render
  const view = r || last;
  $("markers-title").textContent = r
    ? "Hardware and speed (live)"
    : last ? `Hardware and speed (last render: ${last.stamp})` : "Hardware and speed";
  $("markers").innerHTML = renderMarkers(view);
}

function stateLabel(s) { return `<span class="state ${esc(s)}">${esc(s)}</span>`; }

function renderDrives(list) {
  const body = $("drives");
  if (!list.length) {
    body.innerHTML = `<tr><td colspan="4" class="muted">No drives yet — click Sync now after checks pass.</td></tr>`;
    return;
  }
  body.innerHTML = list.map((d) => {
    const pct = d.state === "done" ? 100 : d.pct;
    const detail = d.state === "failed" ? `<span class="muted">${esc(d.error)}</span>` : esc(d.out || "");
    return `<tr>
      <td>${esc(d.stamp)}</td>
      <td>${stateLabel(d.state)}</td>
      <td class="num"><span class="mini"><i style="width:${pct}%"></i></span>${pct.toFixed(0)}%</td>
      <td class="small">${detail}</td></tr>`;
  }).join("");
}

function renderStatus(s) {
  // transfer + drive bars
  const t = s.transfer || {};
  const drive = s.drives.find((d) => d.route === t.route);
  const dTotal = t.drive_total || 0;
  const dPct = dTotal ? Math.min(100, (t.drive_done / dTotal) * 100) : 0;
  $("drive-bar").style.width = `${dPct}%`;
  $("drive-pct").textContent = `${dPct.toFixed(1)}%  ·  ${fmtBytes(t.drive_done)} / ${fmtBytes(dTotal)}`;
  $("drive-label").textContent = drive
    ? `Downloading ${drive.stamp}`
    : s.encoding_route ? `Encoding ${s.encoding_route}` : (s.cycle_running ? "Working…" : "Idle");
  const fPct = t.file_total ? Math.min(100, (t.file_done / t.file_total) * 100) : 0;
  $("file-bar").style.width = `${fPct}%`;
  $("file-label").textContent = t.file || "—";
  $("file-pct").textContent = t.file_total ? `${fPct.toFixed(0)}%` : "";

  $("speed").textContent = `${((t.speed_bps || 0) / 1e6).toFixed(2)} MB/s`;
  $("net").textContent = `${fmtMbps(s.net?.rx_mbps)} / ${fmtMbps(s.net?.tx_mbps)}`;
  $("encoder").textContent = s.encoder || "—";
  $("queue").textContent = s.encode_queue + (s.encoding_route ? " + 1 encoding" : "");

  renderDrives(s.drives);
  renderEncoding(s);
  renderGpu(s.gpu);

  const logEl = $("log");
  const atBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 20;
  logEl.textContent = s.log.join("\n");
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;

  $("btn-sync").disabled = s.cycle_running;
  $("btn-stop").disabled = !s.cycle_running;
}

async function pollStatus() {
  try { renderStatus(await api("/api/status")); } catch (_) { /* server restarting */ }
}

/* ---------- checks ---------- */
const ICON = { ok: "✓", warn: "!", fail: "✗", skip: "–" };
function renderChecks(data) {
  $("overall").className = `pill ${data.summary}`;
  $("overall").textContent = { ok: "All checks passed", warn: "Ready with warnings", fail: "Setup needs attention" }[data.summary];
  $("checks").innerHTML = data.checks.map((c) => `
    <li><span class="dot ${esc(c.status)}" title="${esc(c.status)}"></span>
      <div><div class="check-title">${esc(c.label)} <span class="muted small">${ICON[c.status] || ""}</span></div>
        <div class="check-detail">${esc(c.detail)}</div>
        ${c.hint && c.status !== "ok" ? `<div class="check-hint">${esc(c.hint)}</div>` : ""}
      </div></li>`).join("");
}
async function loadChecks(force = false) {
  try { renderChecks(await api(`/api/checks${force ? "?force=1" : ""}`)); } catch (_) {}
}

/* ---------- SSH key ---------- */
async function loadKey() {
  const k = await api("/api/key");
  $("pubkey").value = k.public_key || "";
  $("key-status").innerHTML = k.valid
    ? `<span class="pill ok">Key valid</span> ${esc(k.message)}`
    : `<span class="pill fail">Key problem</span> ${esc(k.message)}`;
}

/* ---------- settings ---------- */
const FIELDS = ["comma_host", "scan_subnets", "github_user", "comma_port", "comma_user", "output_dir",
  "min_free_gb", "scan_interval_min", "min_age_secs", "encoder", "decoder",
  "encode_threads", "encode_nice", "resolution_mode", "video_quality", "encode_preset",
  "max_bitrate_mbps", "audio_kbps", "delete_raw_after_encode"];
async function loadSettings() {
  const s = await api("/api/settings");
  const f = $("settings");
  for (const k of FIELDS) {
    const el = f.elements[k];
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!s[k]; else el.value = s[k];
  }
}
$("settings").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {};
  for (const k of FIELDS) {
    const el = e.target.elements[k];
    body[k] = el.type === "checkbox" ? el.checked : el.value;
  }
  await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
  $("settings-msg").textContent = "Saved.";
  loadChecks(true);
});

/* ---------- buttons ---------- */
$("btn-sync").onclick = () => api("/api/sync", { method: "POST" });
$("btn-stop").onclick = () => api("/api/stop", { method: "POST" });
$("btn-recheck").onclick = () => loadChecks(true);
$("btn-copy").onclick = async () => {
  try { await navigator.clipboard.writeText($("pubkey").value); $("key-status").insertAdjacentHTML("beforeend", " · copied"); }
  catch { $("pubkey").select(); }
};
$("btn-gen").onclick = async () => { await api("/api/key/generate", { method: "POST", body: "{}" }); loadKey(); loadChecks(true); };
$("btn-import").onclick = async () => {
  const key = $("import-key").value;
  if (!key.trim()) { $("key-status").textContent = "Paste a private key first."; return; }
  const r = await api("/api/key/import", { method: "POST", body: JSON.stringify({ private_key: key }) });
  if (r.ok === false) { $("key-status").innerHTML = `<span class="pill fail">Import failed</span> ${esc(r.message)}`; return; }
  $("import-key").value = "";
  $("key-status").innerHTML = `<span class="pill ok">Key imported</span> Register this public key on GitHub if it isn't already there.`;
  loadKey(); loadChecks(true);
};
$("btn-regen").onclick = async () => {
  if (!confirm("Replace the app's SSH key? You must re-register the new public key on GitHub.")) return;
  await api("/api/key/generate", { method: "POST", body: JSON.stringify({ overwrite: true }) });
  loadKey(); loadChecks(true);
};
$("btn-find").onclick = async () => {
  $("settings-msg").textContent = "Searching the network for the comma…";
  const r = await api("/api/discover", { method: "POST" });
  $("settings-msg").textContent = r.message;
  loadSettings();
  loadChecks(true);
};
$("btn-test").onclick = async () => {
  $("settings-msg").textContent = "Testing…";
  const r = await api("/api/test-connection", { method: "POST" });
  $("settings-msg").textContent = r.message;
  loadChecks(true);
};

/* ---------- start ---------- */
loadSettings();
loadKey();
loadChecks();
pollStatus();
setInterval(pollStatus, 1000);
setInterval(() => loadChecks(false), 15000);
