/* JARVIS-Oberfläche */
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const api = new Proxy({}, { get: (_, k) => async (...a) => {
  const r = await window.pywebview.api[k](...a);
  if (r && typeof r === "object" && !Array.isArray(r) && r.error) { toast(r.error, "Fehler", true); throw new Error(r.error); }
  return r;
}});
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

let S = null;            // Zustand von JARVIS (Config, Status)
let core = null;
let page = "home";
const STATE_TEXT = { idle: "Online", listening: "Zuhören", thinking: "Denken", executing: "Ausführen", speaking: "Spricht", halted: "Not-Aus" };
const CORE_TEXT = { idle: "ONLINE", listening: "ICH HÖRE ZU", thinking: "ANALYSIERE", executing: "FÜHRE AUS", speaking: "SPRICHT", halted: "NOT-AUS" };

window.addEventListener("error", e => { try { window.pywebview.api.js_error(`${e.message} @ ${e.filename}:${e.lineno}`); } catch {} });
window.addEventListener("unhandledrejection", e => { try { window.pywebview.api.js_error(String(e.reason)); } catch {} });

/* ===================================================== Start */
window.addEventListener("pywebviewready", init);

async function init() {
  core = new Core($("#core"));
  const bootOn = await bootAnimation();
  S = await api.ready();
  $("#version").textContent = "v" + S.version;
  applyHalted(S.status.halted);
  applyFocus(S.status.modules.focus);
  restoreChat();
  if (bootOn) await sleep(600);
  $("#boot").classList.add("done");
  if (!S.config.setup_done) wizard();
  else if (!$("#chat").children.length) greet();
  setInterval(refreshCoreStats, 2500); refreshCoreStats();
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function bootAnimation() {
  let on = true;
  try { const st = await window.pywebview.api.state(); on = st.config.app.boot_animation && !st.background; } catch {}
  if (!on) { $("#boot").classList.add("done"); return false; }
  const bc = new Core($("#boot-canvas"), { boot: true });
  const title = "J.A.R.V.I.S.";
  for (let i = 1; i <= title.length; i++) { $("#boot-title").textContent = title.slice(0, i); await sleep(70); }
  const lines = ["Kernsysteme", "Sprachmodul", "Gedächtnis", "PC-Steuerung", "Profile & Routinen", "Smart Home", "Sicherheitssystem"];
  for (const l of lines) {
    const d = document.createElement("div"); d.innerHTML = `${esc(l)} … <span class="ok">bereit</span>`;
    $("#boot-log").appendChild(d); $("#boot-log").scrollTop = 999; await sleep(170);
  }
  bc.setState("speaking"); bc.setLevel(.6);
  return true;
}

function greet() {
  const h = new Date().getHours();
  const g = h < 11 ? "Guten Morgen" : h < 18 ? "Guten Tag" : "Guten Abend";
  addMsg("jarvis", `${g}${S.config.user_name ? ", " + S.config.user_name : ""}. Alle Systeme sind bereit.`);
}

/* ===================================================== Events aus dem Backend */
window.JARVIS = {
  event(name, d) {
    switch (name) {
      case "state": setState(d.state); break;
      case "level": core && core.setLevel(d.level); break;
      case "reply": removeTyping(); addMsg(d.kind || "jarvis", d.text); break;
      case "transcript": addMsg("user", d.text, true); showTyping(); break;
      case "confirm": removeTyping(); addConfirm(d.text); break;
      case "notify": toast(d.text, d.title); break;
      case "halted": applyHalted(d.halted); break;
      case "focus": applyFocus({ mode: d.mode, label: d.label, manual: d.manual }); if (page === "settings") renderSettings(); break;
      case "missed_changed": applyFocus({ missed: d.count }); if (page === "dashboard") renderDashboard(); break;
      case "activity": if (page === "log") renderLog(); if (!d.ok && d.kind === "fehler") {} break;
      case "automations_changed": if (page === "automations") renderAutomations(); break;
      case "devices_changed": if (page === "home-auto") renderRooms(); if (page === "phone") renderPhone(); break;
      case "update_available": toast(`Version ${d.version} ist verfügbar – Einstellungen → Updates.`, "Update"); break;
      case "update_progress": toast(`${d.stage} ${d.percent ? d.percent + " %" : ""}`, "Update"); break;
      case "screenshot": toast("Screenshot gespeichert (Bilder/JARVIS).", "Screenshot"); break;
      case "wake": $("#btn-mic").classList.add("on"); break;
    }
  },
};

let currentState = "idle";
function setState(s) {
  currentState = s;
  const halted = document.body.dataset.halted === "1";
  const shown = halted && s === "idle" ? "halted" : s;
  core && core.setState(shown);
  $("#status-pill").dataset.s = shown;
  $("#status-pill span").textContent = STATE_TEXT[shown] || shown;
  $("#core-state").textContent = CORE_TEXT[shown] || shown.toUpperCase();
  $("#btn-mic").classList.toggle("on", s === "listening");
  $("#btn-stop").classList.toggle("hidden", s !== "speaking");
  if (s === "idle") removeTyping();
}
/* Modus-Anzeige in der Titelleiste: Modus + Zahl verpasster Meldungen */
let focusState = { mode: "normal", label: "Normal", missed: 0 };
const MODE_ICON = { gaming: "🎮", film: "🎬", schlafen: "🌙", arbeit: "💼" };
function applyFocus(f) {
  if (!f) return;
  focusState = { ...focusState, ...f };
  const chip = $("#mode-chip"), on = focusState.mode !== "normal";
  chip.classList.toggle("hidden", !on && !focusState.missed);
  chip.querySelector("span").textContent = on ? `${MODE_ICON[focusState.mode] || ""} ${focusState.label}${focusState.manual === false ? " (auto)" : ""}` : "Verpasst";
  chip.title = on ? "Klicken: Modus beenden" : "Klicken: verpasste Meldungen anhören";
  const b = chip.querySelector("b"); b.textContent = focusState.missed; b.classList.toggle("hidden", !focusState.missed);
}
$("#mode-chip").onclick = () => send(focusState.mode !== "normal" ? "Modus beenden" : "Was habe ich verpasst?");

function applyHalted(h) {
  document.body.dataset.halted = h ? "1" : "0";
  $("#halt-banner").classList.toggle("hidden", !h);
  setState(currentState);
}

/* ===================================================== Chat */
let chatLog = [];
function addMsg(kind, text, voice = false, save = true) {
  const d = document.createElement("div");
  d.className = `msg ${kind}` + (voice ? " voice" : "");
  d.textContent = text;
  $("#chat").appendChild(d);
  $("#chat").scrollTop = 1e9;
  if (save) { chatLog.push({ kind, text, voice }); chatLog = chatLog.slice(-80); store.set("chat", chatLog); }
  return d;
}
function restoreChat() { chatLog = store.get("chat", []); for (const m of chatLog) addMsg(m.kind, m.text, m.voice, false); }
function showTyping() { removeTyping(); const d = document.createElement("div"); d.className = "msg jarvis typing"; d.id = "typing"; d.innerHTML = "<span></span><span></span><span></span>"; $("#chat").appendChild(d); $("#chat").scrollTop = 1e9; }
function removeTyping() { $("#typing")?.remove(); }
function addConfirm(text) {
  const d = addMsg("jarvis", text);
  const a = document.createElement("div"); a.className = "actions";
  a.innerHTML = `<button class="small">Ja, ausführen</button><button class="small ghost">Abbrechen</button>`;
  a.children[0].onclick = () => { send("ja"); a.remove(); };
  a.children[1].onclick = () => { send("nein"); a.remove(); };
  d.appendChild(a);
}
function send(text) {
  text = text.trim(); if (!text) return;
  addMsg("user", text); showTyping(); api.send(text);
}
$("#composer").addEventListener("submit", e => { e.preventDefault(); send($("#input").value); $("#input").value = ""; });
$("#btn-mic").onclick = () => api.toggle_mic();
$("#btn-stop").onclick = () => api.stop_speaking();
$("#btn-halt").onclick = () => api.emergency_stop();
$("#btn-resume").onclick = () => api.resume();
$("#btn-clear-chat").onclick = () => { chatLog = []; store.set("chat", []); $("#chat").innerHTML = ""; };
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && currentState === "speaking") api.stop_speaking();
  if (e.ctrlKey && e.key === " ") { e.preventDefault(); api.toggle_mic(); }
  if (e.key === "F11") { e.preventDefault(); api.win_fullscreen(); }
});

/* Fenster */
$$("[data-win]").forEach(b => b.onclick = () => ({
  fullscreen: api.win_fullscreen, minimize: api.win_minimize, maximize: api.win_toggle_max, close: api.win_close,
}[b.dataset.win])());
$("#titlebar").addEventListener("dblclick", e => { if (!e.target.closest("button")) api.win_toggle_max(); });

