/* PosturoSPS – Application SPA */
'use strict';

// =====================================================================
// STATE
// =====================================================================
const State = {
  page: 'home',
  status: { cop_x_cm: 0, cop_y_cm: 0, cmd: 0, total: 0, tare_ready: false, offset_ready: false, send_to_esp: false },
  currentPatient: null,
  patients: [],
  sessions: [],
  presets: [],
  sotCondition: 0,     // 0=idle, 1-6=running
  sotTimer: null,
  sotElapsed: 0,
  sotDuration: 0,
  sotRunning: false,
  currentEx: 'ex1',
  exRunning: false,
  exSession: null,      // active session log
  exScore: {},
  videoList: [],
  statusPoll: null,
};

// =====================================================================
// EXERCISES LIBRARY
// =====================================================================
const EXERCISES = [
  { id: 'ex1',  name: 'Plateforme fixe / asservie', cat: 'Plateforme',   icon: '⚖️',  preset_ok: true },
  { id: 'ex2',  name: 'Sinusoïde',                  cat: 'Mouvement',    icon: '〰️',  preset_ok: true },
  { id: 'ex3',  name: 'Petit-Grand-Petit',           cat: 'Mouvement',    icon: '📈',  preset_ok: true },
  { id: 'ex4',  name: 'Impulsions aléatoires',       cat: 'Réactivité',   icon: '⚡',  preset_ok: true },
  { id: 'ex5',  name: 'Points VOR',                  cat: 'Vestibulaire', icon: '👁️',  preset_ok: true },
  { id: 'ex6',  name: 'Point mobile',                cat: 'Vestibulaire', icon: '🎯',  preset_ok: true },
  { id: 'ex7',  name: 'Citations / Dual-task',       cat: 'Cognitif',     icon: '💬',  preset_ok: true },
  { id: 'ex8',  name: 'Cible COP',                   cat: 'COP actif',    icon: '🎯',  preset_ok: true },
  { id: 'ex9',  name: 'Cibles séquentielles',        cat: 'COP actif',    icon: '🔢',  preset_ok: true },
  { id: 'ex10', name: 'Parcours',                    cat: 'COP actif',    icon: '🛤️',  preset_ok: true },
  { id: 'ex11', name: 'Labyrinthe',                  cat: 'COP actif',    icon: '🌀',  preset_ok: true },
  { id: 'ex12', name: 'Plateforme + Vidéo',          cat: 'Distraction',  icon: '🎬',  preset_ok: true },
];

const SOT_CONDITIONS = {
  1: { name: 'EO STABLE',      duration: 20, icon: '👁️' },
  2: { name: 'EC STABLE',      duration: 20, icon: '🚫' },
  3: { name: 'EO OPTO',        duration: 35, icon: '🌀' },
  4: { name: 'EO INSTABLE',    duration: 20, icon: '⚖️' },
  5: { name: 'EC INSTABLE',    duration: 20, icon: '⚠️' },
  6: { name: 'OPTO INSTABLE',  duration: 35, icon: '🌀⚖️' },
};

const DEFAULT_PRESETS = [
  { id: 'vest',     name: 'Vestibulaire',      icon: '🌀', color: 'vest',
    desc: 'VOR + cible + opto 12 min',
    sequence: [
      { ex: 'ex5', duration: 120, params: { platform: 'fixed', vor_mode: 'lr', vor_interval: 5 } },
      { ex: 'ex8', duration: 120, params: { platform: 'fixed', target_mode: 'random', difficulty: 'medium' } },
    ]
  },
  { id: 'proprio',  name: 'Proprioception',    icon: '⚖️', color: 'proprio',
    desc: 'Sinus + impulsions 15 min',
    sequence: [
      { ex: 'ex2', duration: 120, params: { amplitude: 'low', speed: 'low' } },
      { ex: 'ex4', duration: 150, params: { amplitude: 'medium', speed: 'medium' } },
      { ex: 'ex3', duration: 90,  params: { amplitude: 'medium', speed: 'medium' } },
    ]
  },
  { id: 'dual',     name: 'Double tâche',       icon: '🧠', color: 'dual',
    desc: 'Citation + COP 12 min',
    sequence: [
      { ex: 'ex7', duration: 120, params: { platform: 'sinus', amplitude: 'low', speed: 'low' } },
      { ex: 'ex9', duration: 120, params: { platform: 'fixed', sequence: 'cross', difficulty: 'medium' } },
    ]
  },
  { id: 'senior',   name: 'Senior sécurisée',  icon: '🤝', color: 'senior',
    desc: 'Doux et progressif 10 min',
    sequence: [
      { ex: 'ex1', duration: 60,  params: { platform: 'fixed' } },
      { ex: 'ex6', duration: 120, params: { platform: 'fixed', point_mode: 'lr', point_speed: 'low' } },
      { ex: 'ex8', duration: 120, params: { platform: 'fixed', difficulty: 'low' } },
    ]
  },
  { id: 'sport',    name: 'Retour sport',       icon: '🏃', color: 'sport',
    desc: 'Dynamique et réactif 20 min',
    sequence: [
      { ex: 'ex4', duration: 120, params: { amplitude: 'high', speed: 'high' } },
      { ex: 'ex11',duration: 180, params: { platform: 'sinus', difficulty: 'high' } },
      { ex: 'ex10',duration: 120, params: { platform: 'auto', difficulty: 'high' } },
    ]
  },
  { id: 'cervical', name: 'Cervical',           icon: '🔄', color: 'cervical',
    desc: 'VOR + parcours 15 min',
    sequence: [
      { ex: 'ex5', duration: 120, params: { platform: 'fixed', vor_mode: 'random' } },
      { ex: 'ex10',duration: 120, params: { platform: 'fixed', path: 'infinity' } },
    ]
  },
];

