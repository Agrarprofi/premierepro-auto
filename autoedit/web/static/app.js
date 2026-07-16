/* autoedit Dashboard – Vanilla JS, kein Framework. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
let currentProject = null;
let overview = null;

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) { /* egal */ }
    throw new Error(msg);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res.text();
}

function fmtSec(s) { return `${Number(s).toFixed(1)} s`; }

// ------------------------------------------------------------ Projekte

async function loadProjects() {
  const projects = await api("/api/projects");
  const ul = $("#project-list");
  ul.innerHTML = "";
  for (const p of projects) {
    const li = document.createElement("li");
    li.textContent = p.name;
    if (p.name === currentProject) li.classList.add("active");
    li.onclick = () => openProject(p.name);
    ul.appendChild(li);
  }
}

async function openProject(name) {
  currentProject = name;
  await loadProjects();
  const main = $("#main");
  main.innerHTML = "";
  main.appendChild($("#tpl-project").content.cloneNode(true));
  $("#project-title").textContent = name;
  bindProjectEvents();
  await refreshOverview();
}

async function refreshOverview() {
  overview = await api(`/api/projects/${currentProject}/overview`);
  renderStatusChain();
  renderConfig();
  renderFiles();
  renderSync();
  renderStatements();
  renderSegments();
  renderBroll();
  renderSubtitleLinks();
  renderMusic();
  renderExport();
  renderCosts();
  loadPresets();
  loadMusicLibrary();
}

// ------------------------------------------------------------ Rendering

const STEP_LABELS = {
  ingest: "Ingest", transkript: "Transkript", sync: "Sync",
  schnitt: "Schnitt", broll: "B-Roll", untertitel: "Untertitel",
  musik: "Musik", export: "Export",
};

function renderStatusChain() {
  const chain = $("#status-chain");
  chain.innerHTML = "";
  for (const [step, label] of Object.entries(STEP_LABELS)) {
    const st = overview.status[step] || { status: "offen", detail: "" };
    const div = document.createElement("div");
    div.className = `step ${st.status}`;
    div.title = st.detail || "";
    div.innerHTML = `<span class="lamp"></span> ${label}
      <button class="one" title="Nur diesen Schritt ausführen">▶</button>
      <button class="from" title="Ab hier bis zum Ende ausführen (Fehler in B-Roll/Untertitel/Musik werden übersprungen)">⏩</button>`;
    $(".one", div).onclick = () => runStep(step);
    $(".from", div).onclick = () =>
      startJob(`/api/projects/${currentProject}/run_all`,
        { ...cutBody(), ab_schritt: step });
    chain.appendChild(div);
  }
}

function renderConfig() {
  const form = $("#config-form");
  for (const [key, val] of Object.entries(overview.config)) {
    const input = form.elements[key];
    if (!input) continue;
    if (input.type === "checkbox") input.checked = Boolean(val);
    else input.value = val;
  }
}

function renderFiles() {
  const div = $("#files");
  div.innerHTML = "";
  const labels = { cam_a: "Kamera A", cam_b: "Kamera B",
                   audio_dji: "DJI-Audio", broll: "B-Roll" };
  for (const [role, label] of Object.entries(labels)) {
    const files = overview.dateien[role] || [];
    const box = document.createElement("div");
    const infos = (overview.media?.clips || []).filter(c => c.rolle === role);
    box.innerHTML = `<h4>${label} (${files.length})</h4><ul>` +
      files.map(f => {
        const info = infos.find(c => c.name === f.name);
        const extra = info
          ? ` <span class="muted">${info.dauer.toFixed(1)}s` +
            (info.breite ? `, ${info.breite}×${info.hoehe}@${(info.fps || 0).toFixed(2)}` : "") +
            `</span>`
          : "";
        return `<li>${f.name}${extra}</li>`;
      }).join("") + "</ul>";
    div.appendChild(box);
  }
}

function renderSync() {
  const table = $("#sync-table");
  if (!overview.sync) { table.classList.add("hidden"); return; }
  table.classList.remove("hidden");
  const tbody = $("tbody", table);
  tbody.innerHTML = "";
  for (const [datei, o] of Object.entries(overview.sync.offsets)) {
    const tr = document.createElement("tr");
    const konf = o.konfidenz == null ? "–" : o.konfidenz.toFixed(2);
    const drift = o.drift_sekunden != null
      ? ` · Drift ${(o.drift_sekunden * 1000).toFixed(0)} ms` : "";
    tr.innerHTML = `<td>${datei}</td>
      <td><input type="number" step="0.001" style="width:8em"
           value="${o.offset_sekunden.toFixed(3)}"></td>
      <td>${konf}</td><td class="muted">${o.hinweis || ""}${drift}</td>`;
    const input = $("input", tr);
    input.onchange = async () => {
      try {
        await api(`/api/projects/${currentProject}/sync/offsets`, {
          method: "PUT",
          body: JSON.stringify({ relpfad: datei,
                                 offset_sekunden: Number(input.value) }),
        });
        refreshOverview();
      } catch (e) { alert(e.message); }
    };
    tbody.appendChild(tr);
  }
}

