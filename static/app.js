/* CodeHop uplink simulator — UI.  Talks to demo/server.py; no dependencies. */
"use strict";
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  const state = {
    schema: null,
    knobs: {},            // name -> {def, input, valueEl, wrap}
    frame: null,
    stage: 0,
    stageSpec: null,
    stepToBlock: [],
    rx: "sic",
    view: "clean",
    selUser: null,
    selBlock: null,
    corr: null,
    dyn: 45,
    playing: false,
    sweep: { rows: [], cfgKey: null, running: false },
    memo: {},            // modulation -> {key, kpi}: same nodes under each waveform
    hover: null,
    mapHover: null,
  };

  // ------------------------------------------------------------------ utils
  const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
  const pct = (x, d = 1) => (100 * x).toFixed(d) + "%";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  function debounce(fn, ms) {
    let t = null;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  }
  function fmt(x, d = 1) {
    if (x === null || x === undefined || Number.isNaN(x)) return "–";
    const s = Math.abs(x) >= 1e4 ? x.toLocaleString("en-US", { maximumFractionDigits: 0 }) : x.toFixed(d);
    return s.replace(/^-/, "−");
  }
  function decimals(step) {
    const s = String(step);
    return s.includes(".") ? s.split(".")[1].length : 0;
  }
  function niceTicks(lo, hi, n = 5) {
    if (!(hi > lo)) return [lo];
    const span = hi - lo;
    const raw = span / n;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toFixed(10));
    return out;
  }
  function setupCanvas(cv, cssH) {
    const w = cv.parentElement.clientWidth;
    const dpr = window.devicePixelRatio || 1;
    cv.style.height = cssH + "px";
    cv.width = Math.max(1, Math.round(w * dpr));
    cv.height = Math.round(cssH * dpr);
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, cssH);
    return { ctx, w, h: cssH };
  }
  function setStatus(msg, err = false) {
    const el = $("#status");
    el.textContent = msg;
    el.classList.toggle("err", !!err);
  }
  async function api(path, body) {
    const opt = body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    };
    const res = await fetch(path, opt);
    if (!res.ok) {
      let msg = res.status + " " + res.statusText;
      try {
        const j = await res.json();
        if (typeof j.detail === "string") msg = j.detail;
        else if (Array.isArray(j.detail)) msg = j.detail.map((e) => (e.loc || []).slice(-1)[0] + ": " + e.msg).join("; ");
      } catch (e) { /* keep status text */ }
      const er = new Error(msg);
      er.status = res.status;
      throw er;
    }
    return res;
  }
  const axisFont = "11px system-ui, -apple-system, 'Segoe UI', sans-serif";

  function drawAxes(ctx, box, xs, ys, opt = {}) {
    // xs/ys: {lo, hi, ticks, fmt}; box: {x, y, w, h}
    ctx.save();
    ctx.font = axisFont;
    ctx.lineWidth = 1;
    ctx.strokeStyle = css("--grid");
    ctx.fillStyle = css("--muted");
    ctx.textBaseline = "middle";
    ctx.textAlign = "right";
    for (const v of ys.ticks) {
      const y = box.y + box.h - ((v - ys.lo) / (ys.hi - ys.lo)) * box.h;
      if (opt.ygrid !== false) {
        ctx.beginPath(); ctx.moveTo(box.x, Math.round(y) + 0.5); ctx.lineTo(box.x + box.w, Math.round(y) + 0.5); ctx.stroke();
      }
      ctx.fillText(ys.fmt(v), box.x - 6, y);
    }
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (const v of xs.ticks) {
      const x = box.x + ((v - xs.lo) / (xs.hi - xs.lo)) * box.w;
      ctx.fillText(xs.fmt(v), x, box.y + box.h + 6);
    }
    ctx.strokeStyle = css("--axis");
    ctx.beginPath(); ctx.moveTo(box.x, box.y + box.h + 0.5); ctx.lineTo(box.x + box.w, box.y + box.h + 0.5); ctx.stroke();
    if (opt.xlabel) {
      ctx.textAlign = "right";
      ctx.fillStyle = css("--muted");
      ctx.fillText(opt.xlabel, box.x + box.w, box.y + box.h + 20);
    }
    if (opt.ylabel) {
      ctx.save();
      ctx.translate(12, box.y + box.h / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(opt.ylabel, 0, 0);
      ctx.restore();
    }
    ctx.restore();
  }

  function showTip(tip, wrap, x, y, html) {
    tip.innerHTML = html;
    tip.hidden = false;
    const W = wrap.clientWidth;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let left = x + 14, top = y + 14;
    if (left + tw > W) left = x - tw - 14;
    left = clamp(left, 4, Math.max(4, W - tw - 4));
    if (top + th > wrap.clientHeight + 40) top = y - th - 10;
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }
  const row = (k, v) => `<div class="row"><span>${k}</span><b>${v}</b></div>`;

  // ------------------------------------------------------------------ knobs
  function buildKnobs(schema) {
    const form = $("#knobs");
    form.innerHTML = "";
    const groups = [];
    for (const k of schema.knobs) if (!groups.includes(k.group)) groups.push(k.group);
    const closed = new Set(["Packet", "Run"]);
    for (const g of groups) {
      const det = document.createElement("details");
      det.className = "group";
      det.open = !closed.has(g);
      det.innerHTML = `<summary><h3>${g}</h3></summary>`;
      for (const k of schema.knobs.filter((x) => x.group === g)) det.appendChild(buildKnob(k));
      const ro = document.createElement("div");
      ro.className = "readout";
      ro.dataset.group = g;
      det.appendChild(ro);
      form.appendChild(det);
    }
  }

  function buildKnob(k) {
    const wrap = document.createElement("div");
    wrap.className = "knob";
    const id = "k-" + k.name;
    let input, valueEl = null;
    if (k.kind === "select") {
      wrap.innerHTML = `<div class="knob-top"><label for="${id}">${k.label}</label></div>`;
      input = document.createElement("select");
      k.options.forEach(([v, lab], i) => {
        const o = document.createElement("option");
        o.value = String(i);
        o.textContent = lab;
        input.appendChild(o);
      });
    } else if (k.kind === "bool") {
      wrap.classList.add("bool");
      const lab = document.createElement("label");
      input = document.createElement("input");
      input.type = "checkbox";
      lab.appendChild(input);
      lab.appendChild(document.createTextNode(k.label));
      wrap.appendChild(lab);
    } else if (k.max - k.min > 5000 && k.name === "seed") {
      wrap.classList.add("int-free");
      wrap.innerHTML = `<div class="knob-top"><label for="${id}">${k.label}</label></div>`;
      input = document.createElement("input");
      input.type = "number";
      input.min = k.min; input.max = k.max; input.step = 1;
    } else {
      wrap.innerHTML = `<div class="knob-top"><label for="${id}">${k.label}</label><span class="knob-val"></span></div>`;
      valueEl = $(".knob-val", wrap);
      input = document.createElement("input");
      input.type = "range";
      input.min = k.min; input.max = k.max; input.step = k.step;
    }
    input.id = id;
    input.name = k.name;
    if (k.kind !== "bool") wrap.appendChild(input);
    input.addEventListener("input", () => { updateKnobText(k.name); onKnobChange(k.name); });
    state.knobs[k.name] = { def: k, input, valueEl, wrap };
    return wrap;
  }

  function knobValue(name) {
    const { def, input } = state.knobs[name];
    if (def.kind === "select") return def.options[+input.value][0];
    if (def.kind === "bool") return input.checked;
    const v = parseFloat(input.value);
    return def.kind === "int" ? Math.round(v) : v;
  }

  function setKnob(name, v) {
    const kn = state.knobs[name];
    if (!kn) return;
    const { def, input } = kn;
    if (def.kind === "select") {
      let idx = def.options.findIndex(([ov]) => ov === v || (typeof ov === "number" && Math.abs(ov - v) < 1e-9));
      input.value = String(Math.max(0, idx));
    } else if (def.kind === "bool") input.checked = !!v;
    else input.value = v;
    updateKnobText(name);
  }

  function updateKnobText(name) {
    const { def, valueEl } = state.knobs[name];
    if (!valueEl) return;
    const v = knobValue(name);
    if (def.fmt === "phase") {
      const where = v < -0.04 ? "rising" : v > 0.04 ? "setting" : "peak";
      valueEl.textContent = `${fmt(v, 2)} · ${where}`;
      return;
    }
    const sc = def.scale || 1;
    const d = def.scale ? decimals(def.step * sc) : decimals(def.step);
    valueEl.textContent = fmt(v * sc, d) + (def.unit ? " " + def.unit : "");
  }

  function readCfg() {
    const cfg = {};
    for (const name in state.knobs) cfg[name] = knobValue(name);
    return cfg;
  }

  function applyCfg(cfg) {
    for (const [k, v] of Object.entries(cfg)) setKnob(k, v);
    updateVisibility();
  }

  function updateVisibility() {
    const cfg = readCfg();
    for (const name in state.knobs) {
      const { def, wrap } = state.knobs[name];
      let show = true;
      if (def.show_if) for (const [f, vals] of Object.entries(def.show_if)) if (!vals.includes(cfg[f])) show = false;
      wrap.hidden = !show;
    }
    // rs_k can't exceed n_data
    const rs = state.knobs.rs_k, nd = knobValue("n_data");
    rs.input.max = nd;
    if (knobValue("rs_k") > nd) setKnob("rs_k", nd);
  }

  const scheduleRun = debounce(() => runFrame(), 450);
  const scheduleDerived = debounce(() => refreshDerived(), 120);

  function onKnobChange(name) {
    updateVisibility();
    scheduleDerived();
    if ($("#autorun").checked) scheduleRun();
    markSweepStale();
  }

  // ------------------------------------------------------------------ defaults
  function loadDefaults(run) {
    applyCfg(state.schema.defaults);
    refreshDerived();
    markSweepStale();
    if (run) runFrame();
  }

  // ------------------------------------------------------------------ derived readouts
  async function refreshDerived() {
    let d;
    try {
      d = await (await api("/api/derived", readCfg())).json();
    } catch (e) {
      setStatus(e.message, true);
      return;
    }
    state.derived = d;
    const cfg = readCfg();
    const R = {};
    const kHz = (x) => fmt(x / 1e3, 2) + " kHz";
    const lora = cfg.modulation === "lora";
    R["Nodes"] = lora
      ? [["Channel collision", pct(d.p_coll_joint, 1)]]
      : cfg.signature === "distinct"
        ? [["Codes", d.n_codes]]
        : [["Code collision", pct(d.p_coll_code, 1)], ["Signature collision", pct(d.p_coll_joint, 2)]];
    R["Satellite Parameters"] = [
      ["Time", fmt(d.t_now_s, 0) + " s"],
      ["Elevation", fmt(d.centre_elev_deg, 1) + "°"],
      ["Slant range", fmt(d.centre_slant_km, 0) + " km"],
      ["Doppler", kHz(d.centre_doppler_hz)],
      ["Doppler rate", fmt(d.centre_rate_hz_s, 0) + " Hz/s"],
    ];
    R["Power"] = cfg.power_model === "link" ? [["Centre SNR", fmt(d.centre_snr_db, 1) + " dB"]] : [];
    R["PHY"] = [
      ["Chips", d.n],
      ["Symbol", fmt(d.t_block_s * 1e3, 2) + " ms"],
      ["Bandwidth", fmt(d.rc / 1e3, 0) + " kHz"],
      ["Proc. gain", fmt(d.proc_gain_db, 2) + " dB"],
      ["CFO scale", fmt(d.cfo_scale_hz, 0) + " Hz"],
      ["Sample rate", fmt(d.fs / 1e6, 2) + " MS/s"],
    ];
    R["Packet"] = [
      ["Airtime", fmt(d.t_packet_s * 1e3, 2) + " ms"],
      ["Payload", fmt(cfg.n_data * d.bits_per_symbol, 1) + " bits"],
      ["Correctable", d.rs_t],
      ["Info bits", fmt(d.info_bits, 1)],
    ];
    const rec = [["Search centre", kHz(cfg.bulk_comp ? d.centre_doppler_hz : 0)]];
    if (!cfg.bulk_comp && Math.abs(d.centre_doppler_hz) > cfg.cfo_span_khz * 1e3)
      rec.push(["__warn", "Doppler outside search"]);
    R["Receiver"] = rec;
    R["Run"] = [["Cost", fmt(d.load * 100, 0) + "%"]];
    if (d.load > 1) R["Run"].push(["__warn", "Too heavy"]);
    $$(".readout").forEach((el) => {
      const rows = R[el.dataset.group] || [];
      el.innerHTML = rows.map(([k, v]) => k === "__warn" ? `<span class="warnline">⚠ ${v}</span>` : `<span>${k}</span><b>${v}</b>`).join("");
      el.hidden = rows.length === 0;
    });
  }

  // ------------------------------------------------------------------ frame
  // Fields that only describe the waveform; everything else (nodes, pass,
  // power, channels, packet, receiver, seed) is shared between the two.
  const WAVEFORM_FIELDS = ["modulation", "family", "chip_rate", "bt", "mod_index", "hop_pool",
    "signature", "guard_us", "lora_sf", "lora_bw_khz"];
  const MOD_LABEL = { codehop: "CodeHop", lora: "Hopping LoRa" };
  const otherMod = (m) => (m === "lora" ? "codehop" : "lora");
  function sharedKey(cfg) {
    const c = { ...cfg };
    for (const f of WAVEFORM_FIELDS) delete c[f];
    return JSON.stringify(Object.keys(c).sort().map((k) => [k, c[k]]));
  }
  let otherReq = 0;
  async function fetchOther(fr) {
    const my = ++otherReq;
    const m = otherMod(fr.cfg.modulation), key = sharedKey(fr.cfg);
    if (state.memo[m] && state.memo[m].key === key) return;
    try {
      const j = await (await api("/api/kpi", { ...fr.cfg, modulation: m })).json();
      if (my !== otherReq) return;
      state.memo[m] = { key: sharedKey(j.cfg), kpi: j.kpi };
      renderKPIs();
    } catch (e) { /* the comparison is optional */ }
  }
  let frameReq = 0;
  async function runFrame() {
    const my = ++frameReq;
    stopPlay();
    const cfg = readCfg();
    $("#run").disabled = true;
    setStatus("running…");
    try {
      const fr = await (await api("/api/frame", cfg)).json();
      if (my !== frameReq) return;
      state.frame = fr;
      state.stage = 0;
      state.stageSpec = null;
      state.selUser = null;
      state.stepToBlock = [];
      for (const b of fr.blocks) if (b.step >= 0) state.stepToBlock[b.step] = b;
      autoDyn();
      const st = $("#stage");
      st.max = fr.n_steps;
      st.value = 0;
      // default block: the hardest data block under SIC
      let pick = null;
      for (const b of fr.blocks) if (b.kind === "data" && (!pick || b.sic_m < pick.sic_m)) pick = b;
      state.selBlock = pick ? pick.j : null;
      state.selUser = null;
      setStatus(`${fr.elapsed_ms.toFixed(0)} ms`);
      state.memo[fr.cfg.modulation] = { key: sharedKey(fr.cfg), kpi: fr.kpi };
      renderAll();
      loadCorr();
      fetchOther(fr);
    } catch (e) {
      if (my === frameReq) setStatus(e.message, true);
    } finally {
      if (my === frameReq) $("#run").disabled = false;
    }
  }

  function renderAll() {
    renderKPIs();
    drawSpec();
    renderUsers();
    drawDoppler();
    drawCorr();
    updateStageLabel();
    drawSweep();
    drawMap();
  }

  // ------------------------------------------------------------------ KPIs
  function renderKPIs() {
    const fr = state.frame;
    if (!fr) return;
    const c = fr.cfg;
    const om = otherMod(c.modulation), o = state.memo[om];
    const fresh = o && o.key === sharedKey(c);
    const tile = (lab, k, kind, col) => {
      const v = fr.kpi[k];
      const frac = kind === "raw" ? v.raw : v.pkt;
      const val = kind === "raw" ? pct(v.raw) : `${v.pkt_ok}<small>/ ${v.users}</small>`;
      const found = `found ${v.det}/${v.users}` + (v.false ? ` · ${v.false} false` : "");
      const note = kind === "raw" ? `${v.sym_correct} / ${v.sym_total}` : found;
      const ov = fresh ? (kind === "raw" ? pct(o.kpi[k].raw) : `${o.kpi[k].pkt_ok} / ${o.kpi[k].users}`) : "…";
      return `<div class="tile"><div class="lab">${lab}</div>
        <div class="val">${val}</div>
        <div class="note">${note}</div>
        <div class="meter"><div style="width:${(frac * 100).toFixed(1)}%;background:var(${col})"></div></div>
        <div class="cmp">${MOD_LABEL[om]} <b>${ov}</b></div></div>`;
    };
    $("#kpis").innerHTML = tile("SIC symbols", "sic", "raw", "--s1") + tile("SIC packets", "sic", "pkt", "--s1") +
      tile("TIN symbols", "tin", "raw", "--s2") + tile("TIN packets", "tin", "pkt", "--s2");
  }

  // ------------------------------------------------------------------ spectrogram
  // MATLAB/matplotlib "jet": dark blue -> blue -> cyan -> yellow -> red -> dark red
  const JET_LUT = (() => {
    const lut = new Uint8ClampedArray(256 * 3);
    for (let i = 0; i < 256; i++) {
      const x = i / 255;
      lut[i * 3] = 255 * clamp(1.5 - Math.abs(4 * x - 3), 0, 1);
      lut[i * 3 + 1] = 255 * clamp(1.5 - Math.abs(4 * x - 2), 0, 1);
      lut[i * 3 + 2] = 255 * clamp(1.5 - Math.abs(4 * x - 1), 0, 1);
    }
    return lut;
  })();
  function makeLUT() { return JET_LUT; }
  const specCache = new WeakMap();
  function specBytes(spec) {
    let a = specCache.get(spec);
    if (!a) {
      const bin = atob(spec.data);
      a = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i);
      specCache.set(spec, a);
    }
    return a;
  }
  function specImage(spec) {
    const arr = specBytes(spec);
    const lut = makeLUT();
    const oc = document.createElement("canvas");
    oc.width = spec.n_t; oc.height = spec.n_f;
    const g = oc.getContext("2d");
    const img = g.createImageData(spec.n_t, spec.n_f);
    const range = spec.db_ceil - spec.db_floor;
    const lo = spec.db_ceil - state.dyn;
    for (let fi = 0; fi < spec.n_f; fi++) {
      const y = spec.n_f - 1 - fi;
      for (let ti = 0; ti < spec.n_t; ti++) {
        const db = spec.db_floor + (arr[fi * spec.n_t + ti] / 255) * range;
        const k = Math.round(clamp((db - lo) / state.dyn, 0, 1) * 255);
        const o = (y * spec.n_t + ti) * 4;
        img.data[o] = lut[k * 3]; img.data[o + 1] = lut[k * 3 + 1]; img.data[o + 2] = lut[k * 3 + 2]; img.data[o + 3] = 255;
      }
    }
    g.putImageData(img, 0, 0);
    return oc;
  }

  let specGeom = null;
  function drawSpec() {
    const fr = state.frame;
    const cv = $("#spec");
    const H = window.innerWidth < 640 ? 260 : 360;
    const { ctx, w, h } = setupCanvas(cv, H);
    if (!fr) return;
    const base = state.view === "clean" ? fr.spec_clean : fr.spec;
    const spec = state.stage > 0 && state.stageSpec ? state.stageSpec : base;
    const box = { x: 58, y: 8, w: w - 58 - 64, h: h - 8 - 40 };
    // limit the displayed band to the occupied span (+ margin) for readability
    const d = fr.derived;
    const chf = d.channel_freqs_hz;
    const occ = (Math.max(...chf.map(Math.abs)) + Math.abs(d.centre_doppler_hz) + d.offset_bound_hz * 0.25 + 1.6 * d.rc) / 1e3;
    const fHalf = Math.min(Math.max(Math.abs(spec.f0_khz), spec.f1_khz), Math.max(occ, 2 * d.rc / 1e3));
    const xs = { lo: spec.t0_ms, hi: spec.t1_ms };
    const ys = { lo: -fHalf, hi: fHalf };
    const X = (t) => box.x + ((t - xs.lo) / (xs.hi - xs.lo)) * box.w;
    const Y = (f) => box.y + box.h - ((f - ys.lo) / (ys.hi - ys.lo)) * box.h;
    // image (crop rows to the displayed band)
    const img = specImage(spec);
    const fPerRow = (spec.f1_khz - spec.f0_khz) / (spec.n_f - 1);
    const rowTop = clamp(Math.floor((spec.f1_khz - fHalf) / fPerRow), 0, spec.n_f - 1);
    const rowBot = clamp(Math.ceil((spec.f1_khz + fHalf) / fPerRow), rowTop + 1, spec.n_f);
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(img, 0, rowTop, spec.n_t, rowBot - rowTop, box.x, box.y, box.w, box.h);
    // axes
    drawAxes(ctx, box,
      { lo: xs.lo, hi: xs.hi, ticks: niceTicks(xs.lo, xs.hi, Math.max(3, Math.floor(box.w / 90))), fmt: (v) => fmt(v, v % 1 ? 1 : 0) },
      { lo: ys.lo, hi: ys.hi, ticks: niceTicks(ys.lo, ys.hi, Math.max(3, Math.floor(box.h / 50))), fmt: (v) => fmt(v, 0) },
      { ygrid: false, xlabel: "time (ms)", ylabel: "offset (kHz)" });
    // block overlays
    const half = 0.6 * d.rc / 1e3;
    const stage = state.stage;
    const rects = [];
    ctx.save();
    ctx.beginPath(); ctx.rect(box.x, box.y, box.w, box.h); ctx.clip();
    const good = css("--good"), bad = css("--bad"), muted = css("--muted"), ink = css("--ink");
    const sel = state.selUser;
    for (const b of fr.blocks) {
      const x0 = X(b.t0), x1 = X(b.t1), y0 = Y(b.f + half), y1 = Y(b.f - half);
      rects.push({ b, x0, x1, y0, y1 });
      const miss = state.rx === "sic" ? b.miss : b.tin_miss;
      const ok = b.kind === "pilot" ? null : state.rx === "sic" ? b.sic === b.true : b.tin === b.true;
      let alpha = 0.95;
      if (sel !== null && b.u !== sel) alpha = 0.28;
      if (state.rx === "sic" && stage > 0 && b.step >= 0 && b.step < stage - 1) alpha *= 0.35;
      ctx.globalAlpha = alpha;
      const rx = Math.round(x0) + 0.5, ry = Math.round(y0) + 0.5;
      const rw = Math.max(1, Math.round(x1 - x0) - 1), rh = Math.max(1, Math.round(y1 - y0) - 1);
      ctx.strokeStyle = "rgba(0,0,0,0.75)";            // halo so outlines read on any jet colour
      ctx.lineWidth = (b.u === sel ? 2 : 1.25) + 2;
      ctx.strokeRect(rx, ry, rw, rh);
      ctx.strokeStyle = miss ? muted : ok === null ? "#ffffff" : ok ? good : bad;
      ctx.lineWidth = b.u === sel ? 2 : 1.25;
      if (miss) ctx.setLineDash([3, 3]);
      ctx.strokeRect(rx, ry, rw, rh);
      ctx.setLineDash([]);
      if (state.rx === "sic" && stage > 0 && b.step === stage - 1) {
        ctx.globalAlpha = 1; ctx.strokeStyle = ink; ctx.lineWidth = 2.5;
        ctx.strokeRect(x0 - 2, y0 - 2, x1 - x0 + 4, y1 - y0 + 4);
      }
      if (b.j === state.selBlock) {
        ctx.globalAlpha = 1; ctx.strokeStyle = ink; ctx.lineWidth = 1.5;
        ctx.setLineDash([3, 3]);
        ctx.strokeRect(x0 - 4, y0 - 4, x1 - x0 + 8, y1 - y0 + 8);
        ctx.setLineDash([]);
      }
    }
    ctx.restore();
    // colorbar
    const cb = { x: box.x + box.w + 18, y: box.y, w: 10, h: box.h };
    const lut = makeLUT();
    for (let i = 0; i < cb.h; i++) {
      const k = Math.round((1 - i / cb.h) * 255);
      ctx.fillStyle = `rgb(${lut[k * 3]},${lut[k * 3 + 1]},${lut[k * 3 + 2]})`;
      ctx.fillRect(cb.x, cb.y + i, cb.w, 1);
    }
    ctx.font = axisFont; ctx.fillStyle = css("--muted"); ctx.textAlign = "left"; ctx.textBaseline = "middle";
    for (const v of [0, -state.dyn / 2, -state.dyn]) ctx.fillText(fmt(v, 0), cb.x + cb.w + 4, cb.y + (-v / state.dyn) * cb.h);
    ctx.textBaseline = "top";
    ctx.fillText("dB", cb.x - 2, cb.y + cb.h + 6);
    specGeom = { rects, box };
  }

  function hitBlock(mx, my) {
    if (!specGeom) return null;
    let best = null;
    for (const r of specGeom.rects) {
      if (mx >= r.x0 - 2 && mx <= r.x1 + 2 && my >= r.y0 - 2 && my <= r.y1 + 2) {
        const pri = (r.b.u === state.selUser ? 0 : 1) + (r.x1 - r.x0) * 1e-6;
        if (!best || pri < best.pri) best = { pri, b: r.b };
      }
    }
    return best ? best.b : null;
  }

  function blockTip(b) {
    const fr = state.frame;
    const u = fr.users[b.u];
    const rx = state.rx;
    const miss = rx === "sic" ? b.miss : b.tin_miss;
    const dec = miss ? "missed" : b.kind === "pilot" ? "pilot" : (rx === "sic" ? b.sic : b.tin);
    const ok = b.kind === "pilot" || miss ? "" : (dec === b.true ? ' <span class="ok"></span>' : ' <span class="no"></span>');
    return `<div class="tt-h">Node ${b.u} · block ${b.b}</div>` +
      row("Channel", b.ch) + row("Shift", b.true) +
      (b.kind === "data" ? row(rx.toUpperCase(), dec + ok) : "") +
      (b.kind === "data" && !miss ? row("Margin", fmt(rx === "sic" ? b.sic_m : b.tin_m, 1) + " dB") : "") +
      (b.step >= 0 ? row("SIC step", b.step + 1) : "") +
      row("SNR", fmt(u.snr_db, 1) + " dB") +
      row("Offset", fmt(b.f, 2) + " kHz");
  }

  function wireSpec() {
    const cv = $("#spec"), tip = $("#tip"), wrap = cv.parentElement;
    cv.addEventListener("mousemove", (e) => {
      const r = cv.getBoundingClientRect();
      const b = hitBlock(e.clientX - r.left, e.clientY - r.top);
      cv.style.cursor = b ? "pointer" : "default";
      if (b) showTip(tip, wrap, e.clientX - r.left, e.clientY - r.top, blockTip(b));
      else tip.hidden = true;
    });
    cv.addEventListener("mouseleave", () => { tip.hidden = true; });
    cv.addEventListener("click", (e) => {
      const r = cv.getBoundingClientRect();
      const b = hitBlock(e.clientX - r.left, e.clientY - r.top);
      if (b) { state.selBlock = b.j; state.selUser = b.u; loadCorr(); }
      else { state.selUser = null; }
      drawSpec(); renderUsers(); drawMap();
    });
  }

  // ------------------------------------------------------------------ SIC peel
  let stageReq = 0;
  async function fetchStage(s) {
    const fr = state.frame;
    if (!fr) return;
    const my = ++stageReq;
    if (s === 0) { state.stageSpec = null; drawSpec(); updateStageLabel(); return; }
    try {
      const j = await (await api("/api/stage", { cfg: fr.cfg, stage: s, view: state.view })).json();
      if (my !== stageReq || state.frame !== fr) return;
      state.stageSpec = j.spec;
      drawSpec();
      updateStageLabel();
    } catch (e) {
      setStatus(e.message, true);
    }
  }
  const fetchStageDebounced = debounce(fetchStage, 60);

  function updateStageLabel() {
    const fr = state.frame;
    const s = state.stage;
    let txt = "Received";
    if (fr && s > 0) {
      const b = state.stepToBlock[s - 1];
      txt = `Step ${s} / ${fr.n_steps}` + (b ? ` · node ${b.u} block ${b.b}` : "");
    }
    $("#stage-label").textContent = txt;
  }

  function setPlayIcon(on) {
    $("#play-icon").setAttribute("d", on ? "M7 5h4v14H7zM13 5h4v14h-4z" : "M8 5v14l11-7z");
  }
  function stopPlay() { state.playing = false; setPlayIcon(false); }
  async function play() {
    const fr = state.frame;
    if (!fr) return;
    if (state.playing) { stopPlay(); return; }
    state.playing = true; setPlayIcon(true);
    if (state.rx !== "sic") setRx("sic");
    if (state.stage >= fr.n_steps) state.stage = 0;
    const inc = Math.max(1, Math.ceil(fr.n_steps / 90));
    while (state.playing && state.frame === fr && state.stage < fr.n_steps) {
      state.stage = Math.min(fr.n_steps, state.stage + inc);
      $("#stage").value = state.stage;
      const t0 = performance.now();
      await fetchStage(state.stage);
      await sleep(Math.max(0, 70 - (performance.now() - t0)));
    }
    stopPlay();
  }

  function setRx(rx) {
    state.rx = rx;
    $$("#rx-seg button").forEach((b) => b.classList.toggle("on", b.dataset.rx === rx));
    drawSpec(); drawMap(); loadCorr();
  }

  function autoDyn() {
    const fr = state.frame;
    if (!fr) return;
    state.dyn = state.view === "clean" ? 40 : clamp(Math.round(fr.spec.db_ceil - fr.spec.db_med + 3), 12, 60);
    $("#dyn").value = state.dyn;
    $("#dyn-val").textContent = state.dyn + " dB";
  }

  function setView(v) {
    state.view = v;
    $$("#view-seg button").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
    autoDyn();
    state.stageSpec = null;
    if (state.stage > 0) fetchStage(state.stage); else drawSpec();
  }

  // ------------------------------------------------------------------ correlation
  let corrReq = 0;
  async function loadCorr() {
    const fr = state.frame;
    state.corrMsg = null;
    if (!fr || state.selBlock === null) { state.corr = null; drawCorr(); return; }
    const b = fr.blocks[state.selBlock];
    const rx = b.kind === "pilot" ? "sic" : state.rx;
    const my = ++corrReq;
    try {
      const j = await (await api("/api/corr", { cfg: fr.cfg, j: b.j, rx })).json();
      if (my !== corrReq) return;
      state.corr = j;
    } catch (e) {
      if (my !== corrReq) return;
      state.corr = null;
      state.corrMsg = e.status === 404 && /detected/.test(e.message) ? "Not detected" : null;
    }
    drawCorr();
  }

  function drawCorr() {
    const cv = $("#corr");
    const { ctx, w, h } = setupCanvas(cv, 230);
    const c = state.corr;
    const fr = state.frame;
    if (!c || !fr) {
      ctx.fillStyle = css("--muted"); ctx.font = axisFont;
      ctx.fillText(state.corrMsg || "No block selected", 12, 20);
      return;
    }
    const n = c.profile.length;
    const box = { x: 58, y: 18, w: w - 58 - 12, h: h - 18 - 40 };
    const pk = Math.max(...c.profile);
    const ymax = Math.max(0.2, pk * 1.18);
    const ys = { lo: 0, hi: ymax, ticks: niceTicks(0, ymax, 4), fmt: (v) => fmt(v, 2) };
    const xs = { lo: -0.5, hi: n - 0.5, ticks: niceTicks(0, n - 1, Math.max(3, Math.floor(box.w / 80))), fmt: (v) => fmt(v, 0) };
    drawAxes(ctx, box, xs, ys, { xlabel: "shift", ylabel: "|corr|" });
    const X = (i) => box.x + ((i - xs.lo) / (xs.hi - xs.lo)) * box.w;
    const Y = (v) => box.y + box.h - (v / ymax) * box.h;
    // noise rms reference
    ctx.strokeStyle = css("--muted"); ctx.lineWidth = 1;
    const yn = Y(c.noise_rms);
    ctx.beginPath(); ctx.moveTo(box.x, Math.round(yn) + 0.5); ctx.lineTo(box.x + box.w, Math.round(yn) + 0.5); ctx.stroke();
    // stems
    const bw = Math.max(1, box.w / n - (n <= 140 ? 1 : 0));
    ctx.fillStyle = css("--axis");
    for (let i = 0; i < n; i++) {
      if (i === c.decided || i === c.true) continue;
      const y = Y(c.profile[i]);
      ctx.fillRect(X(i) - bw / 2, y, bw, box.y + box.h - y);
    }
    const ok = c.decided === c.true;
    const col = c.pilot ? css("--muted") : ok ? css("--good") : css("--bad");
    // true shift
    if (c.true !== c.decided) {
      const y = Y(c.profile[c.true]);
      ctx.fillStyle = css("--ink-2");
      ctx.fillRect(X(c.true) - Math.max(1.5, bw / 2), y, Math.max(3, bw), box.y + box.h - y);
    }
    // decided
    const yd = Y(c.profile[c.decided]);
    ctx.fillStyle = col;
    ctx.fillRect(X(c.decided) - Math.max(1.5, bw / 2), yd, Math.max(3, bw), box.y + box.h - yd);
    ctx.beginPath(); ctx.arc(X(c.decided), yd, 5, 0, 2 * Math.PI);
    ctx.lineWidth = 2; ctx.strokeStyle = css("--surface"); ctx.stroke(); ctx.fill();
    // labels
    ctx.font = "600 12px system-ui, sans-serif"; ctx.fillStyle = css("--ink"); ctx.textAlign = "left"; ctx.textBaseline = "top";
    const head = `Node ${c.u} · block ${c.b} · ${c.pilot ? "pilot" : c.rx.toUpperCase()}`;
    ctx.fillText(head, box.x, 0);
    ctx.font = axisFont; ctx.fillStyle = css("--ink-2");
    const lx = X(c.decided) + 8 > box.x + box.w - 120 ? X(c.decided) - 8 : X(c.decided) + 8;
    ctx.textAlign = lx < X(c.decided) ? "right" : "left";
    ctx.textBaseline = "middle";
    ctx.fillText(c.pilot ? `${c.decided}` : `${c.decided} ${ok ? "✓" : "✗ (true " + c.true + ")"}`, lx, Math.max(box.y + 8, yd));
    ctx.font = axisFont; ctx.textAlign = "right"; ctx.textBaseline = "bottom";
    const nw = ctx.measureText("noise").width;
    ctx.fillStyle = css("--surface"); ctx.fillRect(box.x + box.w - nw - 6, yn - 15, nw + 6, 13);
    ctx.fillStyle = css("--muted");
    ctx.fillText("noise", box.x + box.w - 2, yn - 2);
  }

  // ------------------------------------------------------------------ pass
  function drawDoppler() {
    const cv = $("#doppler");
    const { ctx, w, h } = setupCanvas(cv, 210);
    const fr = state.frame;
    if (!fr) return;
    const p = fr.pass;
    const box = { x: 50, y: 10, w: w - 50 - 10, h: h - 10 - 40 };
    const t = p.t_s;
    const lo = Math.min(...p.lo_khz), hi = Math.max(...p.hi_khz);
    const pad = (hi - lo) * 0.08 + 0.5;
    const ys = { lo: lo - pad, hi: hi + pad };
    ys.ticks = niceTicks(ys.lo, ys.hi, 4); ys.fmt = (v) => fmt(v, 0);
    const xs = { lo: t[0], hi: t[t.length - 1] };
    xs.ticks = niceTicks(xs.lo, xs.hi, 4); xs.fmt = (v) => fmt(v, 0);
    drawAxes(ctx, box, xs, ys, { xlabel: "time (s)", ylabel: "kHz" });
    const X = (v) => box.x + ((v - xs.lo) / (xs.hi - xs.lo || 1)) * box.w;
    const Y = (v) => box.y + box.h - ((v - ys.lo) / (ys.hi - ys.lo)) * box.h;
    // user band
    ctx.fillStyle = css("--s1-wash");
    ctx.beginPath();
    t.forEach((tt, i) => (i ? ctx.lineTo(X(tt), Y(p.hi_khz[i])) : ctx.moveTo(X(tt), Y(p.hi_khz[i]))));
    for (let i = t.length - 1; i >= 0; i--) ctx.lineTo(X(t[i]), Y(p.lo_khz[i]));
    ctx.closePath(); ctx.fill();
    // centre
    ctx.strokeStyle = css("--s1"); ctx.lineWidth = 2; ctx.lineJoin = "round";
    ctx.beginPath();
    t.forEach((tt, i) => (i ? ctx.lineTo(X(tt), Y(p.centre_khz[i])) : ctx.moveTo(X(tt), Y(p.centre_khz[i]))));
    ctx.stroke();
    // now
    const tn = p.t_now_s;
    const dn = fr.derived.centre_doppler_hz / 1e3;
    ctx.strokeStyle = css("--ink"); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(Math.round(X(tn)) + 0.5, box.y); ctx.lineTo(Math.round(X(tn)) + 0.5, box.y + box.h); ctx.stroke();
    ctx.fillStyle = css("--s1");
    ctx.beginPath(); ctx.arc(X(tn), Y(dn), 5, 0, 2 * Math.PI);
    ctx.lineWidth = 2; ctx.strokeStyle = css("--surface"); ctx.stroke(); ctx.fill();
    ctx.font = axisFont; ctx.fillStyle = css("--ink"); ctx.textBaseline = "top";
    ctx.textAlign = X(tn) > box.x + box.w * 0.6 ? "right" : "left";
    ctx.fillText(`${fmt(dn, 1)} kHz`, X(tn) + (ctx.textAlign === "left" ? 6 : -6), box.y + 2);
    ctx.textAlign = "left"; ctx.textBaseline = "top";
    const sl = "Nodes";
    ctx.fillStyle = css("--surface");
    ctx.fillRect(box.x + 2, box.y + box.h - 18, ctx.measureText(sl).width + 6, 15);
    ctx.fillStyle = css("--ink-2");
    ctx.fillText(sl, box.x + 4, box.y + box.h - 16);
  }

  // symbol-success ramp: red -> orange -> amber -> green.  Most of the range is
  // spent above 50 %, where the interesting differences are (15/16 vs 16/16).
  const SUCCESS_STOPS = [[0, "#c8302f"], [0.5, "#e2672a"], [0.8, "#e9b62c"], [0.94, "#9cc33a"], [1, "#22a447"]];
  const hexRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  function successColor(f) {
    f = clamp(f, 0, 1);
    for (let i = 1; i < SUCCESS_STOPS.length; i++) {
      const [x1, c1] = SUCCESS_STOPS[i];
      if (f <= x1) {
        const [x0, c0] = SUCCESS_STOPS[i - 1];
        const t = (f - x0) / (x1 - x0 || 1), a = hexRgb(c0), b = hexRgb(c1);
        return `rgb(${a.map((v, k) => Math.round(v + (b[k] - v) * t)).join(",")})`;
      }
    }
    return SUCCESS_STOPS[SUCCESS_STOPS.length - 1][1];
  }
  function nodeSuccess(fr, u) {
    return (state.rx === "sic" ? u.sic_ok : u.tin_ok) / fr.cfg.n_data;
  }

  let mapGeom = null;
  function drawMap() {
    const cv = $("#map"), wrap = cv.parentElement;
    const { ctx, w, h } = setupCanvas(cv, Math.max(240, wrap.clientHeight));
    const fr = state.frame;
    if (!fr) return;
    const f = fr.footprint, users = fr.users, ss = f.subsat;
    // view: all nodes, the node region and the satellite, equal scale
    const xs = [ss[0], -f.cap_km, f.cap_km, ...users.map((u) => u.x_km)];
    const ys = [ss[1], -f.cap_km, f.cap_km, ...users.map((u) => u.y_km)];
    let x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const span = Math.max(x1 - x0, y1 - y0, 300);
    const sc = Math.min(w / (span * 1.3), h / (span * 1.3), w / ((x1 - x0) * 1.25 || 1), h / ((y1 - y0) * 1.35 || 1));
    const mx = (x0 + x1) / 2, my = (y0 + y1) / 2;
    const P = (x, y) => [w / 2 + (x - mx) * sc, h / 2 + 10 - (y - my) * sc];
    const ink = css("--ink"), ink2 = css("--ink-2"), muted = css("--muted"), axis = css("--axis"), surf = css("--surface");
    ctx.save();
    ctx.beginPath(); ctx.rect(0, 0, w, h); ctx.clip();
    ctx.fillStyle = css("--surface-2"); ctx.fillRect(0, 0, w, h);
    // faint km grid
    const gstep = niceTicks(0, span / 4, 1).slice(-1)[0] || 200;
    ctx.strokeStyle = css("--grid"); ctx.lineWidth = 1;
    const [gx0, gy0] = [mx - w / 2 / sc, my - h / 2 / sc], [gx1, gy1] = [mx + w / 2 / sc, my + h / 2 / sc];
    for (let g = Math.ceil(gx0 / gstep) * gstep; g <= gx1; g += gstep) { const [px] = P(g, 0); ctx.beginPath(); ctx.moveTo(Math.round(px) + 0.5, 0); ctx.lineTo(Math.round(px) + 0.5, h); ctx.stroke(); }
    for (let g = Math.ceil(gy0 / gstep) * gstep; g <= gy1; g += gstep) { const [, py] = P(0, g); ctx.beginPath(); ctx.moveTo(0, Math.round(py) + 0.5); ctx.lineTo(w, Math.round(py) + 0.5); ctx.stroke(); }
    // elevation rings around the sub-satellite point (exact, from the server)
    const path = (pts) => { ctx.beginPath(); pts.forEach(([x, y], i) => (i ? ctx.lineTo(...P(x, y)) : ctx.moveTo(...P(x, y)))); ctx.closePath(); };
    const rings = f.rings || [];
    if (rings.length) { path(rings[0].pts); ctx.fillStyle = css("--s1-wash"); ctx.fill(); }
    ctx.font = axisFont; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for (const r of rings) {
      path(r.pts); ctx.strokeStyle = axis; ctx.lineWidth = 1; ctx.stroke();
      // label where the ring crosses the vertical through the satellite, above it
      let best = null;
      for (const [x, y] of r.pts) {
        const [px, py] = P(x, y);
        if (y > ss[1] && py > 14 && py < h - 14 && px > 20 && px < w - 20 && (!best || Math.abs(x - ss[0]) < Math.abs(best[0] - ss[0]))) best = [x, y, px, py];
      }
      if (best) {
        const lab = `${r.elev.toFixed(0)}°`;
        const tw = ctx.measureText(lab).width + 6;
        ctx.fillStyle = css("--surface-2"); ctx.fillRect(best[2] - tw / 2, best[3] - 7, tw, 14);
        ctx.fillStyle = muted; ctx.fillText(lab, best[2], best[3]);
      }
    }
    // node region
    ctx.setLineDash([4, 4]); ctx.strokeStyle = muted; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(...P(0, 0), f.cap_km * sc, 0, 2 * Math.PI); ctx.stroke();
    ctx.setLineDash([]);
    // ground track: flown part solid, still to come dashed
    const tr = f.track;
    let iNow = 0;
    tr.forEach(([x, y], i) => { if (Math.hypot(x - ss[0], y - ss[1]) < Math.hypot(tr[iNow][0] - ss[0], tr[iNow][1] - ss[1])) iNow = i; });
    ctx.strokeStyle = ink2; ctx.lineWidth = 1.5;
    ctx.beginPath(); tr.slice(0, iNow + 1).forEach(([x, y], i) => (i ? ctx.lineTo(...P(x, y)) : ctx.moveTo(...P(x, y)))); ctx.stroke();
    ctx.setLineDash([5, 5]);
    ctx.beginPath(); tr.slice(iNow).forEach(([x, y], i) => (i ? ctx.lineTo(...P(x, y)) : ctx.moveTo(...P(x, y)))); ctx.stroke();
    ctx.setLineDash([]);
    // satellite: body + panels across the track, arrow along it
    const [sx, sy] = P(ss[0], ss[1]);
    const nxt = tr[Math.min(tr.length - 1, iNow + 1)], prv = tr[Math.max(0, iNow - 1)];
    const [ax, ay] = P(nxt[0], nxt[1]), [bx, by] = P(prv[0], prv[1]);
    const ang = Math.atan2(ay - by, ax - bx);
    ctx.save(); ctx.translate(sx, sy); ctx.rotate(ang);
    ctx.fillStyle = css("--s1"); ctx.strokeStyle = surf; ctx.lineWidth = 1.5;
    for (const sgn of [-1, 1]) { ctx.beginPath(); ctx.rect(-4, sgn > 0 ? 6 : -16, 8, 10); ctx.fill(); ctx.stroke(); }
    ctx.fillStyle = ink; ctx.beginPath(); ctx.rect(-5, -5, 10, 10); ctx.fill(); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(14, 0); ctx.lineTo(24, 0); ctx.strokeStyle = ink; ctx.lineWidth = 1.5; ctx.stroke();
    ctx.beginPath(); ctx.moveTo(26, 0); ctx.lineTo(20, -4); ctx.lineTo(20, 4); ctx.closePath(); ctx.fillStyle = ink; ctx.fill();
    ctx.restore();
    ctx.font = axisFont; ctx.fillStyle = ink; ctx.textAlign = "center"; ctx.textBaseline = "top";
    const slab = "Satellite", sw = ctx.measureText(slab).width;
    ctx.fillText(slab, clamp(sx, sw / 2 + 4, w - sw / 2 - 4), sy + 20);
    // nodes
    const pts = [];
    const order = [...users].sort((a, b) => nodeSuccess(fr, b) - nodeSuccess(fr, a));   // failures on top
    for (const u of order) {
      const [x, y] = P(u.x_km, u.y_km);
      const lost = !(state.rx === "sic" ? u.sic_pkt : u.tin_pkt);
      const found = state.rx === "sic" ? u.det : u.tin_det;
      const hov = state.mapHover === u.id, sel = u.id === state.selUser;
      const r = hov ? 7.5 : 5.5;
      ctx.beginPath(); ctx.arc(x, y, r, 0, 2 * Math.PI);
      if (found) {
        ctx.fillStyle = successColor(nodeSuccess(fr, u)); ctx.lineWidth = 1.5; ctx.strokeStyle = surf; ctx.fill(); ctx.stroke();
      } else {
        ctx.fillStyle = surf; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = successColor(0); ctx.stroke();
      }
      if (lost && found) { ctx.beginPath(); ctx.arc(x, y, r + 2.5, 0, 2 * Math.PI); ctx.strokeStyle = ink; ctx.lineWidth = 1.5; ctx.stroke(); }
      if (sel) { ctx.beginPath(); ctx.arc(x, y, r + 6, 0, 2 * Math.PI); ctx.strokeStyle = ink; ctx.lineWidth = 2; ctx.stroke(); }
      pts.push({ u, x, y });
    }
    ctx.restore();
    // legend: success ramp, lost-packet ring
    const lg = { x: 12, y: 12, w: 120 };
    ctx.fillStyle = surf;
    ctx.beginPath(); ctx.roundRect(lg.x - 6, lg.y - 6, lg.w + 12, 82, 6); ctx.fill();
    ctx.font = axisFont; ctx.fillStyle = ink2; ctx.textAlign = "left"; ctx.textBaseline = "top";
    ctx.fillText(`${state.rx === "sic" ? "SIC" : "TIN"} symbols`, lg.x, lg.y);
    for (let i = 0; i < lg.w; i++) { ctx.fillStyle = successColor(i / (lg.w - 1)); ctx.fillRect(lg.x + i, lg.y + 17, 1, 8); }
    ctx.fillStyle = muted; ctx.textBaseline = "top";
    for (const v of [0, 0.5, 1]) { ctx.textAlign = v === 0 ? "left" : v === 1 ? "right" : "center"; ctx.fillText(`${v * 100}%`, lg.x + v * lg.w, lg.y + 28); }
    ctx.textAlign = "left";
    ctx.beginPath(); ctx.arc(lg.x + 5, lg.y + 50, 4, 0, 2 * Math.PI); ctx.fillStyle = successColor(0.5); ctx.fill();
    ctx.beginPath(); ctx.arc(lg.x + 5, lg.y + 50, 6.5, 0, 2 * Math.PI); ctx.strokeStyle = ink; ctx.lineWidth = 1.5; ctx.stroke();
    ctx.fillStyle = ink2; ctx.textBaseline = "middle"; ctx.fillText("Lost packet", lg.x + 16, lg.y + 50);
    ctx.beginPath(); ctx.arc(lg.x + 5, lg.y + 68, 4.5, 0, 2 * Math.PI); ctx.fillStyle = surf; ctx.fill();
    ctx.lineWidth = 2; ctx.strokeStyle = successColor(0); ctx.stroke();
    ctx.fillStyle = ink2; ctx.fillText("Not found", lg.x + 16, lg.y + 68);
    // scale bar
    const nice = niceTicks(0, span / 3, 1).slice(-1)[0] || 100;
    ctx.strokeStyle = ink2; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(12, h - 12); ctx.lineTo(12 + nice * sc, h - 12); ctx.stroke();
    ctx.fillStyle = ink2; ctx.textAlign = "left"; ctx.textBaseline = "bottom";
    ctx.fillText(`${nice} km`, 12, h - 15);
    mapGeom = pts;
  }

  function mapHit(e) {
    if (!mapGeom) return null;
    const r = $("#map").getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    let best = null;
    for (const p of mapGeom) {
      const dd = Math.hypot(p.x - mx, p.y - my);
      if (dd < 12 && (!best || dd < best.dd)) best = { dd, p };
    }
    return best ? best.p : null;
  }
  function wireMap() {
    const cv = $("#map"), tip = $("#tip3"), wrap = cv.parentElement;
    cv.addEventListener("click", (e) => {
      const p = mapHit(e);
      state.selUser = p && state.selUser !== p.u.id ? p.u.id : null;
      if (state.selUser !== null) {
        const blk = state.frame.blocks.find((b) => b.u === state.selUser && b.kind === "data");
        if (blk) { state.selBlock = blk.j; loadCorr(); }
      }
      drawMap(); drawSpec(); renderUsers();
    });
    cv.addEventListener("mousemove", (e) => {
      const p = mapHit(e), fr = state.frame;
      const id = p ? p.u.id : null;
      if (id !== state.mapHover) { state.mapHover = id; drawMap(); }
      cv.style.cursor = p ? "pointer" : "default";
      if (!p) { tip.hidden = true; return; }
      const u = p.u, D = fr.cfg.n_data;
      const pk = (ok) => `<span class="${ok ? "ok" : "no"}"></span>`;
      const html = `<div class="tt-h">Node ${u.id}</div>` +
        row("SIC", u.det ? `${u.sic_ok}/${D} ${pk(u.sic_pkt)}` : "not found") +
        row("TIN", u.tin_det ? `${u.tin_ok}/${D} ${pk(u.tin_pkt)}` : "not found") +
        row("SNR", `${fmt(u.snr_db, 1)} dB`) +
        row("Elevation", `${fmt(u.elev_deg, 1)}°`) + row("Slant range", `${fmt(u.slant_km, 0)} km`) +
        row("Doppler", `${fmt(u.doppler_hz / 1e3, 2)} kHz`) +
        row(fr.cfg.modulation === "lora" ? "Channel" : "Code", fr.cfg.modulation === "lora" ? u.ch : u.code);
      const r = cv.getBoundingClientRect();
      showTip(tip, wrap, e.clientX - r.left, e.clientY - r.top, html);
    });
    cv.addEventListener("mouseleave", () => { tip.hidden = true; if (state.mapHover !== null) { state.mapHover = null; drawMap(); } });
    // the map fills whatever height the neighbouring panels give it
    let lastH = 0;
    new ResizeObserver(() => {
      const hh = wrap.clientHeight;
      if (Math.abs(hh - lastH) > 1) { lastH = hh; requestAnimationFrame(drawMap); }
    }).observe(wrap);
  }

  // ------------------------------------------------------------------ users table
  function renderUsers() {
    const fr = state.frame;
    const tb = $("#users");
    if (!fr) { tb.innerHTML = ""; return; }
    const D = fr.cfg.n_data;
    const lora = fr.cfg.modulation === "lora";
    let html = `<thead><tr><th>Node</th><th>${lora ? "Chirp" : "Code"}</th><th>${lora ? "Channel" : "Hop"}</th><th>SNR dB</th><th>Elev °</th><th>Doppler kHz</th><th>LO Hz</th><th>CFO err Hz</th><th>Arrival µs</th><th>Found</th><th>SIC</th><th>SIC pkt</th><th>TIN</th><th>TIN pkt</th></tr></thead><tbody>`;
    for (const u of fr.users) {
      html += `<tr data-u="${u.id}" class="${u.id === state.selUser ? "sel" : ""}"><td>${u.id}</td><td>${lora ? "shared" : u.code}</td><td>${lora ? u.ch : u.hop}</td><td>${fmt(u.snr_db, 1)}</td><td>${fmt(u.elev_deg, 1)}</td><td>${fmt(u.doppler_hz / 1e3, 2)}</td><td>${fmt(u.osc_hz, 0)}</td><td>${fmt(u.cfo_err_hz, 0)}</td><td>${fmt(u.arrival_us, 0)}</td><td><span class="${u.det ? "ok" : "no"}"></span></td>` +
        `<td>${u.sic_ok}/${D}</td><td><span class="${u.sic_pkt ? "ok" : "no"}"></span></td><td>${u.tin_ok}/${D}</td><td><span class="${u.tin_pkt ? "ok" : "no"}"></span></td></tr>`;
    }
    tb.innerHTML = html + "</tbody>";
    $$("tbody tr", tb).forEach((tr) => tr.addEventListener("click", () => {
      const u = +tr.dataset.u;
      state.selUser = state.selUser === u ? null : u;
      if (state.selUser !== null) {
        const blk = fr.blocks.find((b) => b.u === u && b.kind === "data");
        if (blk) { state.selBlock = blk.j; loadCorr(); }
      }
      renderUsers(); drawSpec(); drawMap();
    }));
  }

  // ------------------------------------------------------------------ sweep
  function sweepKey(cfg) {
    const c = { ...cfg };
    delete c.n_users; delete c.seed;
    return JSON.stringify(c);
  }
  function markSweepStale() {
    const stale = state.sweep.rows.length && state.sweep.cfgKey !== sweepKey(readCfg());
    $("#sweep-stale").hidden = !stale;
    drawSweep();
  }

  async function runSweep() {
    if (state.sweep.running) return;
    const cfg = readCfg();
    const grid = $("#kgrid").value.split(/[,\s]+/).map((x) => parseInt(x, 10)).filter((x) => x >= 1 && x <= state.schema.k_max);
    const fpk = clamp(parseInt($("#fpk").value, 10) || 5, 1, 20);
    if (!grid.length) { setStatus("invalid K grid", true); return; }
    state.sweep = { rows: [], cfgKey: sweepKey(cfg), running: true };
    const btn = $("#sweep"), prog = $("#sweep-prog");
    btn.disabled = true; prog.hidden = false; prog.firstElementChild.style.width = "0%";
    markSweepStale();
    try {
      const res = await api("/api/sweep", { cfg, k_grid: grid, frames_per_k: fpk });
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
          if (!line.trim()) continue;
          const m = JSON.parse(line);
          if (m.error) throw new Error(m.error);
          if (m.progress !== undefined) {
            prog.firstElementChild.style.width = (m.progress * 100).toFixed(1) + "%";
            setStatus(`K = ${m.k} · ${m.trial + 1}/${fpk}`);
          }
          if (m.row) { state.sweep.rows.push(m.row); drawSweep(); renderSweepTable(); }
          if (m.done) setStatus(`${m.elapsed_s} s`);
        }
      }
    } catch (e) {
      setStatus(e.message, true);
    } finally {
      state.sweep.running = false;
      btn.disabled = false;
      setTimeout(() => { prog.hidden = true; }, 600);
    }
  }

  const SERIES = [
    { key: "sic_raw", label: "SIC symbols", col: "--s1", dash: false },
    { key: "tin_raw", label: "TIN symbols", col: "--s2", dash: false },
    { key: "sic_pkt", label: "SIC packets", col: "--s1", dash: true },
    { key: "tin_pkt", label: "TIN packets", col: "--s2", dash: true },
  ];
  function renderLegend() {
    let html = SERIES.map((s) => `<span><i class="${s.dash ? "dash" : ""}" style="border-color:var(${s.col})"></i>${s.label}</span>`).join("");
    $("#sweep-legend").innerHTML = html;
  }

  let sweepGeom = null;
  function drawSweep() {
    renderLegend();
    const cv = $("#sweepc");
    const { ctx, w, h } = setupCanvas(cv, 280);
    const rows = state.sweep.rows;
    const box = { x: 58, y: 12, w: w - 58 - 16, h: h - 12 - 40 };
    const ks = rows.map((r) => r.k);
    const gridK = $("#kgrid").value.split(/[,\s]+/).map((x) => parseInt(x, 10)).filter((x) => x >= 1);
    const kmin = Math.min(...(ks.length ? ks : gridK.length ? gridK : [1]));
    const kmax = Math.max(...(ks.length ? ks : gridK.length ? gridK : [20]), kmin + 1);
    const xs = { lo: kmin - 0.5, hi: kmax + 0.5 };
    xs.ticks = niceTicks(Math.max(1, kmin), kmax, Math.max(3, Math.floor(box.w / 70))).filter(Number.isInteger); xs.fmt = (v) => fmt(v, 0);
    const ys = { lo: 0, hi: 1.0, ticks: [0, 0.25, 0.5, 0.75, 1], fmt: (v) => (v * 100).toFixed(0) + "%" };
    drawAxes(ctx, box, xs, ys, { xlabel: "simultaneous transmissions K", ylabel: "recovered" });
    const X = (k) => box.x + ((k - xs.lo) / (xs.hi - xs.lo)) * box.w;
    const Y = (v) => box.y + box.h - v * box.h;
    // 90% reference
    ctx.strokeStyle = css("--muted"); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(box.x, Math.round(Y(0.9)) + 0.5); ctx.lineTo(box.x + box.w, Math.round(Y(0.9)) + 0.5); ctx.stroke();
    ctx.font = axisFont; ctx.fillStyle = css("--muted"); ctx.textAlign = "left"; ctx.textBaseline = "top";
    ctx.fillText("90%", box.x + 4, Y(0.9) + 3);
    if (!rows.length) {
      ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillStyle = css("--muted");
      ctx.fillText(state.sweep.running ? "Running…" : "No data", box.x + box.w / 2, box.y + box.h / 2);
    }
    const stale = rows.length && state.sweep.cfgKey !== sweepKey(readCfg());
    ctx.globalAlpha = stale ? 0.45 : 1;
    const surf = css("--surface");
    for (const s of SERIES) {
      const col = css(s.col);
      ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.lineCap = "round";
      ctx.setLineDash(s.dash ? [6, 4] : []);
      ctx.beginPath();
      rows.forEach((r, i) => (i ? ctx.lineTo(X(r.k), Y(r[s.key])) : ctx.moveTo(X(r.k), Y(r[s.key]))));
      ctx.stroke();
      ctx.setLineDash([]);
      if (!s.dash) for (const r of rows) {
        ctx.beginPath(); ctx.arc(X(r.k), Y(r[s.key]), 4, 0, 2 * Math.PI);
        ctx.fillStyle = col; ctx.lineWidth = 2; ctx.strokeStyle = surf; ctx.stroke(); ctx.fill();
      }
    }
    ctx.globalAlpha = 1;
    // hover crosshair
    if (state.hover && rows.length) {
      const r = rows[state.hover.i];
      ctx.strokeStyle = css("--axis"); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(Math.round(X(r.k)) + 0.5, box.y); ctx.lineTo(Math.round(X(r.k)) + 0.5, box.y + box.h); ctx.stroke();
    }
    sweepGeom = { X, box };
  }

  function wireSweep() {
    const cv = $("#sweepc"), tip = $("#tip2"), wrap = cv.parentElement;
    cv.addEventListener("mousemove", (e) => {
      const rows = state.sweep.rows;
      if (!rows.length || !sweepGeom) return;
      const r = cv.getBoundingClientRect();
      const mx = e.clientX - r.left;
      let bi = 0;
      rows.forEach((row, i) => { if (Math.abs(sweepGeom.X(row.k) - mx) < Math.abs(sweepGeom.X(rows[bi].k) - mx)) bi = i; });
      state.hover = { i: bi };
      drawSweep();
      const rw = rows[bi];
      let html = `<div class="tt-h">K = ${rw.k}</div>`;
      for (const s of SERIES) html += `<div class="row"><span><i class="sw" style="background:var(${s.col})"></i>${s.label}</span><b>${pct(rw[s.key])}</b></div>`;
      showTip(tip, wrap, sweepGeom.X(rw.k), e.clientY - r.top, html);
    });
    cv.addEventListener("mouseleave", () => { state.hover = null; tip.hidden = true; drawSweep(); });
  }

  function renderSweepTable() {
    const rows = state.sweep.rows;
    $("#sweep-table").innerHTML = `<thead><tr><th>K</th><th>SIC symbols</th><th>TIN symbols</th><th>SIC packets</th><th>TIN packets</th><th>Symbols</th></tr></thead><tbody>` +
      rows.map((r) => `<tr><td>${r.k}</td><td>${pct(r.sic_raw)}</td><td>${pct(r.tin_raw)}</td><td>${pct(r.sic_pkt)}</td><td>${pct(r.tin_pkt)}</td><td>${r.sic_sym_total}</td></tr>`).join("") + "</tbody>";
  }

  // ------------------------------------------------------------------ init
  async function init() {
    try {
      state.schema = await (await api("/api/schema")).json();
    } catch (e) {
      setStatus("API unreachable: " + e.message, true);
      return;
    }
    buildKnobs(state.schema);
    wireSpec(); wireMap(); wireSweep();
    $("#run").addEventListener("click", () => runFrame());
    $("#reseed").addEventListener("click", () => {
      setKnob("seed", Math.floor(Math.random() * 1e6));
      runFrame();
    });
    $("#reset").addEventListener("click", () => loadDefaults(true));
    $("#autorun").addEventListener("change", (e) => { if (e.target.checked) runFrame(); });
    $("#stage").addEventListener("input", (e) => {
      stopPlay();
      state.stage = +e.target.value;
      if (state.rx !== "sic") setRx("sic");
      updateStageLabel();
      fetchStageDebounced(state.stage);
    });
    $("#to-rx").addEventListener("click", () => { stopPlay(); state.stage = 0; $("#stage").value = 0; fetchStage(0); });
    $("#to-res").addEventListener("click", () => {
      if (!state.frame) return;
      stopPlay(); state.stage = state.frame.n_steps; $("#stage").value = state.stage; fetchStage(state.stage);
    });
    $("#play").addEventListener("click", play);
    $("#dyn").addEventListener("input", (e) => { state.dyn = +e.target.value; $("#dyn-val").textContent = state.dyn + " dB"; drawSpec(); });
    $$("#rx-seg button").forEach((b) => b.addEventListener("click", () => setRx(b.dataset.rx)));
    $$("#view-seg button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
    $("#sweep").addEventListener("click", runSweep);
    $("#kgrid").addEventListener("change", () => drawSweep());
    $("#theme").addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("codehop-theme", next); } catch (e) { /* ignore */ }
      renderAll();
    });
    let lastW = 0;
    const ro = new ResizeObserver(debounce(() => {
      const w = $(".main").clientWidth;
      if (w !== lastW) { lastW = w; renderAll(); }
    }, 120));
    ro.observe($(".main"));
    $("#kgrid").value = state.schema.sweep_grid.join(", ");
    loadDefaults(true);
  }
  init();
})();