// =====================================================================
// API HELPERS
// =====================================================================
async function api(url, opts = {}) {
  try {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const ct = r.headers.get('content-type') || '';
    return ct.includes('json') ? r.json() : r.text();
  } catch (e) {
    console.warn('API error:', url, e);
    return null;
  }
}

// =====================================================================
// TOAST
// =====================================================================
let _toastTimer = null;
function toast(msg, type = 'ok', dur = 2500) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `show ${type}`;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.className = ''; }, dur);
}

// =====================================================================
// NAVIGATION
// =====================================================================
function navigate(page, opts = {}) {
  // Hide all pages
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));

  const pageEl = document.getElementById(`page-${page}`);
  if (!pageEl) return;
  pageEl.classList.add('active');

  const tabBtn = document.getElementById(`tab-${page}`);
  if (tabBtn) tabBtn.classList.add('active');

  State.page = page;

  // Update hash for "back" support
  history.pushState({ page }, '', `?page=${page}`);

  // Page-specific init
  if (page === 'home') renderHome();
  if (page === 'sot') renderSOT();
  if (page === 'exercises') renderExercises(opts);
  if (page === 'patients') renderPatients();
  if (page === 'more') renderMore();
}

// =====================================================================
// STATUS POLLING
// =====================================================================
function startStatusPoll() {
  if (State.statusPoll) return;
  State.statusPoll = setInterval(async () => {
    const s = await api('/status');
    if (!s) return;
    State.status = s;
    updateStatusBar(s);
    if (State.page === 'exercises') updateExLive(s);
    if (State.page === 'sot') updateSOTLive(s);
  }, 300);
}

function updateStatusBar(s) {
  const dot = document.getElementById('status-dot');
  const lbl = document.getElementById('status-label');
  if (!dot || !lbl) return;
  const ready = s.tare_ready && s.offset_ready;
  dot.className = 'status-dot' + (ready ? ' ok' : '');
  lbl.textContent = ready ? `COP: ${s.cop_x_cm.toFixed(1)}, ${s.cop_y_cm.toFixed(1)}` : 'Non initialisé';
}

// =====================================================================
// HOME PAGE
// =====================================================================
function renderHome() {
  const p = document.getElementById('page-home');
  const s = State.status;
  const ready = s.tare_ready && s.offset_ready;

  // Update welcome text
  const patEl = document.getElementById('home-patient');
  if (patEl) {
    patEl.textContent = State.currentPatient
      ? `Patient: ${State.currentPatient.prenom} ${State.currentPatient.nom}`
      : 'Aucun patient sélectionné';
  }

  // Platform status indicators
  const tareSt = document.getElementById('home-tare-st');
  const centerSt = document.getElementById('home-center-st');
  if (tareSt) tareSt.className = 'badge ' + (s.tare_ready ? 'badge-green' : 'badge-red');
  if (tareSt) tareSt.textContent = s.tare_ready ? 'OK' : 'EN ATTENTE';
  if (centerSt) centerSt.className = 'badge ' + (s.offset_ready ? 'badge-green' : 'badge-red');
  if (centerSt) centerSt.textContent = s.offset_ready ? 'OK' : 'EN ATTENTE';
}

// =====================================================================
// TARE / CENTER (shared)
// =====================================================================
async function doTare() {
  const btn = document.getElementById('btn-tare');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Tare...'; }
  toast('Tare en cours...', 'ok', 5000);
  await api('/tare');
  toast('✅ Tare OK', 'ok');
  if (btn) { btn.disabled = false; btn.textContent = '1) TARE'; }
  const s = await api('/status');
  if (s) State.status = s;
  renderHome();
}

async function doCenter() {
  const btn = document.getElementById('btn-center');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Centrage...'; }
  toast('Centrage + HOME en cours (10s)...', 'ok', 12000);
  const r = await api('/exercices/center');
  if (typeof r === 'string' && r.includes('ERROR')) {
    toast('❌ Pas de charge détectée', 'err');
    if (btn) { btn.disabled = false; btn.textContent = '2) SET CENTER'; }
    return;
  }
  toast('✅ Centrage OK', 'ok');
  if (btn) { btn.disabled = false; btn.textContent = '2) SET CENTER'; }
  const s = await api('/status');
  if (s) State.status = s;
  renderHome();
}

// =====================================================================
// SOT PAGE
// =====================================================================
let sotTimerInterval = null;

function renderSOT() {
  renderSOTConditions();
  updateSOTButtons();
}

function renderSOTConditions() {
  const grid = document.getElementById('sot-cond-grid');
  if (!grid) return;
  grid.innerHTML = '';
  const done = State.sotCondition > 6 ? 6 : State.sotCondition - 1;
  for (let c = 1; c <= 6; c++) {
    const cond = SOT_CONDITIONS[c];
    const isActive = c === State.sotCondition && State.sotRunning;
    const isDone = c < State.sotCondition || (State.sotCondition > 6 && c <= 6);
    const div = document.createElement('div');
    div.className = `sot-cond ${isActive ? 'active' : ''} ${isDone ? 'done' : ''}`;
    div.innerHTML = `<div class="sot-cond-num">${cond.icon}</div>
      <div style="font-size:13px;font-weight:700;">C${c}</div>
      <div class="sot-cond-name">${cond.name}</div>`;
    grid.appendChild(div);
  }
}

function updateSOTButtons() {
  const btnStart = document.getElementById('sot-btn-start');
  const btnNext  = document.getElementById('sot-btn-next');
  const btnStop  = document.getElementById('sot-btn-stop');
  const btnReset = document.getElementById('sot-btn-reset');
  const btnReport = document.getElementById('sot-btn-report');
  const condNum   = document.getElementById('sot-cond-num-label');

  const idle = State.sotCondition === 0;
  const done = State.sotCondition > 6;

  if (btnStart) btnStart.style.display = idle ? '' : 'none';
  if (btnNext)  btnNext.style.display  = (!idle && !done && !State.sotRunning) ? '' : 'none';
  if (btnStop)  btnStop.style.display  = (!idle && !done) ? '' : 'none';
  if (btnReset) btnReset.style.display = (!idle) ? '' : 'none';
  if (btnReport) btnReport.style.display = done ? '' : 'none';
  if (condNum) {
    if (idle) condNum.textContent = 'Prêt';
    else if (done) condNum.textContent = 'Terminé ✅';
    else condNum.textContent = `Condition ${State.sotCondition}/6`;
  }
}