/* Navigation */
$$("#nav button").forEach(b => b.onclick = () => go(b.dataset.page));
function go(p) {
  page = p;
  $$("#nav button").forEach(b => b.classList.toggle("active", b.dataset.page === p));
  $$(".page").forEach(s => s.classList.toggle("active", s.id === "page-" + p));
  ({ dashboard: renderDashboard, memory: renderMemory, automations: renderAutomations, "home-auto": renderRooms,
     phone: renderPhone, log: renderLog, diagnose: renderBackups, settings: renderSettings }[p] || (() => {}))();
  if (p === "home") setTimeout(() => core.resize(), 20);
}

/* ===================================================== Hilfen */
function toast(text, title = "JARVIS", err = false) {
  const t = document.createElement("div"); t.className = "toast" + (err ? " err" : "");
  t.innerHTML = `<b>${esc(title)}</b>${esc(text)}`;
  $("#toasts").appendChild(t); setTimeout(() => t.remove(), 5500);
}
function modal(title, body, onOk, okText = "Speichern") {
  $("#modal-box").innerHTML = `<h2>${esc(title)}</h2><div class="form">${body}</div>
    <div class="foot"><button class="ghost" data-x>Abbrechen</button><button data-ok>${esc(okText)}</button></div>`;
  $("#modal").classList.remove("hidden");
  const close = () => $("#modal").classList.add("hidden");
  $("#modal-box [data-x]").onclick = close;
  $("#modal-box [data-ok]").onclick = async () => { try { if (await onOk($("#modal-box")) !== false) close(); } catch (e) { console.error(e); } };
  setTimeout(() => $("#modal-box input, #modal-box textarea")?.focus(), 50);
}
function confirmBox(text, onYes) { modal("Bestätigen", `<p>${esc(text)}</p>`, onYes, "Ja"); }
const fmtBytes = b => b > 2 ** 30 ? (b / 2 ** 30).toFixed(1) + " GB" : (b / 2 ** 20).toFixed(0) + " MB";
const fmtTime = ts => new Date(ts * 1000).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
function fmtUptime(s) { const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return (d ? d + " T " : "") + h + " h " + m + " min"; }
function gauge(val, label, sub = "") {
  const v = Math.max(0, Math.min(100, val || 0)), r = 34, c = 2 * Math.PI * r;
  const col = v > 85 ? "var(--danger)" : v > 65 ? "var(--warn)" : "var(--accent)";
  return `<div class="gauge"><svg viewBox="0 0 86 86"><circle cx="43" cy="43" r="${r}" stroke="rgba(255,255,255,.07)" stroke-width="7"/>
    <circle cx="43" cy="43" r="${r}" stroke="${col}" stroke-width="7" stroke-dasharray="${c * v / 100} ${c}" transform="rotate(-90 43 43)" style="filter:drop-shadow(0 0 4px ${col})"/></svg>
    <div><div class="val">${Math.round(v)}%</div><div class="lbl">${esc(label)}</div><div class="meta">${esc(sub)}</div></div></div>`;
}
const sw = (key, val) => `<label class="switch"><input type="checkbox" data-key="${key}" ${val ? "checked" : ""}><span></span></label>`;

/* ===================================================== Zentrale: Kurzstatus */
async function refreshCoreStats() {
  if (page !== "home" || document.hidden) return;
  try {
    const d = await window.pywebview.api.dashboard();
    if (d.error) return;
    const s = d.system, now = new Date();
    $("#core-stats").innerHTML = `
      <div class="col"><div>CPU <b>${s.cpu.toFixed(0)}%</b></div><div>RAM <b>${s.ram.toFixed(0)}%</b></div>${s.gpu ? `<div>GPU <b>${s.gpu.load.toFixed(0)}%</b> · ${s.gpu.temp}°C</div>` : ""}</div>
      <div class="col r"><div><b>${now.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })}</b></div><div>${now.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" })}</div>
      <div>KI <b>${d.status.modules.ai?.available ? "bereit" : "lokal"}</b></div></div>`;
    const v = d.status.modules.voice || {};
    $("#core-hint").textContent = v.error ? "Mikrofon-Problem: " + v.error
      : v.wake ? "Sag „Hey Jarvis“ oder klicke auf das Mikrofon (Strg+Leertaste)" : "Klicke auf das Mikrofon oder schreibe unten (Strg+Leertaste)";
  } catch {}
}

/* ===================================================== KI-Kosten */
const eurFmt = new Intl.NumberFormat("de-DE", { style: "currency", currency: "EUR" });
const fmtEur = v => v > 0 && v < 0.01 ? "< 1 Cent" : eurFmt.format(v || 0);
const fmtUnits = (n, unit) => unit === "Sekunden" ? (n >= 60 ? Math.round(n / 60) + " Min." : Math.round(n) + " Sek.")
  : Math.round(n).toLocaleString("de-DE") + " " + unit;
function costsCard(c) {
  if (!c) return "";
  // Balken der letzten 30 Tage (fehlende Tage = 0)
  const byDay = Object.fromEntries((c.days || []).map(d => [d.day, d.eur]));
  const days = [...Array(30)].map((_, i) => { const t = new Date(Date.now() - (29 - i) * 864e5); const k = t.toLocaleDateString("sv-SE"); return { k, t, v: byDay[k] || 0 }; });
  const max = Math.max(...days.map(d => d.v), 0.0001);
  const bars = days.map(d => `<i style="height:${Math.max(2, d.v / max * 100)}%" title="${d.t.toLocaleDateString("de-DE")}: ${fmtEur(d.v)}"></i>`).join("");
  const rows = (c.by_kind || []).map(k => `<span>${esc(k.service)} · ${esc(k.kind)}</span><span>${k.eur ? fmtEur(k.eur) : "gratis"} <small class="meta">${k.n}× · ${fmtUnits(k.units, k.unit)}</small></span>`).join("");
  const el = c.elevenlabs_free ? Math.min(100, c.elevenlabs_chars / c.elevenlabs_free * 100) : 0;
  return `<div class="card span2"><div class="card-head"><h3>KI-Kosten</h3><span class="meta">Richtwerte · genaue Abrechnung bei OpenAI</span></div>
    <div class="cost-stats"><div><b>${fmtEur(c.today)}</b><span>Heute</span></div><div><b>${fmtEur(c.month)}</b><span>Dieser Monat</span></div>
      <div><b>${fmtEur(c.prev_month)}</b><span>Letzter Monat</span></div><div><b>${fmtEur(c.all)}</b><span>Insgesamt</span></div></div>
    <div class="cost-bars" title="Letzte 30 Tage">${bars}</div>
    ${rows ? `<div class="kv" style="margin-top:12px">${rows}</div>` : `<p class="hint">Diesen Monat noch keine kostenpflichtige KI-Nutzung.</p>`}
    ${c.elevenlabs_chars ? `<div style="margin-top:12px"><div class="kv"><span>ElevenLabs-Freikontingent</span><span>${c.elevenlabs_chars.toLocaleString("de-DE")} / ${c.elevenlabs_free.toLocaleString("de-DE")} Zeichen</span></div>
      <div class="bar"><i style="width:${el}%;${el > 85 ? "background:var(--warn)" : ""}"></i></div></div>` : ""}
    <p class="hint" style="margin-bottom:0">Frag einfach: „Jarvis, wie viel habe ich für KI ausgegeben?“</p></div>`;
}

