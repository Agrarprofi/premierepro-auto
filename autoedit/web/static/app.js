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

const batchSelection = new Set();

async function loadProjects() {
  const projects = await api("/api/projects");
  const ul = $("#project-list");
  ul.innerHTML = "";
  for (const p of projects) {
    const li = document.createElement("li");
    const done = Object.values(p.status).filter(s => s === "ok").length;
    const total = Object.keys(p.status).length;
    li.innerHTML = `<input type="checkbox" class="batch-check"
        title="Projekt auswählen – für die Warteschlange oder zum Löschen">
      <span class="pname">${p.name}</span>
      <span class="muted">${done}/${total}</span>`;
    const cb = $("input", li);
    cb.checked = batchSelection.has(p.name);
    cb.onclick = e => {
      e.stopPropagation();
      if (e.target.checked) batchSelection.add(p.name);
      else batchSelection.delete(p.name);
    };
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
  await attachRunningJob();
}

async function refreshOverview() {
  overview = await api(`/api/projects/${currentProject}/overview`);
  renderNextStep();
  renderStatusChain();
  renderConfig();
  renderFiles();
  renderSync();
  renderStatements();
  renderSkript();
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

const STEP_HINWEISE = {
  ingest: "Dateien einlesen und prüfen (VFR-Material wird gewandelt)",
  transkript: "Interview transkribieren (WhisperX, dauert etwas)",
  sync: "Kameras und Ton synchronisieren",
  schnitt: "Die besten Aussagen fürs Reel wählen lassen",
  broll: "B-Roll analysieren und im Reel platzieren",
  untertitel: "Untertitel erzeugen und korrigieren",
  musik: "Musik-Track wählen",
  export: "Premiere-XML erzeugen",
};

function renderNextStep() {
  const el = $("#next-step");
  const next = Object.keys(STEP_LABELS).find(
    s => overview.status[s]?.status !== "ok");
  el.classList.remove("hidden");
  if (!next) {
    el.className = "next-step done";
    el.innerHTML = "✅ Alles erledigt – die Premiere-XML steht unten im " +
      "Export-Bereich zum Download bereit.";
    return;
  }
  const st = overview.status[next];
  el.className = "next-step" + (st.status === "fehler" ? " fehler" : "");
  el.innerHTML = `${st.status === "fehler" ? "⚠️ Fehler bei" : "👉 Nächster Schritt:"}
    <b>${STEP_LABELS[next]}</b>
    <span class="muted">– ${STEP_HINWEISE[next]}</span>
    <button class="primary">▶ Jetzt ausführen</button>
    <button title="Ab diesem Schritt alles Restliche durchrechnen">⏩ bis zum Ende</button>`;
  const [b1, b2] = el.querySelectorAll("button");
  b1.onclick = () => runStep(next);
  b2.onclick = () => startJob(`/api/projects/${currentProject}/run_all`,
    { ...cutBody(), ab_schritt: next });
}

function renderStatusChain() {
  // Sektions-Lampen spiegeln den Schritt-Status
  document.querySelectorAll("h3 .lamp[data-step]").forEach(el => {
    const st = overview.status[el.dataset.step];
    el.className = `lamp ${st ? st.status : "offen"}`;
    el.title = st?.detail || "";
  });
  const chain = $("#status-chain");
  chain.innerHTML = "";
  for (const [step, label] of Object.entries(STEP_LABELS)) {
    const st = overview.status[step] || { status: "offen", detail: "" };
    const div = document.createElement("div");
    div.className = `step ${st.status}`;
    div.title = STEP_HINWEISE[step] +
      (st.detail ? `\n\nLetzter Lauf: ${st.detail}` : "");
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
        let vfr = "";
        if (info?.vfr_original) {
          vfr = ` <span class="muted" title="Original hat variable Framerate; für Vorschau und Export wird die automatisch erzeugte CFR-Kopie verwendet">✓ VFR→CFR</span>`;
        } else if (info?.cfr_fehler) {
          vfr = ` <span title="Variable Framerate erkannt, Wandlung nach CFR fehlgeschlagen – Ingest neu ausführen. ${info.cfr_fehler.replace(/"/g, "'")}">⚠️ VFR</span>`;
        } else if (info?.breite && role !== "audio_dji") {
          vfr = ` <button class="mini-cfr" data-rel="input/${role}/${f.name}"
            title="Datei manuell nach konstanter Framerate wandeln – falls das Bild dieser Kamera trotz allem gegen den Ton driftet">→ CFR</button>`;
        }
        const extra = info
          ? ` <span class="muted">${info.dauer.toFixed(1)}s` +
            (info.breite ? `, ${info.breite}×${info.hoehe}@${(info.fps || 0).toFixed(2)}` : "") +
            `</span>`
          : "";
        return `<li>${f.name}${extra}${vfr}</li>`;
      }).join("") + "</ul>";
    div.appendChild(box);
  }
  div.querySelectorAll(".mini-cfr").forEach(btn => {
    btn.onclick = () => startJob(`/api/projects/${currentProject}/cfr`,
      { relpfad: btn.dataset.rel });
  });
}