function updateSOTLive(s) {
  const xEl = document.getElementById('sot-cop-x');
  const yEl = document.getElementById('sot-cop-y');
  if (xEl) xEl.textContent = s.cop_x_cm.toFixed(2);
  if (yEl) yEl.textContent = s.cop_y_cm.toFixed(2);
  drawCOPMini('sot-cop-canvas', s.cop_x_cm, s.cop_y_cm);
}

async function sotStartFirst() {
  // Ensure tare + center first
  if (!State.status.tare_ready) { toast('❌ Tare non faite', 'err'); return; }
  if (!State.status.offset_ready) { toast('❌ Centrage non fait', 'err'); return; }
  State.sotCondition = 1;
  State.sotRunning = true;
  const cond = SOT_CONDITIONS[1];
  startSOTTimer(cond.duration);
  await api('/sot/start/1');
  renderSOTConditions();
  updateSOTButtons();
  toast(`Condition 1 démarrée – ${cond.name}`, 'ok');
}

async function sotNext() {
  stopSOTTimer();
  const r = await api('/sot/next');
  if (typeof r === 'string' && r.includes('FINISHED')) {
    State.sotCondition = 7;
    State.sotRunning = false;
    renderSOTConditions();
    updateSOTButtons();
    toast('✅ SOT terminé – rapport disponible', 'ok', 4000);
    return;
  }
  State.sotCondition++;
  State.sotRunning = true;
  const cond = SOT_CONDITIONS[State.sotCondition];
  if (cond) startSOTTimer(cond.duration);
  renderSOTConditions();
  updateSOTButtons();
  toast(`Condition ${State.sotCondition} – ${cond ? cond.name : ''}`, 'ok');
}

async function sotStop() {
  stopSOTTimer();
  State.sotRunning = false;
  await api('/sot/stop');
  State.sotCondition = 7;
  renderSOTConditions();
  updateSOTButtons();
  toast('SOT arrêté – analyse en cours...', 'ok', 4000);
}

async function sotReset() {
  stopSOTTimer();
  State.sotCondition = 0;
  State.sotRunning = false;
  await api('/sot/stop');
  renderSOTConditions();
  updateSOTButtons();
  document.getElementById('sot-timer').textContent = '00:00';
  document.getElementById('sot-timer').className = '';
  document.getElementById('sot-progress-bar').style.width = '0%';
  toast('SOT réinitialisé', 'ok');
}

async function sotRestart() {
  if (State.sotCondition < 1 || State.sotCondition > 6) return;
  stopSOTTimer();
  State.sotRunning = true;
  const cond = SOT_CONDITIONS[State.sotCondition];
  startSOTTimer(cond.duration);
  await api('/sot/restart');
  toast(`Relance condition ${State.sotCondition}`, 'ok');
}

function startSOTTimer(duration) {
  State.sotElapsed = 0;
  State.sotDuration = duration;
  const timerEl = document.getElementById('sot-timer');
  const barEl = document.getElementById('sot-progress-bar');
  if (timerEl) { timerEl.textContent = fmtTime(0); timerEl.className = ''; }
  if (barEl) barEl.style.width = '0%';

  sotTimerInterval = setInterval(() => {
    State.sotElapsed++;
    const rem = Math.max(0, duration - State.sotElapsed);
    if (timerEl) timerEl.textContent = fmtTime(rem);
    if (barEl) barEl.style.width = Math.min(100, (State.sotElapsed / duration * 100)) + '%';
    if (State.sotElapsed >= duration) {
      if (timerEl) timerEl.className = 'done';
      stopSOTTimer();
      toast('✅ Condition terminée – appuyez SUIVANT', 'ok', 5000);
    }
  }, 1000);
}

function stopSOTTimer() {
  clearInterval(sotTimerInterval);
  sotTimerInterval = null;
}

function fmtTime(s) {
  const m = Math.floor(s / 60);
  const ss = s % 60;
  return String(m).padStart(2, '0') + ':' + String(ss).padStart(2, '0');
}

// =====================================================================
// EXERCISES PAGE
// =====================================================================
let exFilterCat = 'all';

function renderExercises(opts = {}) {
  renderExLibrary();
  renderPresets();
  loadVideoList();
  pollExStatus();
}

function renderExLibrary() {
  const grid = document.getElementById('ex-library-grid');
  if (!grid) return;
  grid.innerHTML = '';
  const cats = ['all', ...new Set(EXERCISES.map(e => e.cat))];
  const filt = State.exFilter || 'all';

  EXERCISES.forEach(ex => {
    if (filt !== 'all' && ex.cat !== filt) return;
    const div = document.createElement('div');
    div.className = 'ex-card' + (State.currentEx === ex.id ? ' selected' : '');
    div.innerHTML = `
      <div class="ex-num">${ex.icon} ${ex.id.toUpperCase()}</div>
      <div class="ex-name">${ex.name}</div>
      <div class="ex-cat">${ex.cat}</div>`;
    div.onclick = () => selectExercise(ex.id);
    grid.appendChild(div);
  });
}