/* ===================================================== Dashboard */
async function renderDashboard() {
  const d = await api.dashboard(); const s = d.system, m = d.status.modules;
  const disks = s.disks.map(x => `<div style="margin-bottom:8px"><div class="kv"><span>${esc(x.mount)}</span><span>${fmtBytes(x.used)} / ${fmtBytes(x.total)}</span></div><div class="bar"><i style="width:${x.percent}%"></i></div></div>`).join("");
  const dot = ok => `<span class="dot ${ok ? "ok" : "bad"}"></span>`;
  $("#dash").innerHTML = `
    <div class="card">${gauge(s.cpu, "Prozessor", s.cpu_name)}</div>
    <div class="card">${gauge(s.ram, "Arbeitsspeicher", fmtBytes(s.ram_used) + " / " + fmtBytes(s.ram_total))}</div>
    ${s.gpu ? `<div class="card">${gauge(s.gpu.load, "Grafikkarte", s.gpu.name + " · " + s.gpu.temp + " °C")}</div>` : ""}
    ${s.battery ? `<div class="card">${gauge(s.battery.percent, "Akku", s.battery.plugged ? "lädt" : "Akkubetrieb")}</div>` : ""}
    <div class="card"><div class="card-head"><h3>System</h3></div><div class="kv">
      <span>Rechner</span><span>${esc(s.host)}</span><span>System</span><span>${esc(s.os)}</span>
      <span>Kerne</span><span>${s.cores}</span><span>Laufzeit</span><span>${fmtUptime(s.uptime)}</span><span>Prozesse</span><span>${s.procs}</span></div></div>
    <div class="card"><div class="card-head"><h3>Laufwerke</h3></div>${disks}</div>
    <div class="card"><div class="card-head"><h3>JARVIS</h3></div><div class="kv">
      <span>KI</span><span>${dot(m.ai?.available)}${m.ai?.available ? esc(m.ai.model) : m.ai?.has_key ? "deaktiviert" : "kein API-Schlüssel"}</span>
      <span>KI-Anfragen</span><span>${m.ai?.requests ?? 0} (diese Sitzung)</span>
      <span>Stimme</span><span>${dot(!m.voice?.error)}${esc(m.voice?.tts)} · Erkennung ${esc(m.voice?.stt)}</span>
      <span>Wake-Word</span><span>${dot(m.voice?.wake)}${m.voice?.wake ? "aktiv" : "aus"}</span>
      <span>Programme</span><span>${m.pc?.apps ?? 0} erkannt</span>
      <span>Version</span><span>${esc(d.status.version)}</span></div></div>
    <div class="card"><div class="card-head"><h3>Gedächtnis &amp; Automationen</h3></div><div class="kv">
      <span>Gedächtnis</span><span>${d.memory_count} Einträge</span>
      <span>Profile</span><span>${m.automation?.profiles ?? 0}</span><span>Routinen</span><span>${m.automation?.routines ?? 0}</span>
      <span>Eigene Befehle</span><span>${m.automation?.commands ?? 0}</span></div></div>
    <div class="card"><div class="card-head"><h3>Geräte</h3></div><div class="kv">
      <span>Zimmer</span><span>${d.rooms}</span><span>Smart-Home</span><span>${d.devices} Geräte (${m.smarthome?.on ?? 0} an)</span>
      <span>Handy</span><span>${(m.phone?.devices || []).map(x => esc(x.model)).join(", ") || "nicht verbunden"}</span>
      <span>Fernzugriff</span><span>${m.remote?.enabled ? "aktiv (Port " + m.remote.port + ")" : "aus"}</span></div></div>
    ${d.missed && d.missed.length ? `<div class="card span2"><div class="card-head"><h3>Verpasste Meldungen</h3><button class="small ghost" id="missed-clear">Gelesen</button></div>
      <div class="log">${d.missed.map(x => `<div class="e"><span class="t">${new Date(x.ts * 1000).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</span>
        <span class="kind">${esc(["Info", "Wichtig", "Dringend", "Kritisch"][x.priority] || "")}</span><span>${esc(x.title ? x.title + ": " : "")}${esc(x.text)}</span></div>`).join("")}</div></div>` : ""}
    ${costsCard(d.costs)}
    <div class="card span2"><div class="card-head"><h3>Letzte Aktivitäten</h3></div><div class="log">${d.activity.map(logRow).join("")}</div></div>`;
  $("#missed-clear") && ($("#missed-clear").onclick = async () => { await api.missed_clear(); renderDashboard(); });
  if (page === "dashboard") setTimeout(() => page === "dashboard" && renderDashboard(), 4000);
}

/* ===================================================== Gedächtnis */
async function renderMemory() {
  const rows = await api.memory_list(); const q = ($("#mem-search").value || "").toLowerCase();
  const f = rows.filter(r => !q || (r.label + r.value).toLowerCase().includes(q));
  $("#mem-list").innerHTML = f.length ? f.map(r => `<div class="row" data-k="${esc(r.key)}">
      <span class="k">${esc(r.label)}</span><span class="v">${esc(r.value)}</span><span class="meta">${fmtTime(r.updated)}</span>
      <button class="small ghost" data-edit>Ändern</button><button class="small ghost danger" data-del>Vergessen</button></div>`).join("")
    : `<div class="empty">Noch nichts gespeichert. Sag zum Beispiel: „Mein Lieblingsspiel ist GTA“.</div>`;
  $$("#mem-list .row").forEach(el => {
    const r = rows.find(x => x.key === el.dataset.k);
    el.querySelector("[data-edit]").onclick = () => memEdit(r);
    el.querySelector("[data-del]").onclick = () => confirmBox(`„${r.label}“ vergessen?`, async () => { await api.memory_delete(r.key); renderMemory(); });
  });
}
function memEdit(r) {
  modal(r ? "Eintrag ändern" : "Neuer Eintrag", `<label>Was<input id="m-l" value="${esc(r?.label || "")}" placeholder="z. B. Lieblingsspiel"></label>
    <label>Wert<input id="m-v" value="${esc(r?.value || "")}" placeholder="z. B. GTA V"></label>`,
    async b => { const l = $("#m-l", b).value.trim(), v = $("#m-v", b).value.trim(); if (!l || !v) return false; await api.memory_set(l, v, r?.key); renderMemory(); });
}
$("#mem-add").onclick = () => memEdit(null);
$("#mem-search").oninput = renderMemory;

