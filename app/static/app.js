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
    return `
      <div class="gpu-name">${esc(x.name)} <span class="muted small">#${x.index}</span></div>
      <div class="meter"><div class="row"><span>Utilization</span><span class="val">${util.toFixed(0)}%</span></div>
        <div class="bar"><div class="fill" style="width:${util}%"></div></div></div>
      <div class="meter"><div class="row"><span>VRAM</span><span class="val">${(x.mem_used_mb / 1024).toFixed(1)} / ${(x.mem_total_mb / 1024).toFixed(1)} GB</span></div>
        <div class="bar"><div class="fill alt" style="width:${memPct}%"></div></div></div>
      <div class="meter"><div class="row"><span>Temperature</span><span class="val">${temp.toFixed(0)} °C</span></div>
        <div class="bar"><div class="fill" style="width:${Math.min(temp, 100)}%;background:${temp >= 80 ? "var(--fail)" : temp >= 70 ? "var(--warn)" : "var(--accent)"}"></div></div></div>`;
  }).join("");
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
  "min_free_gb", "scan_interval_min", "min_age_secs", "encoder", "delete_raw_after_encode"];
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