function renderExFilterTabs() {
  const container = document.getElementById('ex-filter-tabs');
  if (!container) return;
  const cats = ['all', ...new Set(EXERCISES.map(e => e.cat))];
  container.innerHTML = '';
  cats.forEach(cat => {
    const btn = document.createElement('button');
    btn.className = 'btn btn-sm ' + ((State.exFilter || 'all') === cat ? 'btn-blue' : 'btn-ghost');
    btn.textContent = cat === 'all' ? 'Tous' : cat;
    btn.style.flexShrink = '0';
    btn.onclick = () => { State.exFilter = cat; renderExLibrary(); renderExFilterTabs(); };
    container.appendChild(btn);
  });
}

function selectExercise(exId) {
  State.currentEx = exId;
  renderExLibrary();
  showExConfig(exId);
}

function showExConfig(exId) {
  const panel = document.getElementById('ex-config-panel');
  if (!panel) return;

  const ex = EXERCISES.find(e => e.id === exId);
  if (!ex) return;

  // Build config form based on exercise
  panel.innerHTML = buildExConfigHTML(exId, ex);
  panel.classList.remove('hidden');

  // Post-render hooks
  if (exId === 'ex12') loadVideoListIntoSelect();
  syncPlatformMotionBox(exId);
}

function buildExConfigHTML(exId, ex) {
  const motionOptions = `
    <div class="field">
      <label>Amplitude plateforme</label>
      <select id="cfg-amplitude">
        <option value="low">Léger</option>
        <option value="medium" selected>Moyen</option>
        <option value="high">Fort</option>
      </select>
    </div>
    <div class="field">
      <label>Vitesse plateforme</label>
      <select id="cfg-speed">
        <option value="low">Lent</option>
        <option value="medium" selected>Moyen</option>
        <option value="high">Rapide</option>
      </select>
    </div>`;

  const platformField = `
    <div class="field">
      <label>Mode plateforme</label>
      <select id="cfg-platform" onchange="syncPlatformMotionBox('${exId}')">
        <option value="fixed">Fixe</option>
        <option value="auto">Asservie (COP)</option>
        <option value="sinus">Sinus</option>
        <option value="ramp">Petit-Grand-Petit</option>
        <option value="impulses">Impulsions</option>
      </select>
    </div>
    <div id="motion-box" class="hidden">${motionOptions}</div>`;

  const difficultyField = `
    <div class="field">
      <label>Difficulté</label>
      <select id="cfg-difficulty">
        <option value="low">Facile</option>
        <option value="medium" selected>Moyen</option>
        <option value="high">Difficile</option>
      </select>
    </div>`;

  let specific = '';

  if (exId === 'ex1') {
    specific = `
      <div class="field">
        <label>Mode plateforme</label>
        <select id="cfg-platform">
          <option value="fixed">Fixe</option>
          <option value="auto">Asservie (COP)</option>
        </select>
      </div>
      <div class="field">
        <label>Écran HDMI</label>
        <select id="cfg-screen">
          <option value="black">Noir</option>
          <option value="opto">Optocinetique</option>
        </select>
      </div>`;
  } else if (exId === 'ex2') {
    specific = `
      <div class="field"><label>Amplitude</label>
        <select id="cfg-amplitude"><option value="low">Léger</option><option value="medium" selected>Moyen</option><option value="high">Fort</option></select>
      </div>
      <div class="field"><label>Vitesse</label>
        <select id="cfg-speed"><option value="low">Lent</option><option value="medium" selected>Moyen</option><option value="high">Rapide</option></select>
      </div>
      <div class="field"><label>Écran HDMI</label>
        <select id="cfg-screen"><option value="black">Noir</option><option value="opto">Optocinetique</option></select>
      </div>`;
  } else if (exId === 'ex3') {
    specific = `
      <div class="field"><label>Amplitude</label>
        <select id="cfg-amplitude"><option value="low">Léger</option><option value="medium" selected>Moyen</option><option value="high">Fort</option></select>
      </div>
      <div class="field"><label>Vitesse</label>
        <select id="cfg-speed"><option value="low">Lent</option><option value="medium" selected>Moyen</option><option value="high">Rapide</option></select>
      </div>
      <div class="field"><label>Durée (s)</label>
        <input id="cfg-duration" type="number" min="5" max="120" value="30"/>
      </div>`;
  } else if (exId === 'ex4') {
    specific = `
      <div class="field"><label>Amplitude</label>
        <select id="cfg-amplitude"><option value="low">Léger</option><option value="medium" selected>Moyen</option><option value="high">Fort</option></select>
      </div>
      <div class="field"><label>Vitesse</label>
        <select id="cfg-speed"><option value="low">Lent</option><option value="medium" selected>Moyen</option><option value="high">Rapide</option></select>
      </div>
      <div class="field"><label>Délai min / max (s)</label>
        <div class="flex gap-8">
          <input id="cfg-gap-min" type="number" step="0.1" min="0.2" max="20" value="1.5" style="width:50%"/>
          <input id="cfg-gap-max" type="number" step="0.1" min="0.2" max="20" value="4.0" style="width:50%"/>
        </div>
      </div>
      <div class="field"><label>Durée impulsion (ms)</label>
        <input id="cfg-pulse-ms" type="number" min="200" max="3000" value="900"/>
      </div>`;
  } else if (exId === 'ex5') {
    specific = platformField + `
      <div class="field"><label>Mode points VOR</label>
        <select id="cfg-vor-mode"><option value="lr">Gauche/Droite</option><option value="ud">Haut/Bas</option><option value="diag1">Diag 1</option><option value="diag2">Diag 2</option><option value="random">Aléatoire</option></select>
      </div>
      <div class="field"><label>Temps par paire</label>
        <select id="cfg-vor-interval"><option value="2">2s</option><option value="5">5s</option><option value="10">10s</option></select>
      </div>`;
  } else if (exId === 'ex6') {
    specific = platformField + `
      <div class="field"><label>Trajectoire point</label>
        <select id="cfg-point-mode"><option value="lr">Gauche-Droite</option><option value="ud">Haut-Bas</option><option value="circle">Cercle</option><option value="infinity">Huit couché</option></select>
      </div>
      <div class="field"><label>Vitesse point</label>
        <select id="cfg-point-speed"><option value="low">Lent</option><option value="medium" selected>Moyen</option><option value="high">Rapide</option></select>
      </div>`;
  } else if (exId === 'ex7') {
    specific = platformField + `
      <div class="field"><label>Intervalle citations</label>
        <select id="cfg-interval"><option value="2">2s</option><option value="5" selected>5s</option><option value="10">10s</option></select>
      </div>`;
  } else if (exId === 'ex8') {
    specific = platformField + `
      <div class="field"><label>Mode cible</label>
        <select id="cfg-target-mode" onchange="syncEx8Target()"><option value="single">Cible fixe</option><option value="random">Cibles aléatoires</option></select>
      </div>
      <div id="ex8-target-box" class="field">
        <label>Cible (fixe)</label>
        <select id="cfg-target"><option value="center">Centre</option><option value="front">Avant</option><option value="back">Arrière</option><option value="left">Gauche</option><option value="right">Droite</option></select>
      </div>
      ${difficultyField}`;
  } else if (exId === 'ex9') {
    specific = platformField + `
      <div class="field"><label>Séquence</label>
        <select id="cfg-sequence"><option value="cross">Croix</option><option value="square">Carré</option><option value="star">Étoile</option></select>
      </div>
      ${difficultyField}`;
  } else if (exId === 'ex10') {
    specific = platformField + `
      <div class="field"><label>Parcours</label>
        <select id="cfg-path"><option value="infinity">Huit couché</option><option value="circle">Cercle</option><option value="square">Carré</option></select>
      </div>
      ${difficultyField}`;
  } else if (exId === 'ex11') {
    specific = platformField + difficultyField;
  } else if (exId === 'ex12') {
    specific = platformField + `
      <div class="field"><label>Vidéo</label>
        <select id="cfg-video-on"><option value="on">Vidéo ON</option><option value="off">Noir</option></select>
      </div>
      <div class="field"><label>Mode vidéo</label>
        <select id="cfg-video-mode"><option value="single">Fichier unique</option><option value="playlist">Playlist auto</option></select>
      </div>
      <div class="field"><label>Fichier vidéo</label>
        <select id="cfg-video-file"><option value="voiture1.mp4">voiture1.mp4</option></select>
      </div>
      <div class="field"><label>Intervalle playlist (s)</label>
        <input id="cfg-video-interval" type="number" min="5" max="300" value="20"/>
      </div>`;
  }

  return `
    <div class="card">
      <div class="flex items-center justify-between mb-12">
        <div>
          <div class="card-big-title">${ex.icon} ${ex.name}</div>
          <div class="text-muted text-sm">${ex.cat}</div>
        </div>
        <button class="btn btn-ghost btn-sm" onclick="hideExConfig()">✕</button>
      </div>
      <div class="divider"></div>
      ${specific}
      <div class="divider"></div>
      <div class="flex gap-8">
        <button class="btn btn-green btn-full btn-lg" onclick="startEx()" id="btn-ex-start"
          ${(!State.status.tare_ready || !State.status.offset_ready) ? 'disabled' : ''}>
          ▶ DÉMARRER
        </button>
        <button class="btn btn-red btn-lg" onclick="stopEx()">■ STOP</button>
      </div>
      <div id="ex-status-ribbon" class="status-ribbon stopped mt-12">
        <div class="sr-dot"></div>
        <div id="ex-status-text">Prêt</div>
      </div>
    </div>`;
}