function renderStatements() {
  const box = $("#statements-box");
  const data = overview.statements;
  if (!data || !data.aussagen?.length) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const tbody = $("tbody", box);
  tbody.innerHTML = "";
  for (const a of data.aussagen) {
    const tr = document.createElement("tr");
    const quali = a.qualitaet !== "sauber" ? ` ⚠ ${a.qualitaet}` : "";
    tr.innerHTML = `<td><b>${a.punkte}</b></td><td>${a.kategorie}${quali}</td>
      <td>${a.start.toFixed(1)}–${a.ende.toFixed(1)}</td>
      <td>${a.text}</td><td class="muted">${a.kommentar || ""}</td>`;
    tbody.appendChild(tr);
  }
}

function renderSegments() {
  const wrap = $("#segments");
  wrap.innerHTML = "";
  const data = overview.segments;
  if (!data) { $("#segments-total").textContent = ""; return; }
  data.segmente.forEach((seg, i) => {
    const div = document.createElement("div");
    div.className = "segment" + (seg.aktiv ? "" : " inactive");
    const takeJoin = seg.fortsetzung
      ? '<b>⟂ Take-Kombination:</b> setzt das vorherige Segment mitten im Satz fort · '
      : "";
    div.innerHTML = `
      <input type="checkbox" ${seg.aktiv ? "checked" : ""} title="Segment verwenden">
      <div class="text">${seg.text}
        <div class="meta">${takeJoin}${seg.start.toFixed(2)}–${seg.ende.toFixed(2)} s
          (${fmtSec(seg.dauer)}) – ${seg.begruendung || ""}</div>
      </div>
      <button class="up" title="nach oben">↑</button>
      <button class="down" title="nach unten">↓</button>`;
    $("input", div).onchange = e => updateSegments({ aktiv: { [seg.id]: e.target.checked } });
    $(".up", div).onclick = () => moveSegment(i, -1);
    $(".down", div).onclick = () => moveSegment(i, +1);
    wrap.appendChild(div);
  });
  const aktivDauer = data.segmente.filter(s => s.aktiv)
    .reduce((a, s) => a + s.dauer, 0);
  $("#segments-total").textContent =
    `Aktive Gesamtlänge: ${fmtSec(aktivDauer)} von max. ${data.reel_laenge_sek} s`;
}

function moveSegment(index, delta) {
  const ids = overview.segments.segmente.map(s => s.id);
  const j = index + delta;
  if (j < 0 || j >= ids.length) return;
  [ids[index], ids[j]] = [ids[j], ids[index]];
  updateSegments({ order: ids });
}

async function updateSegments(body) {
  try {
    await api(`/api/projects/${currentProject}/segments`,
      { method: "PUT", body: JSON.stringify(body) });
    await refreshOverview();
  } catch (e) { alert(e.message); }
}

function renderBroll() {
  const wrap = $("#broll-matches");
  wrap.innerHTML = "";
  const data = overview.broll_matches;
  if (!data || !data.matches.length) {
    wrap.innerHTML = '<p class="muted">Noch keine Zuordnungen.</p>';
    return;
  }
  for (const m of data.matches) {
    const div = document.createElement("div");
    div.className = "match";
    const thumb = m.thumbnail
      ? `<img src="/api/projects/${currentProject}/files/${m.thumbnail}">` : "";
    div.innerHTML = `${thumb}
      <div class="text">bei ${m.transkript_zeit.toFixed(1)} s im Reel:
        <b>${m.broll_datei}</b> (Einstieg ${m.broll_einstieg.toFixed(1)} s,
        ${fmtSec(m.dauer)})
        <div class="meta">${m.begruendung || ""}</div>
      </div>
      <button title="Zuordnung löschen">✕</button>`;
    $("button", div).onclick = async () => {
      await api(`/api/projects/${currentProject}/broll/matches/${m.id}`,
        { method: "DELETE" });
      refreshOverview();
    };
    wrap.appendChild(div);
  }
}