/* ===================================================== Automationen */
let autoTab = "all";
$$("#auto-tabs button").forEach(b => b.onclick = () => { autoTab = b.dataset.t; $$("#auto-tabs button").forEach(x => x.classList.toggle("active", x === b)); renderAutomations(); });
const TYPE_DE = { profile: "Profil", routine: "Routine", command: "Eigener Befehl" };
const DAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
async function renderAutomations() {
  const all = await api.automations(); const list = all.filter(a => autoTab === "all" || a.type === autoTab);
  $("#auto-list").innerHTML = list.length ? list.map(a => `<div class="card auto-card" data-id="${a.id}">
      <div class="card-head"><h3>${esc(a.name)}</h3><div><span class="tag ${a.type}">${TYPE_DE[a.type]}</span>${a.enabled ? "" : '<span class="tag off">deaktiviert</span>'}</div></div>
      <div class="trig">Auslöser: ${a.triggers.map(t => "„" + esc(t) + "“").join(", ")}</div>
      ${a.schedule ? `<div class="trig">Zeitplan: ${a.schedule.days.length === 7 ? "täglich" : a.schedule.days.map(d => DAYS[d]).join(", ")} um ${esc(a.schedule.time)}</div>` : ""}
      ${a.event ? `<div class="trig">Ereignis: ${esc(a.event.type)} ${esc(a.event.value || "")}</div>` : ""}
      <ol class="steps">${a.steps.map(s => "wait" in s ? `<li class="wait">${Math.round(s.wait)} Sekunden warten</li>`
        : `<li class="${s.calls && s.calls.length ? "" : "ai"}">${s.delay ? `<span class="meta">+${Math.round(s.delay)} s</span> ` : ""}${esc(s.text)}</li>`).join("")}</ol>
      <div class="foot"><button class="small" data-run>▶ Ausführen</button><button class="small ghost" data-edit>Bearbeiten</button>
      <button class="small ghost danger" data-del>Löschen</button></div></div>`).join("")
    : `<div class="empty">Noch nichts angelegt. Sag: „Wenn ich FiveM sage, starte Discord und FiveM“.</div>`;
  $$("#auto-list .auto-card").forEach(el => {
    const a = all.find(x => x.id === el.dataset.id);
    el.querySelector("[data-run]").onclick = () => { api.automation_run(a.id); toast(`„${a.name}“ wird ausgeführt.`); };
    el.querySelector("[data-edit]").onclick = () => autoEdit(a);
    el.querySelector("[data-del]").onclick = () => confirmBox(`„${a.name}“ löschen?`, async () => { await api.automation_delete(a.id); renderAutomations(); });
  });
}
function autoEdit(a) {
  const steps = a ? a.steps.map(s => "wait" in s ? `warte ${Math.round(s.wait)} Sekunden` : (s.delay ? `warte ${Math.round(s.delay)} Sekunden\n` : "") + s.text).join("\n") : "";
  const days = a?.schedule?.days ?? [0, 1, 2, 3, 4, 5, 6];
  modal(a ? "Bearbeiten" : "Neu anlegen", `
    <label>Art<select id="a-type">${Object.entries(TYPE_DE).map(([k, v]) => `<option value="${k}" ${a?.type === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
    <label>Name<input id="a-name" value="${esc(a?.name || "")}" placeholder="z. B. FiveM"></label>
    <label>Auslösesätze (mit Komma getrennt)<input id="a-trig" value="${esc((a?.triggers || []).join(", "))}" placeholder="FiveM, Starte FiveM"></label>
    <label>Schritte – ein Befehl pro Zeile, so wie du ihn sagen würdest<textarea id="a-steps" rows="6" placeholder="starte Discord&#10;warte 5 Sekunden&#10;starte FiveM">${esc(steps)}</textarea></label>
    <label>Zeitplan (optional)<input id="a-time" type="time" value="${esc(a?.schedule?.time || "")}"></label>
    <div style="display:flex;gap:6px;flex-wrap:wrap">${DAYS.map((d, i) => `<label class="check"><input type="checkbox" data-day="${i}" ${days.includes(i) ? "checked" : ""}>${d}</label>`).join("")}</div>
    <label class="check"><input type="checkbox" id="a-en" ${a?.enabled !== false ? "checked" : ""}> aktiv</label>`,
    async b => {
      const name = $("#a-name", b).value.trim(); if (!name) return false;
      const t = $("#a-time", b).value;
      await api.automation_save({ ...(a || {}), type: $("#a-type", b).value, name, triggers: [name, ...$("#a-trig", b).value.split(",")],
        steps_text: $("#a-steps", b).value, enabled: $("#a-en", b).checked,
        schedule: t ? { time: t, days: $$("[data-day]", b).filter(x => x.checked).map(x => +x.dataset.day) } : null });
      renderAutomations();
    });
}
$("#auto-add").onclick = () => autoEdit(null);

/* ===================================================== Smart Home */
const DEV_TYPES = { light: "Licht", plug: "Steckdose", tv: "Fernseher", speaker: "Lautsprecher", fan: "Ventilator", climate: "Heizung", cover: "Rollladen", other: "Sonstiges" };
async function renderRooms() {
  const d = await api.smarthome();
  const byRoom = {}; d.rooms.forEach(r => byRoom[r] = []);
  d.devices.forEach(x => (byRoom[x.room || "Ohne Zimmer"] ||= []).push(x));
  const keys = Object.keys(byRoom);
  $("#rooms").innerHTML = keys.length ? keys.map(r => `<div class="card"><div class="card-head"><h3>${esc(r)}</h3>
      <button class="small ghost danger" data-rdel="${esc(r)}">Entfernen</button></div>
      ${byRoom[r].map(x => `<div class="device"><div class="bulb ${x.state === "on" ? "on" : ""}" data-tog="${x.id}" title="Umschalten">
        <svg viewBox="0 0 24 24"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5c.7.7 1 1.5 1 2.5h6c0-1 .3-1.8 1-2.5A6 6 0 0 0 12 3z"/></svg></div>
        <div class="name">${esc(x.name)}<div class="type">${DEV_TYPES[x.type] || x.type} · ${x.provider === "homeassistant" ? "Home Assistant" : "virtuell"}</div></div>
        <button class="small ghost danger" data-ddel="${x.id}">×</button></div>`).join("") || '<div class="meta">Keine Geräte</div>'}</div>`).join("")
    : `<div class="empty">Noch keine Zimmer. Sag: „Füge im Wohnzimmer eine Lampe namens Deckenlicht hinzu“.</div>`;
  $$("[data-tog]").forEach(b => b.onclick = async () => { await api.device_toggle(b.dataset.tog); renderRooms(); });
  $$("[data-ddel]").forEach(b => b.onclick = async () => { await api.device_remove(b.dataset.ddel); renderRooms(); });
  $$("[data-rdel]").forEach(b => b.onclick = () => confirmBox(`Zimmer „${b.dataset.rdel}“ entfernen?`, async () => { await api.room_remove(b.dataset.rdel); renderRooms(); }));
  $("#ha-btn").textContent = d.ha_url ? "Home Assistant" : "Home Assistant verbinden";
}
$("#room-add").onclick = () => modal("Neues Zimmer", `<label>Name<input id="r-n" placeholder="z. B. Wohnzimmer"></label>`,
  async b => { const n = $("#r-n", b).value.trim(); if (!n) return false; await api.room_add(n); renderRooms(); });
$("#dev-add").onclick = async () => {
  const d = await api.smarthome();
  modal("Neues Gerät", `<label>Name<input id="d-n" placeholder="z. B. Deckenlicht"></label>
    <label>Zimmer<input id="d-r" list="d-rl" placeholder="z. B. Wohnzimmer"><datalist id="d-rl">${d.rooms.map(r => `<option value="${esc(r)}">`).join("")}</datalist></label>
    <label>Art<select id="d-t">${Object.entries(DEV_TYPES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></label>
    <label>Home-Assistant-Entity (optional)<input id="d-a" placeholder="z. B. light.wohnzimmer_decke"></label>`,
    async b => { const n = $("#d-n", b).value.trim(), r = $("#d-r", b).value.trim(); if (!n || !r) return false;
      toast(await api.device_add(n, r, $("#d-t", b).value, $("#d-a", b).value.trim())); renderRooms(); });
};
$("#ha-btn").onclick = async () => {
  const d = await api.smarthome();
  modal("Home Assistant", `<p class="hint">Home Assistant verbindet fast alle Smart-Home-Marken (Hue, Shelly, Tuya, Sonos, Fernseher …).
    Token: In Home Assistant → Profil → Sicherheit → „Langlebiges Zugriffstoken“ erstellen.</p>
    <label>Adresse<input id="h-u" value="${esc(d.ha_url || "")}" placeholder="http://homeassistant.local:8123"></label>
    <label>Zugriffstoken<input id="h-t" type="password" placeholder="${d.ha_token ? "gespeichert – leer lassen, um beizubehalten" : "Token einfügen"}"></label>
    <div id="h-s" class="status-msg"></div>`,
    async b => {
      const r = await api.ha_setup($("#h-u", b).value, $("#h-t", b).value || "••••");
      $("#h-s", b).textContent = r.msg; $("#h-s", b).className = "status-msg " + (r.ok ? "ok" : "bad");
      if (r.ok) { toast(await api.ha_import(), "Home Assistant"); renderRooms(); return true; }
      return false;
    }, "Verbinden & importieren");
};

/* ===================================================== Handy */
async function renderPhone() {
  const d = await api.phone();
  const devs = d.devices.map(x => `<div class="row"><span class="dot ${x.state === "device" || x.state === "connected" ? "ok" : "warn"}"></span>
    <span class="k">${esc(x.model)}</span><span class="v">${x.type === "android" ? "Android" : "iPhone/iPad"} · ${esc(x.state === "device" ? "verbunden" : x.state === "unauthorized" ? "USB-Debugging am Handy erlauben" : x.state)}</span></div>`).join("");
  $("#phone").innerHTML = `<div class="two-col">
    <div class="card"><div class="card-head"><h3>Verbundene Geräte</h3><button class="small ghost" id="ph-refresh">Aktualisieren</button></div>
      ${devs || '<div class="empty">Kein Handy erkannt. Android per USB (USB-Debugging an) oder WLAN-Debugging verbinden; iPhones werden per USB erkannt.</div>'}
      <p class="hint">Per Sprache: „Wie ist der Akku vom Handy?“ · „Öffne YouTube auf dem Handy“ · „Handy-Screenshot“</p></div>
    <div class="card"><div class="card-head"><h3>Android (ADB)</h3></div>
      ${d.adb ? '<p><span class="dot ok"></span>ADB ist eingerichtet.</p>' : `<p class="hint">Für Android-Steuerung werden die offiziellen Android-Platform-Tools von Google benötigt (ca. 15 MB).</p><button id="ph-adb">ADB einrichten</button>`}
      <div class="field col" style="margin-top:12px"><div class="lbl">WLAN-Debugging verbinden<small>Am Handy: Entwickleroptionen → Kabelloses Debugging → IP-Adresse &amp; Port</small></div>
        <div class="ctl"><input id="ph-ip" placeholder="192.168.0.23:5555"><button id="ph-con">Verbinden</button></div></div></div>
    <div class="card span2"><div class="card-head"><h3>JARVIS-Handy-App (wie Alexa – im Heimnetz)</h3>${sw("remote", d.remote.enabled)}</div>
      <div style="display:flex;gap:22px;align-items:flex-start;flex-wrap:wrap">
        ${d.remote.qr ? `<img src="${d.remote.qr}" alt="QR-Code" style="width:190px;height:190px;border-radius:10px;background:#fff;padding:6px">` : ""}
        <div style="flex:1;min-width:260px">
          ${d.remote.running ? `<p><span class="dot ok"></span>Aktiv – ${d.remote.clients} Handy(s) verbunden</p>
            <ol class="hint" style="padding-left:18px;margin:8px 0">
              <li>Handy im selben WLAN, QR-Code mit der Kamera scannen.</li>
              <li>Die Zertifikatswarnung einmalig bestätigen („Erweitert → Weiter“) – das Zertifikat hat JARVIS selbst erstellt.</li>
              <li>Im Browser-Menü „Zum Startbildschirm hinzufügen“ – fertig ist die JARVIS-App.</li>
              <li>Fragt Windows beim Aktivieren nach der Firewall, „Private Netzwerke“ erlauben.</li></ol>
            <div class="kv"><span>Adresse</span><span style="user-select:text;font-family:var(--mono)">${esc(d.remote.url)}</span></div>`
          : `<p class="hint">Aktiviere die Handy-App, um JARVIS vom Handy aus per Sprache oder Text zu steuern, Geräte zu schalten und Routinen zu starten.
             Zugriff nur mit geheimem Token (per QR-Code), verschlüsselt über HTTPS.</p>`}
          <div class="field"><div class="lbl">Handy-Antworten auch am PC vorlesen</div><div class="ctl">${sw("remote.speak_on_pc", d.remote.speak_on_pc)}</div></div>
          <button class="ghost danger small" id="ph-newtok">Neuen Token erzeugen (alle Handys abmelden)</button>
        </div></div></div></div>`;
  $("#ph-refresh").onclick = renderPhone;
  $("#ph-adb") && ($("#ph-adb").onclick = async e => { e.target.disabled = true; e.target.textContent = "Wird geladen …"; await api.phone_install_adb(); toast("ADB eingerichtet."); renderPhone(); });
  $("#ph-con").onclick = async () => { toast(await api.phone_connect($("#ph-ip").value.trim()), "Handy"); setTimeout(renderPhone, 1500); };
  $("#phone [data-key=remote]").onchange = async e => { try { await api.remote_set(e.target.checked); } catch {} renderPhone(); };
  $("#phone [data-key='remote.speak_on_pc']").onchange = e => api.settings_set({ "remote.speak_on_pc": e.target.checked });
  $("#ph-newtok").onclick = () => confirmBox("Neuen Token erzeugen? Alle gekoppelten Handys müssen den QR-Code neu scannen.",
    async () => { await api.remote_new_token(); renderPhone(); toast("Neuer Token erstellt – QR-Code neu scannen."); });
}

/* ===================================================== Protokoll */
function logRow(e) { return `<div class="e ${e.ok ? "" : "err"}"><span class="t">${fmtTime(e.ts)}</span><span class="kind">${esc(e.kind)}</span><span class="x">${esc(e.text)}</span></div>`; }
async function renderLog() {
  const rows = await api.activity(300, $("#log-errors").checked);
  $("#log-list").innerHTML = rows.length ? rows.map(logRow).join("") : '<div class="empty">Noch keine Einträge.</div>';
}
$("#log-errors").onchange = renderLog;
$("#log-open").onclick = () => api.open_logs();
$("#log-clear").onclick = () => confirmBox("Aktivitätsprotokoll leeren?", async () => { await api.activity_clear(); renderLog(); });

/* ===================================================== Diagnose & Backup */
$("#diag-run").onclick = async e => {
  e.target.disabled = true; e.target.textContent = "Prüfe …";
  $("#diag-list").innerHTML = '<div class="empty">Selbstdiagnose läuft …</div>';
  try {
    const r = await api.diagnose();
    $("#diag-list").innerHTML = r.map(x => `<div class="row"><span class="dot ${x.ok ? "ok" : "bad"}"></span><span class="k">${esc(x.name)}</span><span class="v">${esc(x.msg)}</span><span class="meta">${esc(x.module)}</span></div>`).join("");
  } finally { e.target.disabled = false; e.target.textContent = "Prüfung starten"; }
};
async function renderBackups() {
  const b = await api.backups();
  $("#backup-list").innerHTML = b.length ? b.map(x => `<div class="row"><span class="v">${esc(x.file.replace("JARVIS-Backup_", "").replace(".zip", ""))}</span>
    <span class="meta">${fmtBytes(x.size)}</span><button class="small ghost" data-restore="${esc(x.file)}">Wiederherstellen</button></div>`).join("")
    : '<div class="empty">Noch keine Backups.</div>';
  $$("[data-restore]").forEach(el => el.onclick = () => confirmBox("Dieses Backup wiederherstellen? Der aktuelle Stand wird vorher gesichert.",
    async () => { await api.backup_restore(el.dataset.restore); toast("Backup wiederhergestellt."); renderBackups(); }));
}
$("#backup-now").onclick = async () => { toast("Backup erstellt: " + await api.backup_create()); renderBackups(); };

/* ===================================================== Einstellungen */
async function renderSettings() {
  S = await api.state();
  const c = S.config, v = c.voice;
  const [voices, devs] = await Promise.all([api.voices(), api.audio_devices()]);
  const opt = (list, cur) => list.map(([val, label]) => `<option value="${esc(val)}" ${val === cur ? "selected" : ""}>${esc(label)}</option>`).join("");
  const voiceSel = v.tts_engine === "openai" ? opt(voices.openai.map(x => [x, x]), v.openai_voice)
    : v.tts_engine === "edge" ? opt(voices.edge, v.edge_voice)
    : v.tts_engine === "elevenlabs" ? opt(voices.elevenlabs, v.elevenlabs_voice) : "";
  const voiceKey = { openai: "voice.openai_voice", edge: "voice.edge_voice", elevenlabs: "voice.elevenlabs_voice" }[v.tts_engine];
  const devOpt = (list, cur) => `<option value="">Windows-Standard</option>` + opt(list.map(x => [x, x]), cur);
  $("#settings").innerHTML = `
  <div class="card"><div class="card-head"><h3>KI</h3></div>
    <div class="field"><div class="lbl">Anbieter<small>OpenAI = stark &amp; mit Websuche · Ollama = lokal, offline, kostenlos</small></div>
      <div class="ctl"><select data-key="ai.provider" data-rerender>${opt([["openai", "OpenAI"], ["ollama", "Ollama (lokal)"]], c.ai.provider)}</select></div></div>
    ${c.ai.provider === "ollama" ? `
    <div class="field col"><div class="lbl">Ollama-Adresse<small>Ollama von ollama.com installieren, dann z. B. „ollama pull qwen3“</small></div>
      <div class="ctl"><input data-key="ai.ollama_url" value="${esc(c.ai.ollama_url)}" data-blur></div></div>` : ""}
    <div class="field col"><div class="lbl">OpenAI-API-Schlüssel<small>${S.has_key ? "Gespeichert (sicher in der Windows-Anmeldeverwaltung)" : "Noch nicht eingerichtet – ohne Schlüssel arbeitet JARVIS rein lokal."}</small></div>
      <div class="ctl"><input type="password" id="s-key" placeholder="${S.has_key ? "•••••••• (neu eingeben zum Ändern)" : "sk-…"}"><button id="s-key-save">Speichern</button>${S.has_key ? '<button class="ghost danger" id="s-key-del">Entfernen</button>' : ""}</div>
      <div id="s-key-msg" class="status-msg"></div></div>
    <div class="field"><div class="lbl">KI verwenden<small>Nur wenn lokale Befehle nicht reichen</small></div><div class="ctl">${sw("ai.enabled", c.ai.enabled)}</div></div>
    <div class="field"><div class="lbl">Modell${c.ai.provider === "ollama" ? " (Ollama)" : ""}</div><div class="ctl"><select data-key="${c.ai.provider === "ollama" ? "ai.ollama_model" : "ai.model"}" id="s-model"><option>${esc(c.ai.provider === "ollama" ? (c.ai.ollama_model || "– wählen –") : c.ai.model)}</option></select><button class="ghost small" id="s-models" title="Modelle laden">↻</button></div></div>
    <div class="field"><div class="lbl">Websuche<small>KI darf bei Bedarf im Internet suchen</small></div><div class="ctl">${sw("ai.web_search", c.ai.web_search)}</div></div>
    <div class="field"><div class="lbl">Bildschirm verstehen<small>Screenshot-Analyse auf Kommando</small></div><div class="ctl">${sw("security.screen_ai", c.security.screen_ai)}</div></div>
    <div class="field"><div class="lbl">Verbindung testen</div><div class="ctl"><button class="ghost" id="s-ai-test">Testen</button></div></div>
  </div>

  <div class="card"><div class="card-head"><h3>Stimme</h3><button class="small" id="s-voice-test">▶ Probehören</button></div>
    <div class="field"><div class="lbl">Sprachausgabe</div><div class="ctl">${sw("voice.tts_enabled", v.tts_enabled)}</div></div>
    <div class="field"><div class="lbl">Stimm-Engine<small>Neural = natürlich &amp; kostenlos (online) · OpenAI / ElevenLabs = Premium · Windows = offline</small></div>
      <div class="ctl"><select data-key="voice.tts_engine" data-rerender>${opt([["edge", "Neural (Microsoft)"], ["openai", "OpenAI KI-Stimme"], ["elevenlabs", "ElevenLabs (Premium-KI)"], ["system", "Windows (lokal)"]], v.tts_engine)}</select></div></div>
    ${v.tts_engine === "elevenlabs" ? `
    <div class="field col"><div class="lbl">ElevenLabs-API-Schlüssel<small>${S.has_eleven_key ? "Gespeichert (sicher in der Windows-Anmeldeverwaltung)" : "Kostenlos auf elevenlabs.io → Profil → API Keys erstellen"}</small></div>
      <div class="ctl"><input type="password" id="s-el-key" placeholder="${S.has_eleven_key ? "•••••••• (neu eingeben zum Ändern)" : "sk_…"}"><button id="s-el-save">Speichern</button>${S.has_eleven_key ? '<button class="ghost danger" id="s-el-del">Entfernen</button>' : ""}</div>
      <div id="s-el-msg" class="status-msg"></div></div>` : ""}
    ${voiceSel ? `<div class="field"><div class="lbl">Stimme</div><div class="ctl"><select data-key="${voiceKey}">${voiceSel}</select></div></div>` : ""}
    <div class="field"><div class="lbl">Lautstärke <small id="vol-l">${v.volume} %</small></div><div class="ctl"><input type="range" min="0" max="100" value="${v.volume}" data-key="voice.volume" data-num data-label="vol-l" data-suffix=" %"></div></div>
    <div class="field"><div class="lbl">Sprechtempo <small id="rate-l">${v.rate > 0 ? "+" : ""}${v.rate} %</small></div><div class="ctl"><input type="range" min="-50" max="50" step="5" value="${v.rate}" data-key="voice.rate" data-num data-label="rate-l" data-suffix=" %"></div></div>
    <div class="field"><div class="lbl">Ausgabegerät</div><div class="ctl"><select data-key="voice.output_device">${devOpt(devs.outputs, v.output_device)}</select></div></div>
  </div>

  <div class="card"><div class="card-head"><h3>Zuhören</h3></div>
    <div class="field"><div class="lbl">Wake-Word „Hey Jarvis“<small>Läuft lokal, auch im Hintergrund</small></div><div class="ctl">${sw("voice.wake_word", v.wake_word)}</div></div>
    <div class="field"><div class="lbl">Empfindlichkeit <small id="thr-l">${Math.round((1 - v.wake_threshold) * 100)} %</small></div><div class="ctl"><input type="range" min="0.2" max="0.9" step="0.05" value="${v.wake_threshold}" data-key="voice.wake_threshold" data-num data-label="thr-l" data-fmt="inv"></div></div>
    <div class="field"><div class="lbl">Aktivieren durch Klatschen<small>Zweimal kurz klatschen = „Hey Jarvis“</small></div><div class="ctl">${sw("voice.clap_wake", v.clap_wake)}</div></div>
    <div class="field"><div class="lbl">Klatsch-Empfindlichkeit <small id="clap-l">${Math.round(v.clap_sensitivity * 100)} %</small><small>Höher = reagiert auf leiseres Klatschen, aber auch eher auf Geräusche</small></div><div class="ctl"><input type="range" min="0.1" max="0.9" step="0.05" value="${v.clap_sensitivity}" data-key="voice.clap_sensitivity" data-num data-label="clap-l" data-fmt="pct"></div></div>
    <div class="field"><div class="lbl">Gesprächsmodus<small>Nach dem Wecken weiter zuhören, bis du fertig bist („Danke“, „Das war's“ oder Stille)</small></div><div class="ctl">${sw("voice.conversation", v.conversation)}</div></div>
    <div class="field"><div class="lbl">Auf nächste Frage warten <small id="fu-l">${v.follow_up_seconds} s</small><small>Danach beendet JARVIS das Gespräch mit einem kurzen Ton</small></div><div class="ctl"><input type="range" min="3" max="20" step="1" value="${v.follow_up_seconds}" data-key="voice.follow_up_seconds" data-num data-label="fu-l" data-suffix=" s"></div></div>
    <div class="field"><div class="lbl">Pause bis Satzende <small id="sil-l">${v.silence_seconds} s</small><small>Länger = du kannst beim Sprechen nachdenken</small></div><div class="ctl"><input type="range" min="0.8" max="4" step="0.1" value="${v.silence_seconds}" data-key="voice.silence_seconds" data-num data-label="sil-l" data-suffix=" s"></div></div>
    <div class="field"><div class="lbl">Maximale Aufnahmelänge <small id="maxrec-l">${v.max_record_seconds} s</small></div><div class="ctl"><input type="range" min="10" max="90" step="5" value="${v.max_record_seconds}" data-key="voice.max_record_seconds" data-num data-label="maxrec-l" data-suffix=" s"></div></div>
    <div class="field"><div class="lbl">Spracherkennung<small>OpenAI = sehr genau · Lokal = privat, offline (lädt einmalig ein Modell)</small></div>
      <div class="ctl"><select data-key="voice.stt_engine">${opt([["openai", "OpenAI"], ["local", "Lokal (Whisper)"]], v.stt_engine)}</select></div></div>
    <div class="field"><div class="lbl">Lokales Modell</div><div class="ctl"><select data-key="voice.local_stt_model">${opt([["base", "Base (schnell)"], ["small", "Small (ausgewogen)"], ["medium", "Medium (genau, langsam)"]], v.local_stt_model)}</select></div></div>
    <div class="field"><div class="lbl">Mikrofon</div><div class="ctl"><select data-key="voice.input_device">${devOpt(devs.inputs, v.input_device)}</select></div></div>
    <div class="field"><div class="lbl">Signalton beim Zuhören</div><div class="ctl">${sw("voice.chime", v.chime)}</div></div>
  </div>

  <div class="card"><div class="card-head"><h3>Modi &amp; Nicht stören</h3></div>
    <div class="field"><div class="lbl">Aktueller Modus<small>Auch per Sprache: „Gaming-Modus an“, „Nicht stören“, „Modus beenden“</small></div>
      <div class="ctl"><select id="s-focus">${opt([["normal", "Normal"], ["gaming", "🎮 Gaming"], ["film", "🎬 Film"], ["schlafen", "🌙 Schlafen"], ["arbeit", "💼 Arbeit / Nicht stören"]], S.status.modules.focus?.mode || "normal")}</select></div></div>
    <div class="field"><div class="lbl">Automatisch erkennen<small>Spiel im Vollbild → Gaming · Video im Vollbild → Film</small></div><div class="ctl">${sw("focus.auto", c.focus.auto)}</div></div>
    <div class="field"><div class="lbl">Schlafmodus nach Zeitplan</div><div class="ctl">${sw("focus.sleep.enabled", c.focus.sleep.enabled)}
      <input type="time" data-key="focus.sleep.from" value="${esc(c.focus.sleep.from)}" style="width:110px"> – <input type="time" data-key="focus.sleep.to" value="${esc(c.focus.sleep.to)}" style="width:110px"></div></div>
    <p class="hint">Gaming &amp; Arbeit: vorlesen nur Dringendes, Unwichtiges wird gesammelt. Film: vorlesen nur Kritisches. Schlafen: nur Kritisches wird überhaupt gemeldet.
      Antworten auf deine eigenen Fragen spricht JARVIS immer. Verpasstes: „Was habe ich verpasst?“</p>
  </div>

  <div class="card"><div class="card-head"><h3>Sicherheit &amp; Datenschutz</h3></div>
    <div class="field"><div class="lbl">Bestätigung erforderlich ab<small>Welche Aktionen JARVIS vorher bestätigen lässt</small></div>
      <div class="ctl"><select data-key="security.confirm_level" data-int>${opt([["1", "allen Aktionen"], ["2", "heiklen Aktionen (empfohlen)"], ["3", "nur kritischen Aktionen"]], String(c.security.confirm_level))}</select></div></div>
    <div class="field"><div class="lbl">Datenschutzmodus<small>Alles lokal: keine KI, lokale Spracherkennung und Stimme</small></div><div class="ctl">${sw("security.privacy_mode", c.security.privacy_mode)}</div></div>
    <p class="hint">Dateien werden nie endgültig gelöscht, sondern höchstens nach Rückfrage in den Papierkorb verschoben. Not-Aus: Knopf oben rechts oder „Jarvis, stopp“.</p>
  </div>

  <div class="card"><div class="card-head"><h3>Morgen-Briefing</h3><button class="small" id="s-brief">▶ Jetzt</button></div>
    <p class="hint">Sag „Guten Morgen“ oder „Briefing“. Zeitgesteuert: „Jeden Tag um 7 Uhr Briefing“.</p>
    <div class="field col"><div class="lbl">Ort fürs Wetter<small>Leer = dein Wohnort aus dem Gedächtnis („Ich wohne in …“)</small></div>
      <div class="ctl"><input data-key="briefing.city" value="${esc(c.briefing.city)}" placeholder="z. B. Berlin" data-blur></div></div>
    <div class="field"><div class="lbl">Nachrichten einbauen<small>per OpenAI mit Websuche</small></div><div class="ctl">${sw("briefing.include_news", c.briefing.include_news)}</div></div>
  </div>

  <div class="card"><div class="card-head"><h3>Allgemein</h3></div>
    <div class="field col"><div class="lbl">Dein Name</div><div class="ctl"><input data-key="user_name" value="${esc(c.user_name)}" data-blur></div></div>
    <div class="field"><div class="lbl">Mit Windows starten<small>JARVIS startet unsichtbar im Tray</small></div><div class="ctl">${sw("app.start_with_windows", c.app.start_with_windows)}</div></div>
    <div class="field"><div class="lbl">Minimiert starten</div><div class="ctl">${sw("app.start_minimized", c.app.start_minimized)}</div></div>
    <div class="field"><div class="lbl">Schließen = in den Tray</div><div class="ctl">${sw("app.close_to_tray", c.app.close_to_tray)}</div></div>
    <div class="field"><div class="lbl">Startanimation</div><div class="ctl">${sw("app.boot_animation", c.app.boot_animation)}</div></div>
    <div class="field"><div class="lbl">Benachrichtigungen</div><div class="ctl">${sw("app.notifications", c.app.notifications)}</div></div>
    <div class="field"><div class="lbl">Aktiver Modus<small>Auf Ereignisse reagieren (Akku, USB, Programmstarts …)</small></div><div class="ctl">${sw("app.active_mode", c.app.active_mode)}</div></div>
    <div class="field"><div class="lbl">Tägliches Backup</div><div class="ctl">${sw("app.auto_backup", c.app.auto_backup)}</div></div>
    <div class="field"><div class="lbl">Einrichtungsassistent</div><div class="ctl"><button class="ghost" id="s-wizard">Erneut starten</button></div></div>
  </div>

  <div class="card"><div class="card-head"><h3>Updates</h3><span class="meta">Version ${esc(S.version)}</span></div>
    <div class="field col"><div class="lbl">Update-Quelle<small>GitHub-Repository oder Manifest-URL</small></div><div class="ctl"><input data-key="update.url" value="${esc(c.update.url)}" placeholder="https://github.com/…/jarvis" data-blur></div></div>
    <div class="field"><div class="lbl">Automatisch nach Updates suchen</div><div class="ctl">${sw("update.auto_check", c.update.auto_check)}</div></div>
    <div class="field"><div class="lbl">Jetzt prüfen</div><div class="ctl"><button class="ghost" id="s-upd-check">Prüfen</button><button id="s-upd-inst">Installieren</button></div></div>
    <p class="hint">Vor jedem Update wird automatisch gesichert. Startet eine neue Version nicht, stellt JARVIS die alte selbst wieder her.</p>
  </div>`;

  // Bindungen
  $$("#settings [data-key]").forEach(el => {
    const ev = el.type === "range" ? "input" : el.dataset.blur !== undefined ? "change" : "change";
    el.addEventListener(ev, async () => {
      let val = el.type === "checkbox" ? el.checked : el.value;
      if (val === "– wählen –") return;
      if (el.dataset.num !== undefined) val = parseFloat(val);
      if (el.dataset.int !== undefined) val = parseInt(val);
      if (el.dataset.label) $("#" + el.dataset.label).textContent = el.dataset.fmt === "inv" ? Math.round((1 - val) * 100) + " %" : el.dataset.fmt === "pct" ? Math.round(val * 100) + " %" : (val > 0 && el.min < 0 ? "+" : "") + val + (el.dataset.suffix || "");
      if (el.tagName === "SELECT" && (el.dataset.key.endsWith("_device")) && val === "") val = null;
      clearTimeout(el._t);
      el._t = setTimeout(async () => { S = await api.settings_set({ [el.dataset.key]: val }); if (el.dataset.rerender !== undefined) renderSettings(); }, el.type === "range" ? 250 : 0);
    });
  });
  $("#s-key-save").onclick = async () => {
    const k = $("#s-key").value.trim(); if (!k) return;
    $("#s-key-msg").textContent = "Prüfe …"; const r = await api.set_api_key(k);
    $("#s-key-msg").textContent = r.msg; $("#s-key-msg").className = "status-msg " + (r.ok ? "ok" : "bad"); if (r.ok) setTimeout(renderSettings, 1200);
  };
  $("#s-focus").onchange = async e => toast(await api.focus_set(e.target.value), "Modus");
  $("#s-el-save") && ($("#s-el-save").onclick = async () => {
    const k = $("#s-el-key").value.trim(); if (!k) return;
    $("#s-el-msg").textContent = "Prüfe …"; const r = await api.set_elevenlabs_key(k);
    $("#s-el-msg").textContent = r.msg; $("#s-el-msg").className = "status-msg " + (r.ok ? "ok" : "bad"); if (r.ok) setTimeout(renderSettings, 1200);
  });
  $("#s-el-del") && ($("#s-el-del").onclick = () => confirmBox("ElevenLabs-Schlüssel entfernen?", async () => { await api.set_elevenlabs_key(""); renderSettings(); }));
  $("#s-key-del") && ($("#s-key-del").onclick = () => confirmBox("API-Schlüssel entfernen?", async () => { await api.set_api_key(""); renderSettings(); }));
  $("#s-models").onclick = loadModels;
  $("#s-ai-test").onclick = async () => { const r = await api.test_openai(); toast(r.msg, "OpenAI", !r.ok); };
  $("#s-voice-test").onclick = () => api.test_voice();
  $("#s-brief").onclick = () => { go("home"); send("Briefing"); };
  $("#s-wizard").onclick = wizard;
  $("#s-upd-check").onclick = async () => toast(await api.update_check(), "Update");
  $("#s-upd-inst").onclick = () => confirmBox("Update jetzt installieren? JARVIS startet danach neu.", async () => toast(await api.update_install(), "Update"));
  if (S.has_key || S.config.ai.provider === "ollama") loadModels();
}
async function loadModels() {
  try {
    const list = await api.models(); if (!list.length) return;
    const cur = S.config.ai.provider === "ollama" ? S.config.ai.ollama_model : S.config.ai.model;
    if (S.config.ai.provider === "ollama" && !cur) list.unshift("– wählen –");
    $("#s-model").innerHTML = list.map(m => `<option ${m === cur ? "selected" : ""}>${esc(m)}</option>`).join("");
  } catch {}
}

/* ===================================================== Einrichtungsassistent */
async function wizard() {
  const steps = [wizWelcome, wizAI, wizVoice, wizMic, wizApps, wizPhone, wizHome, wizDone];
  let i = 0; const data = {};
  $("#wizard").classList.remove("hidden");
  const show = async () => {
    $("#wiz-steps").innerHTML = steps.map((_, k) => `<i class="${k <= i ? "done" : ""}"></i>`).join("");
    $("#wiz-back").style.visibility = i ? "visible" : "hidden";
    $("#wiz-skip").style.visibility = i && i < steps.length - 1 ? "visible" : "hidden";
    $("#wiz-next").textContent = i === steps.length - 1 ? "Los geht's" : "Weiter";
    await steps[i]($("#wiz-body"), data);
  };
  $("#wiz-back").onclick = () => { i = Math.max(0, i - 1); show(); };
  $("#wiz-skip").onclick = () => { i++; show(); };
  $("#wiz-next").onclick = async () => {
    const b = $("#wiz-body");
    if (b._save && (await b._save()) === false) return;
    b._save = null;
    if (i === steps.length - 1) {
      await api.setup_complete(data); $("#wizard").classList.add("hidden"); S = await api.state();
      addMsg("jarvis", `Einrichtung abgeschlossen${data.user_name ? ", " + data.user_name : ""}. Ich bin bereit.`);
      api.test_voice(`Einrichtung abgeschlossen${data.user_name ? ", " + data.user_name : ""}. Ich bin bereit.`);
      return;
    }
    i++; show();
  };
  show();
}
async function wizWelcome(b, data) {
  b.innerHTML = `<h2>Willkommen bei JARVIS</h2><p>Ich führe dich in wenigen Schritten durch die Einrichtung. Alles lässt sich später auch per Sprache oder in den Einstellungen ändern.</p>
    <div class="form"><label>Wie darf ich dich nennen?<input id="w-name" value="${esc(S.config.user_name || "")}" placeholder="Dein Name"></label></div>`;
  b._save = () => { data.user_name = $("#w-name").value.trim(); };
}
async function wizAI(b) {
  b.innerHTML = `<h2>KI verbinden</h2><p>Mit einem OpenAI-API-Schlüssel beantwortet JARVIS beliebige Fragen, versteht komplexe Wünsche und deinen Bildschirm.
    Lokale Befehle (Programme, Lautstärke, Gedächtnis, Routinen …) funktionieren auch ohne und kosten nichts.
    Alternativ läuft JARVIS mit lokalen Modellen über Ollama (Einstellungen → KI → Anbieter).</p>
    <div class="form"><label>OpenAI-API-Schlüssel (platform.openai.com → API keys)<input id="w-key" type="password" placeholder="${S.has_key ? "bereits gespeichert" : "sk-…"}"></label>
    <div id="w-key-msg" class="status-msg"></div>
    <label>Modell<select id="w-model"><option>${esc(S.config.ai.model)}</option></select></label></div>`;
  const fill = async () => { try { const l = await api.models(); if (l.length) $("#w-model").innerHTML = l.map(m => `<option ${m === S.config.ai.model ? "selected" : ""}>${esc(m)}</option>`).join(""); } catch {} };
  if (S.has_key) fill();
  b._save = async () => {
    const k = $("#w-key").value.trim();
    if (k) {
      $("#w-key-msg").textContent = "Prüfe Schlüssel …";
      const r = await api.set_api_key(k);
      $("#w-key-msg").textContent = r.msg; $("#w-key-msg").className = "status-msg " + (r.ok ? "ok" : "bad");
      if (!r.ok) return false;
      S.has_key = true; await fill();
    }
    await api.settings_set({ "ai.model": $("#w-model").value });
  };
}
async function wizVoice(b) {
  const voices = await api.voices(); const v = S.config.voice;
  b.innerHTML = `<h2>Stimme wählen</h2><p>Wähle, wie JARVIS klingen soll. „Neural“ klingt menschlich und ist kostenlos, „OpenAI“ ist die Premium-KI-Stimme.</p>
    <div class="form"><label>Engine<select id="w-eng"><option value="edge">Neural (Microsoft, empfohlen)</option><option value="openai" ${S.has_key ? "" : "disabled"}>OpenAI KI-Stimme</option><option value="elevenlabs" ${S.has_eleven_key ? "" : "disabled"}>ElevenLabs (Premium-KI)</option><option value="system">Windows (offline)</option><option value="off">Keine Sprachausgabe</option></select></label>
    <label>Stimme<select id="w-voice"></select></label><button class="ghost" id="w-test">▶ Probehören</button></div>`;
  const eng = $("#w-eng"); eng.value = v.tts_enabled ? v.tts_engine : "off";
  const fill = () => {
    const e = eng.value;
    $("#w-voice").innerHTML = e === "edge" ? voices.edge.map(([id, l]) => `<option value="${id}" ${id === v.edge_voice ? "selected" : ""}>${esc(l)}</option>`).join("")
      : e === "openai" ? voices.openai.map(x => `<option ${x === v.openai_voice ? "selected" : ""}>${x}</option>`).join("")
      : e === "elevenlabs" ? voices.elevenlabs.map(([id, l]) => `<option value="${id}" ${id === v.elevenlabs_voice ? "selected" : ""}>${esc(l)}</option>`).join("") : "<option>–</option>";
    $("#w-voice").disabled = !["edge", "openai", "elevenlabs"].includes(e);
  };
  const apply = () => {
    const e = eng.value, vals = { "voice.tts_enabled": e !== "off", "voice.tts_engine": e === "off" ? "edge" : e };
    if (e === "edge") vals["voice.edge_voice"] = $("#w-voice").value;
    if (e === "openai") vals["voice.openai_voice"] = $("#w-voice").value;
    if (e === "elevenlabs") vals["voice.elevenlabs_voice"] = $("#w-voice").value;
    return api.settings_set(vals);
  };
  eng.onchange = fill; fill();
  $("#w-test").onclick = async () => { await apply(); api.test_voice(); };
  b._save = apply;
}
async function wizMic(b) {
  const devs = await api.audio_devices();
  b.innerHTML = `<h2>Mikrofon</h2><p>Wähle dein Mikrofon und sprich zum Test etwas.</p>
    <div class="form"><label>Mikrofon<select id="w-mic"><option value="">Windows-Standard</option>${devs.inputs.map(x => `<option ${x === S.config.voice.input_device ? "selected" : ""}>${esc(x)}</option>`).join("")}</select></label>
    <button class="ghost" id="w-mic-test">Pegel testen (1,5 Sekunden sprechen)</button><div class="meter"><i id="w-meter"></i></div><div id="w-mic-msg" class="status-msg"></div>
    <label class="check"><input type="checkbox" id="w-wake" ${S.config.voice.wake_word ? "checked" : ""}> Wake-Word „Hey Jarvis“ aktivieren (lokal, im Hintergrund)</label></div>`;
  $("#w-mic").onchange = () => api.settings_set({ "voice.input_device": $("#w-mic").value || null });
  $("#w-mic-test").onclick = async () => {
    $("#w-mic-msg").textContent = "Sprich jetzt …";
    const lvl = await api.mic_level(); const pct = Math.min(100, lvl / 20);
    $("#w-meter").style.width = pct + "%";
    $("#w-mic-msg").textContent = lvl > 150 ? "Mikrofon funktioniert." : lvl > 20 ? "Sehr leise – näher ans Mikrofon oder Pegel erhöhen." : "Kein Signal – anderes Mikrofon wählen.";
    $("#w-mic-msg").className = "status-msg " + (lvl > 150 ? "ok" : "bad");
  };
  b._save = () => api.settings_set({ "voice.wake_word": $("#w-wake").checked });
}
async function wizApps(b) {
  b.innerHTML = `<h2>Programme erkennen</h2><p>JARVIS durchsucht Startmenü, Store-Apps, Desktop und deine Steam-Bibliothek …</p><div id="w-apps" class="empty">Suche läuft …</div>`;
  const txt = await api.apps_rescan(); const apps = await api.apps();
  const games = apps.filter(a => a.source === "steam");
  $("#w-apps").className = "";
  $("#w-apps").innerHTML = `<p style="color:var(--ok)">${esc(txt)}</p>
    ${games.length ? `<p><b>Spiele:</b> ${games.slice(0, 20).map(a => esc(a.name)).join(", ")}</p>` : ""}
    <p class="hint">Neu installierte Programme erkennt JARVIS automatisch. Profile legst du per Sprache an: „Wenn ich FiveM sage, starte Discord und FiveM“.</p>`;
}
async function wizPhone(b) {
  const d = await api.phone();
  b.innerHTML = `<h2>Handy verbinden <small class="meta">optional</small></h2>
    <p>Android-Handys steuert JARVIS über ADB (USB-Debugging). iPhones werden per USB erkannt.</p>
    ${d.adb ? '<p style="color:var(--ok)">ADB ist eingerichtet.</p>' : '<button id="w-adb">Android-Unterstützung (ADB) einrichten</button>'}
    <div id="w-ph" style="margin-top:14px">${d.devices.map(x => `<div class="row"><span class="dot ok"></span>${esc(x.model)}</div>`).join("") || '<p class="hint">Aktuell kein Handy erkannt – das kannst du später unter „Handy“ nachholen.</p>'}</div>`;
  $("#w-adb") && ($("#w-adb").onclick = async e => { e.target.disabled = true; e.target.textContent = "Wird eingerichtet …"; await api.phone_install_adb(); wizPhone(b); });
}
async function wizHome(b) {
  b.innerHTML = `<h2>Zimmer &amp; Smart Home <small class="meta">optional</small></h2>
    <p>Lege deine Zimmer an. Geräte fügst du später einfach per Sprache hinzu – oder verbinde Home Assistant.</p>
    <div class="form"><label>Zimmer (mit Komma getrennt)<input id="w-rooms" placeholder="Wohnzimmer, Schlafzimmer, Küche"></label>
    <label>Home-Assistant-Adresse (optional)<input id="w-ha" placeholder="http://homeassistant.local:8123" value="${esc(S.config.smarthome.homeassistant_url || "")}"></label>
    <label>Home-Assistant-Token (optional)<input id="w-hat" type="password"></label><div id="w-ha-msg" class="status-msg"></div></div>`;
  b._save = async () => {
    for (const r of $("#w-rooms").value.split(",").map(x => x.trim()).filter(Boolean)) await api.room_add(r);
    if ($("#w-ha").value.trim() && $("#w-hat").value.trim()) {
      const r = await api.ha_setup($("#w-ha").value, $("#w-hat").value);
      $("#w-ha-msg").textContent = r.msg; $("#w-ha-msg").className = "status-msg " + (r.ok ? "ok" : "bad");
      if (r.ok) await api.ha_import(); else return false;
    }
  };
}
async function wizDone(b) {
  b.innerHTML = `<h2>Bereit</h2><p>JARVIS ist eingerichtet. Ein paar Ideen zum Ausprobieren:</p>
    <div class="list">${["„Hey Jarvis, starte Discord“", "„Mein Lieblingsspiel ist GTA“ – später: „Was ist mein Lieblingsspiel?“",
      "„Wenn ich FiveM sage, starte Discord, TeamSpeak und FiveM mit 5 Sekunden Pause“", "„Mach das leiser“ · „Schließ das“ · „Mach das nochmal“",
      "„Gaming-Modus“ · „Ich gehe schlafen“ · „Film-Modus“", "„Was siehst du auf meinem Bildschirm?“", "„Jeden Tag um 23 Uhr sperre den PC“"]
      .map(x => `<div class="row">${esc(x)}</div>`).join("")}</div>`;
}