function hideExConfig() {
  const panel = document.getElementById('ex-config-panel');
  if (panel) panel.classList.add('hidden');
}

function syncPlatformMotionBox(exId) {
  const sel = document.getElementById('cfg-platform');
  const box = document.getElementById('motion-box');
  if (!sel || !box) return;
  const dynamic = ['sinus', 'ramp', 'impulses'].includes(sel.value);
  box.classList.toggle('hidden', !dynamic);
}

function syncEx8Target() {
  const mode = document.getElementById('cfg-target-mode');
  const box = document.getElementById('ex8-target-box');
  if (!mode || !box) return;
  box.classList.toggle('hidden', mode.value !== 'single');
}

async function loadVideoList() {
  const r = await api('/videos/list');
  if (r && r.videos) State.videoList = r.videos;
}

async function loadVideoListIntoSelect() {
  if (!State.videoList.length) await loadVideoList();
  const sel = document.getElementById('cfg-video-file');
  if (!sel) return;
  sel.innerHTML = '';
  const list = State.videoList.length ? State.videoList : ['voiture1.mp4'];
  list.forEach(v => {
    const o = document.createElement('option');
    o.value = v; o.textContent = v;
    sel.appendChild(o);
  });
}

function getExParams(exId) {
  const g = id => { const el = document.getElementById(id); return el ? el.value : null; };
  const gn = id => { const el = document.getElementById(id); return el ? el.value : null; };

  const p = {};
  if (g('cfg-platform')) p.platform = g('cfg-platform');
  if (g('cfg-amplitude')) p.amplitude = g('cfg-amplitude');
  if (g('cfg-speed')) p.speed = g('cfg-speed');
  if (g('cfg-screen')) p.screen = g('cfg-screen');
  if (g('cfg-duration')) p.duration = g('cfg-duration');
  if (g('cfg-gap-min')) p.gap_min = g('cfg-gap-min');
  if (g('cfg-gap-max')) p.gap_max = g('cfg-gap-max');
  if (g('cfg-pulse-ms')) p.pulse_ms = g('cfg-pulse-ms');
  if (g('cfg-vor-mode')) p.vor_mode = g('cfg-vor-mode');
  if (g('cfg-vor-interval')) p.vor_interval = g('cfg-vor-interval');
  if (g('cfg-point-mode')) p.point_mode = g('cfg-point-mode');
  if (g('cfg-point-speed')) p.point_speed = g('cfg-point-speed');
  if (g('cfg-interval')) p.interval = g('cfg-interval');
  if (g('cfg-target-mode')) p.target_mode = g('cfg-target-mode');
  if (g('cfg-target')) p.target = g('cfg-target');
  if (g('cfg-difficulty')) p.difficulty = g('cfg-difficulty');
  if (g('cfg-sequence')) p.sequence = g('cfg-sequence');
  if (g('cfg-path')) p.path = g('cfg-path');
  if (g('cfg-video-on')) p.video_on = g('cfg-video-on');
  if (g('cfg-video-mode')) p.video_mode = g('cfg-video-mode');
  if (g('cfg-video-file')) p.video_file = g('cfg-video-file');
  if (g('cfg-video-interval')) p.video_interval = g('cfg-video-interval');
  return p;
}