async function renderSubtitleLinks() {
  try {
    const subs = await api(`/api/projects/${currentProject}/subtitles`);
    const base = `/api/projects/${currentProject}/files/`;
    $("#link-srt").classList.toggle("hidden", !subs.srt);
    $("#link-srt").href = base + "reel.srt";
    $("#link-srt-korr").classList.toggle("hidden", !subs.korrigiert);
    $("#link-srt-korr").href = base + "reel_korrigiert.srt";
    const diffDiv = $("#subtitle-diff");
    diffDiv.innerHTML = "";
    const changed = (subs.diff?.diff || []).filter(d => d.geaendert);
    if (subs.diff?.fehler) {
      diffDiv.innerHTML = `<p class="muted">${subs.diff.fehler}</p>`;
    } else if (changed.length) {
      diffDiv.innerHTML = "<table><thead><tr><th>#</th><th>Vorher</th>" +
        "<th>Nachher</th></tr></thead><tbody>" +
        changed.map(d =>
          `<tr><td>${d.nr}</td><td class="diff-alt">${d.vorher}</td>` +
          `<td class="diff-neu">${d.nachher}</td></tr>`).join("") +
        "</tbody></table>";
    } else if (subs.diff) {
      diffDiv.innerHTML = '<p class="muted">Korrekturlauf ohne Änderungen.</p>';
    }
  } catch (e) { /* noch keine Untertitel */ }
}

function renderMusic() {
  const span = $("#music-current");
  span.textContent = overview.musik
    ? `Gewählt: ${overview.musik.name} (${overview.musik.quelle}) – ${overview.musik.begruendung || ""}`
    : "Kein Track gewählt.";
}

function renderExport() {
  const link = $("#link-xml");
  const xmlName = `${currentProject}_premiere.xml`;
  const done = overview.status.export?.status === "ok";
  link.classList.toggle("hidden", !done);
  link.href = `/api/projects/${currentProject}/files/${xmlName}`;
  link.download = xmlName;
  const w = overview.export_warnungen || [];
  $("#export-warnungen").innerHTML = w.length
    ? '<p class="muted">⚠ ' + w.join("<br>⚠ ") + "</p>"
    : "";
}

async function renderCosts() {
  try {
    const est = await api(`/api/projects/${currentProject}/cost_estimate`);
    $("#costs").innerHTML =
      `Geschätzt pro Lauf: <b>$${est.summe_usd}</b> (${est.modell}) – ` +
      `bisher verbraucht: $${est.bisher_usd}<br>` +
      est.posten.map(p => `${p.zweck}: $${p.kosten_usd}`).join(" · ");
  } catch (e) { $("#costs").textContent = e.message; }
}

async function loadPresets() {
  const presets = await api("/api/presets");
  const sel = $("#preset-select");
  sel.innerHTML = '<option value="">Preset wählen…</option>';
  for (const name of Object.keys(presets)) {
    sel.insertAdjacentHTML("beforeend",
      `<option value="${name}">${name}</option>`);
  }
}

async function loadMusicLibrary() {
  try {
    const tracks = await api("/api/music/library");
    const sel = $("#music-select");
    sel.innerHTML = '<option value="">– manuell wählen –</option>';
    for (const t of tracks) {
      sel.insertAdjacentHTML("beforeend",
        `<option value="${t.name}">${t.name} (${t.dauer}s)</option>`);
    }
    if (overview.musik) sel.value = overview.musik.name;
  } catch (e) { /* library leer */ }
}

// ------------------------------------------------------------ Jobs

async function pollJob(job, onDone) {
  const el = $("#job-status");
  el.className = "job";
  el.innerHTML = `<div>${job.name}: <span class="msg"></span></div>
    <div class="bar"><div></div></div>`;
  const timer = setInterval(async () => {
    try {
      const j = await api(`/api/jobs/${job.id}`);
      $(".msg", el).textContent = j.meldung || j.status;
      $(".bar > div", el).style.width = `${j.fortschritt * 100}%`;
      if (j.status !== "laeuft") {
        clearInterval(timer);
        if (j.status === "fehler") {
          el.classList.add("fehler");
          $(".msg", el).textContent = j.fehler;
        } else {
          $(".msg", el).textContent = j.meldung || "fertig";
          if (onDone) onDone(j);
        }
        refreshOverview();
      }
    } catch (e) { clearInterval(timer); }
  }, 800);
}

async function startJob(path, body, onDone) {
  try {
    const job = await api(path, { method: "POST",
      body: body ? JSON.stringify(body) : "{}" });
    pollJob(job, onDone);
  } catch (e) { alert(e.message); }
}

function runStep(step) {
  const body = step === "schnitt" ? cutBody() : {};
  startJob(`/api/projects/${currentProject}/steps/${step}`, body);
}

function cutBody() {
  const modus = $("#cut-mode").value;
  return { modus, skript: modus === "skript" ? $("#cut-script").value : null };
}

// ------------------------------------------------------------ Events