let skriptGespeichert = null;   // letzter gespeicherter Stand
let skriptTimer = null;

function renderSkript() {
  const hint = $("#skript-hint");
  const ta = $("#cut-script");
  const sd = overview.skript_datei;
  if (sd) {
    if (!ta.value) {
      ta.value = sd.text;
      skriptGespeichert = sd.text;
    }
    hint.classList.remove("hidden");
    hint.innerHTML = `📄 Gespeichert in <b>${sd.datei}</b> – Änderungen `
      + `werden automatisch übernommen und gelten auch für die `
      + `Warteschlange.`;
  } else {
    skriptGespeichert = skriptGespeichert ?? "";
    hint.classList.add("hidden");
  }
}

async function saveSkript() {
  const ta = $("#cut-script");
  if (!ta || ta.value === skriptGespeichert) return;
  try {
    const r = await api(`/api/projects/${currentProject}/script`, {
      method: "PUT", body: JSON.stringify({ text: ta.value }) });
    skriptGespeichert = ta.value;
    overview.skript_datei = r.skript_datei;
    const hint = $("#skript-hint");
    hint.classList.remove("hidden");
    hint.innerHTML = r.skript_datei
      ? `💾 Gespeichert in <b>${r.skript_datei.datei}</b> – bleibt auch `
        + `nach einem Neustart erhalten und gilt für die Warteschlange.`
      : "💾 Skript gelöscht – die Auswahl läuft wieder vollautomatisch.";
  } catch (e) { /* nächster Tastendruck versucht es erneut */ }
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
      <td><input class="off" type="number" step="0.001" style="width:8em"
           value="${o.offset_sekunden.toFixed(3)}"></td>
      <td><input class="vkorr" type="number" step="0.04" style="width:6em"
           value="${(o.video_korrektur_sekunden || 0).toFixed(3)}"></td>
      <td>${konf}</td><td class="muted">${o.hinweis || ""}${drift}</td>`;
    const send = async body => {
      try {
        await api(`/api/projects/${currentProject}/sync/offsets`, {
          method: "PUT", body: JSON.stringify({ relpfad: datei, ...body }),
        });
        refreshOverview();
      } catch (e) { alert(e.message); }
    };
    $(".off", tr).onchange = e =>
      send({ offset_sekunden: Number(e.target.value) });
    $(".vkorr", tr).onchange = e =>
      send({ video_korrektur_sekunden: Number(e.target.value) });
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

let pollingJobId = null;

function fmtLaufzeit(sek) {
  const s = Math.max(0, Math.round(sek));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} min`;
}