async function startEx() {
  if (!State.status.tare_ready || !State.status.offset_ready) {
    toast('❌ Effectuer Tare + Centrage d\'abord', 'err');
    return;
  }
  const exId = State.currentEx;
  const exNum = parseInt(exId.replace('ex', ''));
  const params = getExParams(exId);

  // Build query string
  const qs = Object.entries(params).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&');
  await api(`/exercise${exNum}/set?${qs}`);
  await api(`/exercise${exNum}/start`);

  State.exRunning = true;
  State.exSession = {
    exId, exNum,
    patient: State.currentPatient ? State.currentPatient.id : null,
    startTime: new Date().toISOString(),
    params,
    events: [{ t: Date.now(), e: 'start' }],
    score: {}
  };

  updateExStatus(true, 'En cours...');
  toast(`✅ ${exId.toUpperCase()} démarré`, 'ok');
  showExLivePanel(exId);
}

async function stopEx() {
  const exId = State.currentEx;
  const exNum = parseInt(exId.replace('ex', ''));
  for (let i = 1; i <= 12; i++) api(`/exercise${i}/stop`);

  if (State.exSession) {
    State.exSession.events.push({ t: Date.now(), e: 'stop' });
    State.exSession.endTime = new Date().toISOString();
    saveExSession(State.exSession);
  }
  State.exRunning = false;
  State.exSession = null;

  updateExStatus(false, 'Arrêté');
  hideExLivePanel();
  toast('■ Exercice arrêté', 'ok');
}

function updateExStatus(running, msg) {
  const ribbon = document.getElementById('ex-status-ribbon');
  const txt = document.getElementById('ex-status-text');
  if (ribbon) ribbon.className = `status-ribbon ${running ? 'running' : 'stopped'} mt-12`;
  if (txt) txt.textContent = msg;
  const btn = document.getElementById('btn-ex-start');
  if (btn) { btn.textContent = running ? '⏸ En cours...' : '▶ DÉMARRER'; btn.disabled = running; }
}

async function pollExStatus() {
  if (!State.currentEx) return;
  const exNum = parseInt(State.currentEx.replace('ex', ''));
  const s = await api(`/exercise${exNum}/status`);
  if (!s) return;
  State.exScore = s.score || {};
  updateExLiveText(s);
}

function updateExLive(status) {
  // update COP display
  drawCOPMini('ex-cop-canvas', status.cop_x_cm, status.cop_y_cm);
}

function updateExLiveText(s) {
  const el = document.getElementById('ex-live-score-text');
  if (!el) return;
  let txt = '';
  if (s.score) {
    const sc = s.score;
    if (sc.hold_time !== undefined) txt = `Maintien: ${sc.hold_time.toFixed(1)}s / ${sc.goal_s || 5}s`;
    else if (sc.index !== undefined) txt = `Cible: ${sc.index + 1} | Tours: ${sc.laps || 0}`;
    else if (sc.completed !== undefined) txt = `Points: ${sc.completed} | Idx: ${sc.index}`;
    else if (sc.offtrack !== undefined) txt = `Sorties: ${sc.offtrack} | Idx: ${sc.index}`;
  }
  el.textContent = txt || (s.running ? 'En cours' : 'Prêt');
}

function showExLivePanel(exId) {
  const panel = document.getElementById('ex-live-panel');
  if (!panel) return;
  panel.className = 'visible';
  const nm = panel.querySelector('.ex-live-name');
  if (nm) nm.textContent = exId.toUpperCase() + ' – ' + (EXERCISES.find(e => e.id === exId)?.name || '');
}

function hideExLivePanel() {
  const panel = document.getElementById('ex-live-panel');
  if (panel) panel.className = '';
}

// =====================================================================
// PRESETS
// =====================================================================
function renderPresets() {
  const grid = document.getElementById('preset-grid');
  if (!grid) return;
  grid.innerHTML = '';
  DEFAULT_PRESETS.forEach(preset => {
    const div = document.createElement('div');
    div.className = `preset-card ${preset.color}`;
    div.innerHTML = `
      <div class="pc-icon">${preset.icon}</div>
      <div class="pc-name">${preset.name}</div>
      <div class="pc-sub">${preset.desc}</div>`;
    div.onclick = () => applyPreset(preset);
    grid.appendChild(div);
  });
}

async function applyPreset(preset) {
  if (!State.status.tare_ready || !State.status.offset_ready) {
    toast('❌ Effectuer Tare + Centrage d\'abord', 'err');
    return;
  }
  // Stop all exercises first
  for (let i = 1; i <= 12; i++) await api(`/exercise${i}/stop`);

  // Apply first exercise in sequence
  const first = preset.sequence[0];
  if (!first) return;

  const exNum = parseInt(first.ex.replace('ex', ''));
  const qs = Object.entries(first.params).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join('&');
  await api(`/exercise${exNum}/set?${qs}`);
  await api(`/exercise${exNum}/start`);

  State.currentEx = first.ex;
  State.exRunning = true;
  State.exSession = {
    exId: first.ex, exNum,
    preset: preset.id,
    patient: State.currentPatient ? State.currentPatient.id : null,
    startTime: new Date().toISOString(),
    params: first.params,
    events: [{ t: Date.now(), e: 'preset_start', preset: preset.id }],
    score: {}
  };

  showExLivePanel(first.ex);
  toast(`✅ Preset "${preset.name}" démarré`, 'ok');
}