function bindProjectEvents() {
  $("#btn-run-all").onclick = () =>
    startJob(`/api/projects/${currentProject}/run_all`,
      { ...cutBody(), fortsetzen: true });

  $("#btn-run-fresh").onclick = () => {
    if (!confirm("Wirklich alle Schritte neu berechnen? " +
                 "(inkl. Transkription – das kann dauern und kostet API-Aufrufe)"))
      return;
    startJob(`/api/projects/${currentProject}/run_all`,
      { ...cutBody(), fortsetzen: false });
  };

  $("#btn-save-config").onclick = async () => {
    const form = $("#config-form");
    const updates = {
      reel_laenge_sek: Number(form.elements.reel_laenge_sek.value),
      broll_dauer_sek: Number(form.elements.broll_dauer_sek.value),
      broll_ziel_anzahl: Number(form.elements.broll_ziel_anzahl.value),
      broll_min_abstand_sek: Number(form.elements.broll_min_abstand_sek.value),
      sprache: form.elements.sprache.value,
      export_format: form.elements.export_format.value,
      musik_aktiv: form.elements.musik_aktiv.checked,
      untertitel_aktiv: form.elements.untertitel_aktiv.checked,
      claude_modell: form.elements.claude_modell.value,
      schnitt_hinweise: form.elements.schnitt_hinweise.value,
    };
    try {
      await api(`/api/projects/${currentProject}/config`,
        { method: "PUT", body: JSON.stringify(updates) });
      refreshOverview();
    } catch (e) { alert(e.message); }
  };

  $("#btn-apply-preset").onclick = async () => {
    const preset = $("#preset-select").value;
    if (!preset) return;
    await api(`/api/projects/${currentProject}/apply_preset`,
      { method: "POST", body: JSON.stringify({ preset }) });
    refreshOverview();
  };

  $("#btn-save-preset").onclick = async () => {
    const name = $("#preset-name").value.trim();
    if (!name) return alert("Preset-Name eingeben");
    await api("/api/presets", { method: "POST",
      body: JSON.stringify({ name, projekt: currentProject }) });
    loadPresets();
  };

  $("#btn-transcribe").onclick = () => runStep("transkript");
  $("#btn-show-transcript").onclick = async () => {
    const pre = $("#transcript-preview");
    try {
      const t = await api(`/api/projects/${currentProject}/transcript`);
      pre.textContent = t.segmente
        .map(s => `[${s.start.toFixed(1)}–${s.end.toFixed(1)}] ${s.text}`)
        .join("\n");
      pre.classList.toggle("hidden");
    } catch (e) { alert(e.message); }
  };

  $("#btn-sync").onclick = () => runStep("sync");
  const syncPreview = rolle => () =>
    startJob(`/api/projects/${currentProject}/sync/preview`, { rolle }, () => {
      const v = $("#sync-video");
      v.src = `/api/projects/${currentProject}/files/preview_sync_${rolle}.mp4?t=${Date.now()}`;
      v.classList.remove("hidden");
    });
  $("#btn-sync-preview-a").onclick = syncPreview("cam_a");
  $("#btn-sync-preview-b").onclick = syncPreview("cam_b");

  $("#cut-mode").onchange = e =>
    $("#cut-script").classList.toggle("hidden", e.target.value !== "skript");
  $("#btn-cut").onclick = () => runStep("schnitt");
  $("#btn-cut-preview").onclick = () =>
    startJob(`/api/projects/${currentProject}/cut/preview`, null, () => {
      const v = $("#cut-video");
      v.src = `/api/projects/${currentProject}/files/preview_reel.mp4?t=${Date.now()}`;
      v.classList.remove("hidden");
    });

  $("#btn-broll").onclick = () => runStep("broll");
  $("#btn-subtitles").onclick = () => runStep("untertitel");
  $("#btn-music").onclick = () => runStep("musik");
  $("#music-select").onchange = async e => {
    if (!e.target.value) return;
    await api(`/api/projects/${currentProject}/music`,
      { method: "PUT", body: JSON.stringify({ name: e.target.value }) });
    refreshOverview();
  };
  $("#btn-export").onclick = () => runStep("export");

  $("#btn-log").onclick = async () => {
    $("#log").textContent =
      await api(`/api/projects/${currentProject}/log`);
  };
}

$("#btn-new-project").onclick = async () => {
  const name = $("#new-project-name").value.trim();
  if (!name) return;
  try {
    await api("/api/projects", { method: "POST",
      body: JSON.stringify({ name }) });
    $("#new-project-name").value = "";
    openProject(name);
  } catch (e) { alert(e.message); }
};

(async function init() {
  try {
    const h = await api("/api/health");
    $("#health").textContent =
      `${h.ffmpeg} · API-Key: ${h.anthropic_key ? "ok" : "FEHLT (.env)"} · ` +
      `Projekte: ${h.projekte_ordner}`;
  } catch (e) { $("#health").textContent = e.message; }
  loadProjects();
})();