async function pollJob(job, onDone, elSel = "#job-status") {
  const el = $(elSel);
  if (!el) return;
  pollingJobId = job.id;
  const isBatch = job.name.startsWith("batch:");
  let tick = 0;
  el.className = "job";
  el.innerHTML = `<div class="head"><b>${isBatch ? "Warteschlange"
        : job.name.split(":").pop()}</b>
      <span class="pct">0 %</span> · <span class="elapsed muted">0:00 min</span>
      <button class="cancel" title="Job abbrechen – danach kannst du anpassen und neu starten">✖</button></div>
    <div class="bar"><div></div></div>
    <div class="msg muted"></div>`;
  $(".cancel", el).onclick = async () => {
    try { await api(`/api/jobs/${job.id}/cancel`, { method: "POST" }); }
    catch (e) { /* Job evtl. schon fertig */ }
  };
  const timer = setInterval(async () => {
    try {
      const j = await api(`/api/jobs/${job.id}`);
      $(".msg", el).textContent = j.meldung || j.status;
      $(".pct", el).textContent = `${Math.round(j.fortschritt * 100)} %`;
      $(".elapsed", el).textContent = fmtLaufzeit(j.laufzeit_sekunden || 0);
      $(".bar > div", el).style.width = `${j.fortschritt * 100}%`;
      // Während der Warteschlange die Projektliste (x/8) mitziehen
      if (isBatch && ++tick % 8 === 0) loadProjects();
      if (j.status !== "laeuft") {
        clearInterval(timer);
        pollingJobId = null;
        $(".cancel", el)?.remove();
        if (j.status === "fehler") {
          el.classList.add("fehler");
          $(".msg", el).textContent = j.fehler;
        } else if (j.status === "abgebrochen") {
          $(".msg", el).textContent =
            "⏹ abgebrochen – anpassen und neu starten";
        } else {
          $(".pct", el).textContent = "100 %";
          $(".bar > div", el).style.width = "100%";
          $(".msg", el).textContent =
            `${j.meldung || "fertig"} (${fmtLaufzeit(j.laufzeit_sekunden || 0)})`;
          if (onDone) onDone(j);
        }
        loadProjects();
        if (currentProject) refreshOverview();
      }
    } catch (e) { clearInterval(timer); pollingJobId = null; }
  }, 800);
}

async function attachRunningJob() {
  // Nach einem Seiten-Reload wieder am laufenden Job andocken
  try {
    const r = await api(`/api/projects/${currentProject}/jobs/running`);
    if (r.job && r.job.id !== pollingJobId) pollJob(r.job);
  } catch (e) { /* egal */ }
}

async function attachRunningBatch() {
  try {
    const r = await api("/api/batch/running");
    if (r.job) {
      $("#batch-status").classList.remove("hidden");
      pollJob(r.job, null, "#batch-status");
    }
  } catch (e) { /* egal */ }
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
  // Skript-Feld ausgefüllt -> Leitfaden-Modus, sonst vollautomatisch
  const skript = $("#cut-script").value.trim();
  return skript ? { modus: "skript", skript } : { modus: "auto", skript: null };
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
      pausen_schnitt_sek: Number(form.elements.pausen_schnitt_sek.value),
      sprache: form.elements.sprache.value,
      export_format: form.elements.export_format.value,
      musik_aktiv: form.elements.musik_aktiv.checked,
      untertitel_aktiv: form.elements.untertitel_aktiv.checked,
      claude_modell: form.elements.claude_modell.value,
      whisper_modell: form.elements.whisper_modell.value,
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

  $("#cut-script").oninput = () => {
    clearTimeout(skriptTimer);
    skriptTimer = setTimeout(saveSkript, 800);
  };
  $("#cut-script").onblur = saveSkript;
  $("#btn-cut").onclick = async () => { await saveSkript(); runStep("schnitt"); };
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

$("#btn-delete-projects").onclick = async () => {
  const projekte = [...batchSelection];
  if (!projekte.length) {
    return alert("Zuerst die Projekte ankreuzen, die gelöscht werden sollen.");
  }
  const liste = projekte.map(p => `  – ${p}`).join("\n");
  if (!confirm(
      `${projekte.length} Projekt(e) WIRKLICH löschen?\n\n${liste}\n\n` +
      "Der komplette Projektordner wird unwiderruflich gelöscht – " +
      "inklusive aller Dateien in input/ und aller Ergebnisse!")) {
    return;
  }
  for (const p of projekte) {
    try {
      await api(`/api/projects/${p}`, { method: "DELETE" });
      batchSelection.delete(p);
      if (currentProject === p) {
        currentProject = null;
        $("#main").innerHTML = `<p class="muted">Projekt links auswählen
          oder neu anlegen.</p>`;
      }
    } catch (e) { alert(`${p}: ${e.message}`); }
  }
  loadProjects();
};

$("#btn-batch").onclick = async () => {
  const projekte = [...batchSelection];
  if (!projekte.length) {
    return alert("Zuerst oben Projekte ankreuzen, die abgearbeitet werden sollen.");
  }
  try {
    const job = await api("/api/batch", { method: "POST",
      body: JSON.stringify({ projekte, fortsetzen: true }) });
    $("#batch-status").classList.remove("hidden");
    pollJob(job, null, "#batch-status");
  } catch (e) { alert(e.message); }
};

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
  attachRunningBatch();
})();