// =====================================================================
// PATIENTS PAGE
// =====================================================================
function renderPatients() {
  const list = document.getElementById('patient-list');
  if (!list) return;

  // Load from localStorage
  loadPatients();

  list.innerHTML = '';
  if (!State.patients.length) {
    list.innerHTML = '<div class="text-muted text-sm text-center" style="padding:24px">Aucun patient enregistré</div>';
    return;
  }

  State.patients.forEach(p => {
    const div = document.createElement('div');
    div.className = 'patient-item';
    const initials = ((p.prenom || '?')[0] + (p.nom || '?')[0]).toUpperCase();
    div.innerHTML = `
      <div class="patient-avatar">${initials}</div>
      <div class="patient-info">
        <div class="patient-name">${p.prenom} ${p.nom}</div>
        <div class="patient-meta">${p.age ? p.age + ' ans • ' : ''}${p.objectif || 'Rééducation'}</div>
      </div>
      <div class="patient-chevron">›</div>`;
    div.onclick = () => openPatient(p);
    list.appendChild(div);
  });
}

function openPatient(p) {
  State.currentPatient = p;
  // Update home patient indicator
  const patEl = document.getElementById('home-patient');
  if (patEl) patEl.textContent = `Patient: ${p.prenom} ${p.nom}`;
  showPatientModal(p);
}

function showPatientModal(p) {
  const mb = document.getElementById('modal-box');
  const sessions = State.sessions.filter(s => s.patient === p.id);
  const lastSessions = sessions.slice(-5).reverse();

  mb.innerHTML = `
    <div class="modal-handle"></div>
    <div id="modal-title">${p.prenom} ${p.nom}</div>
    <div class="flex gap-8 flex-wrap mb-12">
      <span class="badge badge-blue">${p.age ? p.age + ' ans' : 'Âge N/D'}</span>
      <span class="badge badge-muted">${p.niveau || 'Niveau N/D'}</span>
      ${p.contraintes ? `<span class="badge badge-orange">${p.contraintes}</span>` : ''}
    </div>
    <div class="section-title">Objectif</div>
    <div class="text-sm mb-12">${p.objectif || '–'}</div>
    <div class="section-title">Séances récentes</div>
    ${lastSessions.length === 0 ? '<div class="text-muted text-sm">Aucune séance</div>' : ''}
    ${lastSessions.map(s => `
      <div class="session-item">
        <div class="session-header">
          <div class="session-date">${fmtDate(s.startTime)}</div>
          <span class="badge badge-blue">${s.exId.toUpperCase()}</span>
        </div>
        <div class="text-sm text-muted">${s.preset ? 'Preset: ' + s.preset : 'Exercice libre'}</div>
      </div>`).join('')}
    <div class="divider"></div>
    <div class="flex gap-8">
      <button class="btn btn-green btn-full" onclick="selectPatientAndGo('${p.id}')">Sélectionner + Exercices</button>
      <button class="btn btn-ghost" onclick="closeModal()">Fermer</button>
    </div>`;

  document.getElementById('modal-overlay').classList.add('show');
}

function selectPatientAndGo(patId) {
  State.currentPatient = State.patients.find(p => p.id === patId);
  closeModal();
  navigate('exercises');
}

function openNewPatientModal() {
  const mb = document.getElementById('modal-box');
  mb.innerHTML = `
    <div class="modal-handle"></div>
    <div id="modal-title">Nouveau patient</div>
    <div class="field"><label>Prénom *</label><input id="np-prenom" type="text" placeholder="Marie"/></div>
    <div class="field"><label>Nom *</label><input id="np-nom" type="text" placeholder="Dupont"/></div>
    <div class="field"><label>Âge</label><input id="np-age" type="number" min="1" max="120" placeholder="65"/></div>
    <div class="field"><label>Niveau</label>
      <select id="np-niveau">
        <option value="debutant">Débutant</option>
        <option value="intermediaire">Intermédiaire</option>
        <option value="avance">Avancé</option>
      </select>
    </div>
    <div class="field"><label>Objectif thérapeutique</label>
      <select id="np-objectif">
        <option value="Rééducation vestibulaire">Rééducation vestibulaire</option>
        <option value="Prévention chute">Prévention chute</option>
        <option value="Retour au sport">Retour au sport</option>
        <option value="Cervicalgie">Cervicalgie</option>
        <option value="Proprioception">Proprioception</option>
        <option value="Double tâche">Double tâche</option>
        <option value="Bilan postural">Bilan postural</option>
      </select>
    </div>
    <div class="field"><label>Contre-indications</label>
      <input id="np-contraintes" type="text" placeholder="Ex: épilepsie"/>
    </div>
    <div class="flex gap-8 mt-12">
      <button class="btn btn-blue btn-full" onclick="saveNewPatient()">✅ Enregistrer</button>
      <button class="btn btn-ghost" onclick="closeModal()">Annuler</button>
    </div>`;
  document.getElementById('modal-overlay').classList.add('show');
}

function saveNewPatient() {
  const g = id => document.getElementById(id)?.value.trim() || '';
  const prenom = g('np-prenom');
  const nom = g('np-nom');
  if (!prenom || !nom) { toast('Prénom et nom requis', 'err'); return; }

  const p = {
    id: 'pat_' + Date.now(),
    prenom, nom,
    age: g('np-age') || null,
    niveau: g('np-niveau'),
    objectif: g('np-objectif'),
    contraintes: g('np-contraintes'),
    createdAt: new Date().toISOString()
  };
  State.patients.push(p);
  savePatients();
  closeModal();
  renderPatients();
  toast(`✅ ${prenom} ${nom} ajouté(e)`, 'ok');
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('show');
}

// =====================================================================
// MORE / SETTINGS PAGE
// =====================================================================
function renderMore() {
  const s = State.status;
  const espEl = document.getElementById('more-esp-status');
  if (espEl) espEl.textContent = s.send_to_esp ? 'Connecté' : 'Déconnecté';

  // Load session count
  loadSessions();
  const sessEl = document.getElementById('more-session-count');
  if (sessEl) sessEl.textContent = State.sessions.length + ' séances';
}

// =====================================================================
// COP MINI CANVAS
// =====================================================================
function drawCOPMini(canvasId, x, y) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  const cx = w / 2, cy = h / 2;
  const scale = Math.min(w, h) * 0.38;

  ctx.clearRect(0, 0, w, h);

  // Background
  ctx.fillStyle = '#1e293b';
  ctx.fillRect(0, 0, w, h);

  // Grid
  ctx.strokeStyle = 'rgba(148,163,184,0.2)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(cx, h * 0.1); ctx.lineTo(cx, h * 0.9);
  ctx.moveTo(w * 0.1, cy); ctx.lineTo(w * 0.9, cy);
  ctx.stroke();

  // Safe zone circle
  ctx.beginPath();
  ctx.arc(cx, cy, scale * 0.8, 0, Math.PI * 2);
  ctx.strokeStyle = 'rgba(59,130,246,0.3)';
  ctx.stroke();

  // COP point
  const px = cx + x * scale * 0.5;
  const py = cy - y * scale * 0.5;
  ctx.beginPath();
  ctx.arc(px, py, 7, 0, Math.PI * 2);
  ctx.fillStyle = '#22c55e';
  ctx.fill();

  // Center dot
  ctx.beginPath();
  ctx.arc(cx, cy, 3, 0, Math.PI * 2);
  ctx.fillStyle = 'rgba(255,255,255,0.3)';
  ctx.fill();
}

// =====================================================================
// PERSISTENCE (localStorage)
// =====================================================================
function savePatients() { localStorage.setItem('sps_patients', JSON.stringify(State.patients)); }
function loadPatients() {
  try { State.patients = JSON.parse(localStorage.getItem('sps_patients') || '[]'); } catch { State.patients = []; }
}
function saveSessions() { localStorage.setItem('sps_sessions', JSON.stringify(State.sessions)); }
function loadSessions() {
  try { State.sessions = JSON.parse(localStorage.getItem('sps_sessions') || '[]'); } catch { State.sessions = []; }
}
function saveExSession(session) {
  loadSessions();
  State.sessions.push(session);
  saveSessions();
  // Also send to server
  api('/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(session)
  });
}

function exportSessions() {
  loadSessions();
  const data = JSON.stringify(State.sessions, null, 2);
  const blob = new Blob([data], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `posturosps_sessions_${new Date().toISOString().slice(0,10)}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

function exportCSV() {
  loadSessions();
  if (!State.sessions.length) { toast('Aucune séance à exporter', 'err'); return; }
  const rows = [['patient', 'exercice', 'preset', 'debut', 'fin', 'parametres']];
  State.sessions.forEach(s => {
    rows.push([
      s.patient || '',
      s.exId || '',
      s.preset || '',
      s.startTime || '',
      s.endTime || '',
      JSON.stringify(s.params || {})
    ]);
  });
  const csv = rows.map(r => r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `posturosps_sessions_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

function fmtDate(iso) {
  if (!iso) return '–';
  try {
    const d = new Date(iso);
    return d.toLocaleDateString('fr-FR', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  } catch { return iso; }
}

// =====================================================================
// PLATFORM QUICK CONTROLS
// =====================================================================
async function espHome() {
  await api('/esp/home');
  toast('ESP: HOME', 'ok');
}
async function espCenter() {
  await api('/esp/center');
  toast('ESP: CENTER', 'ok');
}
async function espStop() {
  await api('/esp/stop');
  toast('ESP: STOP', 'ok');
}
async function espStart() {
  await api('/esp/start');
  toast('ESP: ASSERV démarré', 'ok');
}

async function shutdownPi() {
  if (!confirm('Éteindre le Raspberry Pi ?')) return;
  await api('/system/shutdown');
  toast('Arrêt en cours...', 'ok', 10000);
}

// =====================================================================
// INSTALL PROMPT (PWA)
// =====================================================================
let deferredInstallPrompt = null;
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  deferredInstallPrompt = e;
  const btn = document.getElementById('btn-install');
  if (btn) btn.classList.remove('hidden');
});

async function installApp() {
  if (!deferredInstallPrompt) return;
  deferredInstallPrompt.prompt();
  const { outcome } = await deferredInstallPrompt.userChoice;
  if (outcome === 'accepted') toast('✅ Application installée !', 'ok');
  deferredInstallPrompt = null;
  const btn = document.getElementById('btn-install');
  if (btn) btn.classList.add('hidden');
}

// =====================================================================
// INIT
// =====================================================================
document.addEventListener('DOMContentLoaded', () => {
  // Register service worker
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js')
      .then(reg => console.log('SW registered:', reg.scope))
      .catch(e => console.warn('SW error:', e));
  }

  // Load persisted data
  loadPatients();
  loadSessions();

  // Start status polling
  startStatusPoll();

  // Read page from URL
  const urlPage = new URLSearchParams(location.search).get('page') || 'home';
  navigate(urlPage);

  // Render exercise filter tabs
  renderExFilterTabs();

  // Modal close on overlay click
  document.getElementById('modal-overlay').addEventListener('click', e => {
    if (e.target === document.getElementById('modal-overlay')) closeModal();
  });

  // Handle back button
  window.addEventListener('popstate', e => {
    if (e.state && e.state.page) navigate(e.state.page);
  });
});
