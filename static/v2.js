/* =====================================================================
   ROM Annotator - 5-point rig-referenced mode ("v2_malleolus").

   Separate from the 3-point tool: separate page, separate script,
   separate API namespace, separate output file. Nothing here touches the
   old mode.

   THE ONE IDEA THAT SHAPES THIS FILE
     bottom_heel is not a free point. It is stored as a single scalar,
     state.travel: how far DOWN the rig direction it sits from the
     malleolus. Its x/y are derived from that scalar on every paint. The
     constraint therefore cannot be bypassed by dragging, nudging, or by
     moving the malleolus or a rig point afterwards - there is no code
     path that can put the heel off the construction line, because an
     off-line position is not representable.

   COORDINATE SPACES
     state.pts and state.travel are in FULL upright image pixels, the same
     frame the server writes to the CSV. Screen coordinates are only ever
     produced for drawing, by multiplying by state.scale.
   ===================================================================== */
(function () {
  "use strict";

  var LOUPE_SIZE = 190;          // px on screen
  var LOUPE_ZOOM = 5;            // magnification relative to the displayed scale
  var HIT_RADIUS = 14;           // screen px to grab a placed dot
  var NUDGE_SMALL = 1;
  var NUDGE_LARGE = 10;

  var el = {
    side: document.getElementById("sideSelect"),
    load: document.getElementById("loadBtn"),
    queueInfo: document.getElementById("queueInfo"),
    zoomToggle: document.getElementById("zoomToggle"),
    zoomForced: document.getElementById("zoomForced"),
    rayToggle: document.getElementById("rayToggle"),
    suggestToggle: document.getElementById("suggestToggle"),
    suggestState: document.getElementById("suggestState"),
    prev: document.getElementById("prevBtn"),
    next: document.getElementById("nextBtn"),
    stage: document.getElementById("stage"),
    canvas: document.getElementById("canvas"),
    empty: document.getElementById("emptyState"),
    progressText: document.getElementById("progressText"),
    progressFill: document.getElementById("progressFill"),
    filename: document.getElementById("filenameText"),
    dims: document.getElementById("dimsText"),
    landmarkList: document.getElementById("landmarkList"),
    nextHint: document.getElementById("nextHint"),
    angleReadout: document.getElementById("angleReadout"),
    metricGrid: document.getElementById("metricGrid"),
    budgetTable: document.getElementById("budgetTable"),
    budgetNote: document.getElementById("budgetNote"),
    subjectId: document.getElementById("subjectId"),
    pairId: document.getElementById("pairId"),
    phase: document.getElementById("phase"),
    screenAngle: document.getElementById("screenAngle"),
    notes: document.getElementById("notesField"),
    stickyMeta: document.getElementById("stickyMeta"),
    vocSubject: document.getElementById("vocSubject"),
    vocPair: document.getElementById("vocPair"),
    vocPhase: document.getElementById("vocPhase"),
    save: document.getElementById("saveBtn"),
    reset: document.getElementById("resetBtn"),
    undo: document.getElementById("undoBtn"),
    status: document.getElementById("status"),
    pathInfo: document.getElementById("pathInfo"),
    warnModal: document.getElementById("warnModal"),
    warnList: document.getElementById("warnList"),
    warnTitle: document.getElementById("warnTitle"),
    warnNote: document.getElementById("warnNote"),
    warnCancel: document.getElementById("warnCancel"),
    warnConfirm: document.getElementById("warnConfirm")
  };

  var state = {
    cfg: null,
    side: "left",
    queue: [],
    index: 0,
    total: 0,
    doneBefore: 0,
    known: {},
    knownAtLoad: {},   // rows that already existed when the queue was loaded
    saved: {},         // images actually written during this session
    suggested: {},     // keys placed by the model, not by the annotator
    suggestToken: 0,   // guards against a reply arriving for a previous image
    suggestAbort: null,   // AbortController for the request in flight
    loadToken: 0,      // guards against image loads resolving out of order
    img: null,
    fullW: 0,
    fullH: 0,
    dispW: 0,
    dispH: 0,
    scale: 1,
    pts: {},            // key -> {x, y} in FULL image px (bottom_heel excluded)
    travel: null,       // bottom_heel: distance DOWN the rig from the malleolus
    order: [],          // keys in the order they were placed, for Undo
    selected: null,
    dragging: null,
    cursor: null,       // {x, y} in screen px
    pendingSave: null,
    lastSave: null      // the save confirmation, re-shown after the next image loads
  };

  var buffer = document.createElement("canvas");
  var bctx = buffer.getContext("2d");
  var ctx = el.canvas.getContext("2d");

  // -------------------------------------------------------------------
  // Small helpers
  // -------------------------------------------------------------------
  function setStatus(msg, kind) {
    el.status.textContent = msg || "";
    el.status.className = "status" + (kind ? " " + kind : "");
  }

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  function clickOrder() { return state.cfg ? state.cfg.click_order : []; }

  function meta(key) {
    var list = state.cfg ? state.cfg.landmarks : [];
    for (var i = 0; i < list.length; i++) if (list[i].key === key) return list[i];
    return null;
  }

  function isCritical(key) {
    var m = meta(key);
    return !!m && m.precision === "critical";
  }

  function currentName() { return state.queue[state.index] || null; }

  // -------------------------------------------------------------------
  // Geometry - the client twin of scheme_v2.py. Same formulas, same signs.
  // -------------------------------------------------------------------
  function signedAngle(ax, ay, bx, by) {
    return Math.atan2(ax * by - ay * bx, ax * bx + ay * by) * 180 / Math.PI;
  }

  function includedAngle(vx, vy, wx, wy) {
    return Math.atan2(Math.abs(vx * wy - vy * wx), vx * wx + vy * wy) * 180 / Math.PI;
  }

  /* Unit vector of the rig's "down" direction, or null until both rig points exist. */
  function rigDir() {
    var t = state.pts.rig_top, b = state.pts.rig_bottom;
    if (!t || !b) return null;
    var dx = b.x - t.x, dy = b.y - t.y;
    var n = Math.hypot(dx, dy);
    if (n < 1e-9) return null;
    return { x: dx / n, y: dy / n, sep: n };
  }

  /* bottom_heel, DERIVED from state.travel. Never stored as a free point. */
  function heelPoint() {
    var r = rigDir(), m = state.pts.malleolus;
    if (!r || !m || state.travel === null) return null;
    return { x: m.x + r.x * state.travel, y: m.y + r.y * state.travel };
  }

  /* Every point including the derived heel, or null if any is missing. */
  function allPoints() {
    var heel = heelPoint();
    if (!heel) return null;
    var out = { bottom_heel: heel };
    var keys = ["5th_metatarsal", "malleolus", "rig_top", "rig_bottom"];
    for (var i = 0; i < keys.length; i++) {
      if (!state.pts[keys[i]]) return null;
      out[keys[i]] = state.pts[keys[i]];
    }
    return out;
  }

  function placed(key) {
    return key === "bottom_heel" ? heelPoint() !== null : !!state.pts[key];
  }

  function pointOf(key) {
    return key === "bottom_heel" ? heelPoint() : (state.pts[key] || null);
  }

  function nextKey() {
    var order = clickOrder();
    for (var i = 0; i < order.length; i++) if (!placed(order[i])) return order[i];
    return null;
  }

  function derive() {
    var p = allPoints();
    if (!p) return null;
    var rx = p.rig_bottom.x - p.rig_top.x, ry = p.rig_bottom.y - p.rig_top.y;
    var fx = p["5th_metatarsal"].x - p.bottom_heel.x;
    var fy = p["5th_metatarsal"].y - p.bottom_heel.y;
    var lx = p.malleolus.x - p.bottom_heel.x, ly = p.malleolus.y - p.bottom_heel.y;
    var mx = p.bottom_heel.x - p.malleolus.x, my = p.bottom_heel.y - p.malleolus.y;
    return {
      angle_deg: includedAngle(fx, fy, lx, ly),
      rig_tilt_deg: signedAngle(0, 1, rx, ry),
      parallel_dev_deg: signedAngle(rx, ry, mx, my),
      foot_len_px: Math.hypot(fx, fy),
      leg_len_px: Math.hypot(lx, ly),
      rig_sep_px: Math.hypot(rx, ry),
      mt_vertical_px: p["5th_metatarsal"].y - p.bottom_heel.y
    };
  }

  function rigUncertainty(sep) {
    if (!sep || sep <= 0) return Infinity;
    var px = state.cfg ? state.cfg.limits.click_error_px : 3;
    return (px * Math.SQRT2 / sep) * 180 / Math.PI;
  }

  function pointUncertainty(footLen) {
    if (!footLen || footLen <= 0) return Infinity;
    var px = state.cfg ? state.cfg.limits.budget_error_px : 5;
    return (px / footLen) * 180 / Math.PI;
  }

  // -------------------------------------------------------------------
  // Placement. Every write to a point funnels through here, so the
  // construction constraint is applied in exactly one place.
  // -------------------------------------------------------------------
  function setPoint(key, fx, fy) {
    fx = clamp(fx, 0, state.fullW);
    fy = clamp(fy, 0, state.fullH);
    if (key === "bottom_heel") {
      var r = rigDir(), m = state.pts.malleolus;
      if (!r || !m) return false;
      // Project onto the construction ray: the only free parameter is how
      // far down the rig direction the sole is.
      var t = (fx - m.x) * r.x + (fy - m.y) * r.y;
      state.travel = t;
      return true;
    }
    state.pts[key] = { x: fx, y: fy };
    return true;
  }

  /* The annotator has taken ownership of this point; it is no longer a guess. */
  function markUserEdited(key) {
    if (state.suggested[key]) {
      delete state.suggested[key];
      setSuggestState("malleolus adjusted by hand", "ok");
    }
  }

  function place(key, fx, fy) {
    if (!setPoint(key, fx, fy)) return;
    if (state.order.indexOf(key) === -1) state.order.push(key);
    state.selected = key;
  }

  function clearPoints() {
    state.pts = {};
    state.suggested = {};
    state.travel = null;
    state.order = [];
    state.selected = null;
  }

  function undoLast() {
    if (!state.order.length) return;
    var key = state.order.pop();
    if (key === "bottom_heel") state.travel = null;
    else delete state.pts[key];
    // The heel hangs off the malleolus and the rig line; if either goes, so
    // does the heel, or it would silently mean something different.
    if ((key === "malleolus" || key === "rig_top" || key === "rig_bottom")
        && state.travel !== null) {
      state.travel = null;
      var at = state.order.indexOf("bottom_heel");
      if (at !== -1) state.order.splice(at, 1);
    }
    state.selected = state.order.length ? state.order[state.order.length - 1] : null;
    refresh();
  }

  // -------------------------------------------------------------------
  // Config
  // -------------------------------------------------------------------
  fetch("/api/v2/config")
    .then(function (r) { return r.json(); })
    .then(function (cfg) {
      state.cfg = cfg;
      el.pathInfo.textContent = "scheme " + cfg.scheme_version + "  •  master: " + cfg.master;
      renderLandmarkList();
      refresh();
    })
    .catch(function (err) { setStatus("Could not load config: " + err, "err"); });

  function renderLandmarkList() {
    var order = clickOrder();
    el.landmarkList.innerHTML = "";
    order.forEach(function (key) {
      var m = meta(key) || { color: "#888", hint: "", precision: "relaxed" };
      var li = document.createElement("li");
      li.className = m.precision;
      li.dataset.key = key;
      var dot = document.createElement("span");
      dot.className = "dot";
      dot.style.background = m.color;
      var name = document.createElement("span");
      name.className = "lm-name";
      name.textContent = key;
      var hint = document.createElement("small");
      hint.className = "lm-hint";
      hint.textContent = m.hint;
      li.appendChild(dot);
      li.appendChild(name);
      li.appendChild(hint);
      li.addEventListener("click", function () {
        if (placed(key)) { state.selected = key; refresh(); }
      });
      el.landmarkList.appendChild(li);
    });
  }

  function renderLandmarkState() {
    var nk = nextKey();
    Array.prototype.forEach.call(el.landmarkList.children, function (li) {
      var key = li.dataset.key;
      li.classList.toggle("done", placed(key));
      li.classList.toggle("active", key === nk);
      li.classList.toggle("selected", key === state.selected);
      li.classList.toggle("suggested", !!state.suggested[key]);
    });
    if (nk) {
      var m = meta(nk);
      el.nextHint.textContent = "Next: click " + nk
        + (m && m.precision === "critical" ? "  — zoom panel is forced on" : "");
    } else {
      el.nextHint.textContent = state.selected
        ? "All five placed. Selected: " + state.selected + " — arrows nudge it."
        : "All five placed. Click a dot to adjust it.";
    }
    var forced = forcedZoomKey();
    el.zoomForced.textContent = forced ? "forced on for " + forced : "";
  }

  /* Which critical point currently demands the zoom panel. */
  function forcedZoomKey() {
    if (state.dragging && isCritical(state.dragging)) return state.dragging;
    var nk = nextKey();
    if (nk && isCritical(nk)) return nk;
    if (!nk && state.selected && isCritical(state.selected)) return state.selected;
    return null;
  }

  function zoomVisible() {
    return !!forcedZoomKey() || el.zoomToggle.checked;
  }

  function rayVisible() { return el.rayToggle.checked; }

  // -------------------------------------------------------------------
  // Readouts
  // -------------------------------------------------------------------
  function row(k, v, cls) {
    return '<span class="k">' + k + '</span><span class="v ' + (cls || "") + '">' + v + "</span>";
  }

  function renderMetrics() {
    var lim = state.cfg ? state.cfg.limits : null;
    var d = derive();
    var r = rigDir();
    var html = "";

    if (d) {
      el.angleReadout.textContent = "angle_deg " + d.angle_deg.toFixed(2) + "°";
      el.angleReadout.className = "angle-readout live";
    } else {
      el.angleReadout.innerHTML = "&mdash;";
      el.angleReadout.className = "angle-readout";
    }

    // Rig separation is shown as soon as both rig points exist, which is
    // while they are still the thing being adjusted.
    if (r && lim) {
      var sep = r.sep, unc = rigUncertainty(sep);
      // Judged as a fraction of frame height, matching the server: the wooden
      // edge is ~0.287 of the frame at both capture resolutions.
      var frac = state.fullH ? sep / state.fullH : 0;
      var sepCls = frac < lim.rig_sep_refuse_frac ? "bad"
                 : ((lim.rig_sep_warn_frac > 0 && frac < lim.rig_sep_warn_frac)
                    ? "warn" : "");
      html += row("rig separation", sep.toFixed(0) + " px ("
                  + (100 * frac).toFixed(0) + "% of frame) ±"
                  + unc.toFixed(3) + "°", sepCls);
    }
    if (d && lim) {
      var tiltCls = Math.abs(d.rig_tilt_deg) > lim.rig_tilt_warn_deg ? "warn" : "";
      html += row("rig_tilt_deg", (d.rig_tilt_deg >= 0 ? "+" : "") + d.rig_tilt_deg.toFixed(3) + "°", tiltCls);
      var devCls = Math.abs(d.parallel_dev_deg) > lim.parallel_dev_max_deg ? "bad" : "good";
      html += row("parallel_dev_deg", (d.parallel_dev_deg >= 0 ? "+" : "") + d.parallel_dev_deg.toFixed(4) + "°", devCls);
      html += row("foot_len_px", d.foot_len_px.toFixed(1), "");
      html += row("leg_len_px", d.leg_len_px.toFixed(1), "");
      // Correction 2: the VERTICAL offset of 5th_metatarsal is what carries
      // the error, so it gets its own live readout.
      html += row("5th MT vertical", (d.mt_vertical_px >= 0 ? "+" : "")
                  + d.mt_vertical_px.toFixed(1) + " px from heel", "");
    } else if (state.travel !== null) {
      html += row("heel travel", state.travel.toFixed(1) + " px down the rig",
                  state.travel <= 0 ? "bad" : "");
    }
    el.metricGrid.innerHTML = html;
    renderBudget(d, r);
  }

  function renderBudget(d, r) {
    if (!state.cfg) return;
    var lim = state.cfg.limits;
    var sep = r ? r.sep : null;
    var foot = d ? d.foot_len_px : null;
    if (!sep && !foot) {
      el.budgetTable.innerHTML = "";
      el.budgetNote.textContent = "Place the rig points and the foot to see the budget.";
      return;
    }

    var rigTerm = sep ? rigUncertainty(sep) : null;
    var soleTerm = foot ? pointUncertainty(foot) : null;
    var mtTerm = soleTerm;
    var total = null;
    if (rigTerm !== null && soleTerm !== null) {
      total = Math.sqrt(rigTerm * rigTerm + soleTerm * soleTerm + mtTerm * mtTerm);
    }

    function deg(v) { return v === null ? "&mdash;" : "±" + v.toFixed(3) + "°"; }
    function cls(v, warnAt, badAt) {
      if (v === null) return "";
      return v >= badAt ? "bad" : (v >= warnAt ? "warn" : "good");
    }

    var rows = [
      ["rig separation", sep ? sep.toFixed(0) + " px" : "&mdash;", rigTerm, 0.40, 0.60, ""],
      ["sole level", lim.budget_error_px + " px", soleTerm, 0.50, 0.80, ""],
      ["5th MT vertical", lim.budget_error_px + " px", mtTerm, 0.50, 0.80, ""]
    ];
    var html = "";
    rows.forEach(function (t) {
      html += "<tr><td class='k'>" + t[0] + "</td><td class='mid'>" + t[1]
            + "</td><td class='v " + cls(t[2], t[3], t[4]) + "'>" + deg(t[2]) + "</td></tr>";
    });
    html += "<tr class='total'><td class='k'>estimated total (RSS)</td><td class='mid'></td>"
          + "<td class='v " + cls(total, 0.70, 1.00) + "'>" + deg(total) + "</td></tr>";
    el.budgetTable.innerHTML = html;
    el.budgetNote.textContent = "Rig term assumes a " + lim.click_error_px
      + " px click error on each rig point; the other two assume "
      + lim.budget_error_px + " px of placement error.";
  }

  /* Images written this session that were not already on record at load. */
  function countSaved() {
    var n = 0;
    for (var name in state.saved) {
      if (state.saved.hasOwnProperty(name) && !state.knownAtLoad[name]) n += 1;
    }
    return n;
  }

  function renderProgress() {
    if (!state.queue.length || state.index >= state.queue.length) {
      var done = state.queue.length ? state.doneBefore + countSaved() : 0;
      el.progressText.innerHTML = state.queue.length ? "Queue complete" : "&mdash;";
      el.progressFill.style.width = state.queue.length
        ? (100 * Math.min(done, state.total) / Math.max(1, state.total)) + "%" : "0%";
      el.filename.textContent = "";
      el.dims.textContent = "";
      return;
    }
    var done = state.doneBefore + countSaved();
    el.progressText.textContent = "Image " + (state.index + 1) + " of " + state.queue.length
      + "   •   " + done + " / " + state.total + " done in folder";
    el.progressFill.style.width = (100 * done / Math.max(1, state.total)) + "%";
    el.filename.textContent = currentName() || "";
    el.dims.textContent = state.fullW ? state.fullW + " × " + state.fullH + " px upright" : "";
  }

  function refresh() {
    renderLandmarkState();
    renderMetrics();
    renderProgress();
    renderNav();
    el.save.disabled = !allPoints();
    schedulePaint();
  }

  // -------------------------------------------------------------------
  // Canvas
  // -------------------------------------------------------------------
  function fitCanvas() {
    if (!state.img) return;
    var pad = 16;
    var availW = el.stage.clientWidth - pad * 2;
    var availH = el.stage.clientHeight - pad * 2;
    var ratio = Math.min(availW / state.fullW, availH / state.fullH);
    if (!isFinite(ratio) || ratio <= 0) ratio = 0.1;
    state.scale = ratio;
    state.dispW = Math.max(1, Math.round(state.fullW * ratio));
    state.dispH = Math.max(1, Math.round(state.fullH * ratio));
    var dpr = window.devicePixelRatio || 1;
    [el.canvas, buffer].forEach(function (c) {
      c.width = Math.round(state.dispW * dpr);
      c.height = Math.round(state.dispH * dpr);
    });
    el.canvas.style.width = state.dispW + "px";
    el.canvas.style.height = state.dispH + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    bctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function toScreen(p) { return { x: p.x * state.scale, y: p.y * state.scale }; }

  function strokeLine(c, x1, y1, x2, y2, colour, w, dash) {
    c.save();
    c.setLineDash(dash || []);
    c.lineCap = "round";
    c.strokeStyle = "rgba(0,0,0,.85)";
    c.lineWidth = w + 2.5;
    c.beginPath(); c.moveTo(x1, y1); c.lineTo(x2, y2); c.stroke();
    c.strokeStyle = colour;
    c.lineWidth = w;
    c.beginPath(); c.moveTo(x1, y1); c.lineTo(x2, y2); c.stroke();
    c.restore();
  }

  /* Where the construction ray leaves the image, for drawing it full-length. */
  function constructionSpan() {
    var r = rigDir(), m = state.pts.malleolus;
    if (!r || !m) return null;
    var ts = [];
    if (Math.abs(r.x) > 1e-9) { ts.push(-m.x / r.x, (state.fullW - m.x) / r.x); }
    if (Math.abs(r.y) > 1e-9) { ts.push(-m.y / r.y, (state.fullH - m.y) / r.y); }
    if (!ts.length) return null;
    var lo = Math.max.apply(null, ts.filter(function (t) { return t <= 0; }).concat([-1e9]));
    var hi = Math.min.apply(null, ts.filter(function (t) { return t >= 0; }).concat([1e9]));
    return {
      a: { x: m.x + r.x * lo, y: m.y + r.y * lo },
      b: { x: m.x + r.x * hi, y: m.y + r.y * hi }
    };
  }

  function drawOverlay(c) {
    var colors = state.cfg.colors;
    var p = state.pts;
    var heel = heelPoint();

    // rig line
    if (p.rig_top && p.rig_bottom) {
      var a = toScreen(p.rig_top), b = toScreen(p.rig_bottom);
      strokeLine(c, a.x, a.y, b.x, b.y, colors.rig, 2.5);
    }

    // Construction ray from the malleolus, dashed - it is a construction,
    // not a measurement, so it must not read like an axis. Hiding it is
    // purely cosmetic: bottom_heel is stored as a distance along this line,
    // so the constraint holds whether or not the line is drawn, and the ray
    // is never burned into the export either way.
    var span = rayVisible() ? constructionSpan() : null;
    if (span) {
      var s1 = toScreen(span.a), s2 = toScreen(span.b);
      strokeLine(c, s1.x, s1.y, s2.x, s2.y, colors.construction, 1.5, [7, 7]);
    }

    // the two axes
    if (heel && p["5th_metatarsal"]) {
      var h = toScreen(heel), m5 = toScreen(p["5th_metatarsal"]);
      strokeLine(c, h.x, h.y, m5.x, m5.y, colors.foot_axis, 2.5);
    }
    if (heel && p.malleolus) {
      var h2 = toScreen(heel), ml = toScreen(p.malleolus);
      strokeLine(c, h2.x, h2.y, ml.x, ml.y, colors.leg_axis, 2.5);
    }

    drawArc(c);

    clickOrder().forEach(function (key) {
      var pt = pointOf(key);
      if (!pt) return;
      drawDot(c, toScreen(pt), meta(key).color, key === state.selected,
              !!state.suggested[key]);
    });
  }

  function drawArc(c) {
    var d = derive();
    if (!d) return;
    var p = allPoints();
    var h = toScreen(p.bottom_heel);
    var r = 52;
    var aFoot = Math.atan2(p["5th_metatarsal"].y - p.bottom_heel.y,
                           p["5th_metatarsal"].x - p.bottom_heel.x);
    var aLeg = Math.atan2(p.malleolus.y - p.bottom_heel.y,
                          p.malleolus.x - p.bottom_heel.x);
    var sweep = ((aLeg - aFoot + Math.PI * 3) % (Math.PI * 2)) - Math.PI;
    c.save();
    c.lineWidth = 4;
    c.strokeStyle = "rgba(0,0,0,.85)";
    c.beginPath(); c.arc(h.x, h.y, r, aFoot, aFoot + sweep, sweep < 0); c.stroke();
    c.lineWidth = 2.5;
    c.strokeStyle = state.cfg.colors.arc;
    c.beginPath(); c.arc(h.x, h.y, r, aFoot, aFoot + sweep, sweep < 0); c.stroke();

    var mid = aFoot + sweep / 2;
    var tx = h.x + Math.cos(mid) * r * 1.7, ty = h.y + Math.sin(mid) * r * 1.7;
    c.font = "600 15px 'Segoe UI', system-ui, sans-serif";
    c.textAlign = "center";
    c.textBaseline = "middle";
    c.lineWidth = 4;
    c.strokeStyle = "rgba(0,0,0,.9)";
    c.strokeText(d.angle_deg.toFixed(1) + "°", tx, ty);
    c.fillStyle = "#fff";
    c.fillText(d.angle_deg.toFixed(1) + "°", tx, ty);
    c.restore();
  }

  function drawDot(c, s, colour, selected, suggested) {
    c.save();
    c.lineWidth = 1.5;
    c.strokeStyle = "rgba(0,0,0,.85)";
    c.beginPath(); c.moveTo(s.x - 11, s.y); c.lineTo(s.x + 11, s.y);
    c.moveTo(s.x, s.y - 11); c.lineTo(s.x, s.y + 11); c.stroke();
    c.strokeStyle = colour;
    c.beginPath(); c.moveTo(s.x - 11, s.y); c.lineTo(s.x + 11, s.y);
    c.moveTo(s.x, s.y - 11); c.lineTo(s.x, s.y + 11); c.stroke();
    c.beginPath();
    c.arc(s.x, s.y, 5, 0, Math.PI * 2);
    c.fillStyle = colour;
    c.fill();
    c.lineWidth = selected ? 3 : 1.5;
    c.strokeStyle = selected ? "#fff" : "rgba(255,255,255,.85)";
    c.stroke();
    if (selected) {
      c.beginPath();
      c.arc(s.x, s.y, 13, 0, Math.PI * 2);
      c.setLineDash([3, 4]);
      c.lineWidth = 1.5;
      c.strokeStyle = "#4c8dff";
      c.stroke();
    }
    if (suggested) {
      c.beginPath();
      c.arc(s.x, s.y, 18, 0, Math.PI * 2);
      c.setLineDash([2, 5]);
      c.lineWidth = 2;
      c.strokeStyle = "#e8b23a";
      c.stroke();
    }
    c.restore();
  }

  function drawLoupe(c) {
    if (!zoomVisible()) return;
    var key = forcedZoomKey() || state.selected || nextKey();
    var focus = null;

    // While placing or dragging, magnify where the point WILL land - for
    // bottom_heel that is the constrained position, not the raw cursor.
    if (state.cursor) {
      var fx = state.cursor.x / state.scale, fy = state.cursor.y / state.scale;
      if (key === "bottom_heel") {
        var r = rigDir(), m = state.pts.malleolus;
        if (r && m) {
          var t = (fx - m.x) * r.x + (fy - m.y) * r.y;
          focus = { x: m.x + r.x * t, y: m.y + r.y * t };
        }
      }
      if (!focus) focus = { x: fx, y: fy };
    } else if (key && pointOf(key)) {
      focus = pointOf(key);
    }
    if (!focus) return;

    var cx = focus.x * state.scale, cy = focus.y * state.scale;
    var half = LOUPE_SIZE / 2;
    var srcHalf = half / LOUPE_ZOOM;
    var lx = cx + 26, ly = cy + 26;
    if (lx + LOUPE_SIZE > state.dispW) lx = cx - 26 - LOUPE_SIZE;
    if (ly + LOUPE_SIZE > state.dispH) ly = cy - 26 - LOUPE_SIZE;
    lx = clamp(lx, 0, Math.max(0, state.dispW - LOUPE_SIZE));
    ly = clamp(ly, 0, Math.max(0, state.dispH - LOUPE_SIZE));

    var originX = cx - srcHalf, originY = cy - srcHalf;

    c.save();
    c.beginPath();
    c.rect(lx, ly, LOUPE_SIZE, LOUPE_SIZE);
    c.clip();
    c.fillStyle = "#000";
    c.fillRect(lx, ly, LOUPE_SIZE, LOUPE_SIZE);
    c.imageSmoothingEnabled = false;
    // Sample the PREVIEW bitmap. state.img is the downscaled preview the server
    // sent, not the full-resolution frame, so screen px convert with the
    // preview's own ratio. Dividing by state.scale would address full-image
    // pixels this bitmap does not contain, and the loupe renders black.
    var pv = state.img.naturalWidth / state.dispW;     // preview px per screen px
    c.drawImage(state.img,
      originX * pv, originY * pv, srcHalf * 2 * pv, srcHalf * 2 * pv,
      lx, ly, LOUPE_SIZE, LOUPE_SIZE);

    function L(p) {
      return { x: lx + (p.x * state.scale - originX) * LOUPE_ZOOM,
               y: ly + (p.y * state.scale - originY) * LOUPE_ZOOM };
    }

    // the construction ray inside the loupe, so the sole level can be judged
    // against the line it must sit on
    var span = rayVisible() ? constructionSpan() : null;
    if (span) {
      var a = L(span.a), b = L(span.b);
      strokeLine(c, a.x, a.y, b.x, b.y, state.cfg.colors.construction, 1.2, [6, 6]);
    }
    // a horizontal rule at the heel level, the reference for 5th MT vertical
    var heel = heelPoint();
    if (heel && key === "5th_metatarsal") {
      var hy = L(heel).y;
      strokeLine(c, lx, hy, lx + LOUPE_SIZE, hy, state.cfg.colors.foot_axis, 1.2, [5, 5]);
    }
    clickOrder().forEach(function (k) {
      var pt = pointOf(k);
      if (!pt) return;
      var s = L(pt);
      if (s.x < lx - 20 || s.x > lx + LOUPE_SIZE + 20) return;
      c.beginPath();
      c.arc(s.x, s.y, 4, 0, Math.PI * 2);
      c.fillStyle = meta(k).color;
      c.fill();
      c.lineWidth = 1;
      c.strokeStyle = "rgba(255,255,255,.9)";
      c.stroke();
    });

    // crosshair on the exact spot
    c.strokeStyle = "rgba(255,255,255,.95)";
    c.lineWidth = 1;
    c.beginPath();
    c.moveTo(lx + half - 12, ly + half); c.lineTo(lx + half + 12, ly + half);
    c.moveTo(lx + half, ly + half - 12); c.lineTo(lx + half, ly + half + 12);
    c.stroke();
    c.restore();

    c.save();
    c.strokeStyle = forcedZoomKey() ? "#e8b23a" : "rgba(255,255,255,.55)";
    c.lineWidth = forcedZoomKey() ? 2 : 1;
    c.strokeRect(lx + .5, ly + .5, LOUPE_SIZE - 1, LOUPE_SIZE - 1);
    if (key) {
      c.font = "600 11px 'Segoe UI', system-ui, sans-serif";
      c.textAlign = "left";
      c.textBaseline = "top";
      c.fillStyle = "rgba(0,0,0,.72)";
      c.fillRect(lx, ly, LOUPE_SIZE, 16);
      c.fillStyle = forcedZoomKey() ? "#e8b23a" : "#fff";
      c.fillText(key + "  ×" + LOUPE_ZOOM, lx + 5, ly + 3);
    }
    c.restore();
  }

  var rafPending = false;

  function schedulePaint() {
    if (rafPending) return;
    rafPending = true;
    window.requestAnimationFrame(function () { rafPending = false; paint(); });
  }

  function paint() {
    if (!state.img || !state.cfg) return;
    bctx.clearRect(0, 0, state.dispW, state.dispH);
    bctx.drawImage(state.img, 0, 0, state.dispW, state.dispH);
    drawOverlay(bctx);
    ctx.clearRect(0, 0, state.dispW, state.dispH);
    ctx.drawImage(buffer, 0, 0, state.dispW, state.dispH);
    drawLoupe(ctx);
  }

  // -------------------------------------------------------------------
  // Pointer interaction
  // -------------------------------------------------------------------
  function canvasPos(ev) {
    var rect = el.canvas.getBoundingClientRect();
    return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
  }

  function hitTest(pos) {
    var best = null, bestD = HIT_RADIUS;
    clickOrder().forEach(function (key) {
      var pt = pointOf(key);
      if (!pt) return;
      var s = toScreen(pt);
      var d = Math.hypot(s.x - pos.x, s.y - pos.y);
      if (d <= bestD) { bestD = d; best = key; }
    });
    return best;
  }

  el.canvas.addEventListener("mousedown", function (ev) {
    if (!state.img || ev.button !== 0) return;
    var pos = canvasPos(ev);
    state.cursor = pos;
    var hit = hitTest(pos);
    if (hit) {
      state.selected = hit;
      state.dragging = hit;
      refresh();
      return;
    }
    var nk = nextKey();
    if (!nk) {
      setStatus("All five points are placed. Click a dot to adjust it, or press R to reset.", "");
      return;
    }
    if (nk === "bottom_heel" && !rigDir()) {
      setStatus("Place rig_top and rig_bottom first — bottom_heel is constructed along the rig line.", "err");
      return;
    }
    if (nk === "bottom_heel" && !state.pts.malleolus) {
      setStatus("Place the malleolus first — bottom_heel is constructed from it.", "err");
      return;
    }
    place(nk, pos.x / state.scale, pos.y / state.scale);
    state.dragging = nk;
    state.lastSave = null;
    setStatus("", "");
    refresh();
  });

  window.addEventListener("mousemove", function (ev) {
    if (!state.img) return;
    var pos = canvasPos(ev);
    var inside = pos.x >= 0 && pos.y >= 0 && pos.x <= state.dispW && pos.y <= state.dispH;
    state.cursor = (inside || state.dragging) ? pos : null;
    if (state.dragging) {
      markUserEdited(state.dragging);
      setPoint(state.dragging, pos.x / state.scale, pos.y / state.scale);
      refresh();
    } else {
      schedulePaint();
    }
  });

  window.addEventListener("mouseup", function () {
    if (state.dragging) { state.dragging = null; refresh(); }
  });

  el.canvas.addEventListener("mouseleave", function () {
    if (!state.dragging) { state.cursor = null; schedulePaint(); }
  });

  // -------------------------------------------------------------------
  // Keyboard: nudging is how the last pixel of precision gets found
  // -------------------------------------------------------------------
  function nudge(dx, dy) {
    var key = state.selected;
    if (!key || !placed(key)) return false;
    if (key === "bottom_heel") {
      // The heel only has one degree of freedom, so up/down move it ALONG
      // the construction line. Sideways would mean leaving the line.
      if (dx !== 0 && dy === 0) {
        setStatus("bottom_heel only moves along the construction line — use ↑ and ↓.", "");
        return true;
      }
      state.travel += dy;
      refresh();
      return true;
    }
    markUserEdited(key);
    var p = state.pts[key];
    setPoint(key, p.x + dx, p.y + dy);
    refresh();
    return true;
  }

  window.addEventListener("keydown", function (ev) {
    var tag = (ev.target && ev.target.tagName) || "";
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
    if (!el.warnModal.hidden) return;

    var step = ev.shiftKey ? NUDGE_LARGE : NUDGE_SMALL;
    var handled = true;
    switch (ev.key) {
      case "ArrowLeft":  handled = nudge(-step, 0); break;
      case "ArrowRight": handled = nudge(step, 0); break;
      case "ArrowUp":    handled = nudge(0, -step); break;
      case "ArrowDown":  handled = nudge(0, step); break;
      case "Enter":      if (!el.save.disabled) save(false); break;
      case "Backspace":  undoLast(); break;
      case "r": case "R": resetClicks(); break;
      case "[":          goTo(state.index - 1); break;
      case "]":          goTo(state.index + 1); break;
      case "Tab":
        cycleSelection(ev.shiftKey ? -1 : 1);
        break;
      default: handled = false;
    }
    if (handled) ev.preventDefault();
  });

  function cycleSelection(dir) {
    var order = clickOrder().filter(placed);
    if (!order.length) return;
    var i = order.indexOf(state.selected);
    state.selected = order[(i + dir + order.length) % order.length];
    refresh();
  }

  window.addEventListener("resize", function () {
    if (!state.img) return;
    fitCanvas();
    schedulePaint();
  });

  // -------------------------------------------------------------------
  // RTMW-x malleolus auto-suggest.
  //
  // Entirely optional and entirely asynchronous: the request goes out after
  // the image is already on screen, so nothing waits for it. A reply that
  // arrives late, for an image the annotator has already left, is discarded -
  // that is what suggestToken is for.
  //
  // The suggestion is only ever a starting point. It is drawn with a dashed
  // amber ring and badged "suggested - check it" until the annotator moves it,
  // so a guess can never be mistaken for a placement.
  // -------------------------------------------------------------------
  var SUGGEST_RETRY_MS = 5000;
  var SUGGEST_RETRIES = 6;        // ~30 s, enough for a cold model to warm up
  // Inference is ~0.9 s and the server runs one at a time, so flicking through
  // images would queue work nobody wants and delay the request that matters.
  // Wait for the annotator to settle, and abort anything already in flight.
  var SUGGEST_DEBOUNCE_MS = 350;

  function setSuggestState(msg, kind) {
    el.suggestState.textContent = msg || "";
    el.suggestState.className = "suggest-state" + (kind ? " " + kind : "");
  }

  function abortSuggestion() {
    state.suggestToken += 1;
    if (state.suggestAbort) {
      try { state.suggestAbort.abort(); } catch (e) { /* already settled */ }
      state.suggestAbort = null;
    }
  }

  function maybeSuggest(name) {
    abortSuggestion();
    if (!el.suggestToggle.checked) { setSuggestState("off"); return; }
    if (state.known[name]) {           // already annotated: never second-guess it
      setSuggestState("skipped - this image already has a row");
      return;
    }
    var token = state.suggestToken;
    setSuggestState("asking RTMW-x…");
    window.setTimeout(function () {
      if (token === state.suggestToken && currentName() === name) {
        requestSuggestion(name, token, 0);
      }
    }, SUGGEST_DEBOUNCE_MS);
  }

  function requestSuggestion(name, token, attempt) {
    var ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    state.suggestAbort = ctrl;
    fetch("/api/v2/suggest_malleolus?side=" + encodeURIComponent(state.side)
          + "&filename=" + encodeURIComponent(name),
          { cache: "no-store", signal: ctrl ? ctrl.signal : undefined })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (token !== state.suggestToken || currentName() !== name) return;  // stale
        if (d.status === "loading" || d.status === "cold") {
          setSuggestState("model loading…", "warn");
          if (attempt < SUGGEST_RETRIES) {
            window.setTimeout(function () {
              if (token === state.suggestToken) requestSuggestion(name, token, attempt + 1);
            }, SUGGEST_RETRY_MS);
          } else {
            setSuggestState("model still loading - carry on by hand", "warn");
          }
          return;
        }
        if (d.status !== "ok") {
          setSuggestState(suggestReason(d), "warn");
          return;
        }
        applySuggestion(name, d);
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return;      // superseded, not a failure
        setSuggestState("unavailable (" + err.message + ")", "warn");
      });
  }

  function suggestReason(d) {
    if (d.status === "unavailable") return "not available - see the server log";
    if (d.status === "no_person") return "no person found in this frame";
    if (d.status === "low_confidence") return "too uncertain to suggest";
    if (d.status === "error") return "inference failed";
    return d.status;
  }

  function applySuggestion(name, d) {
    // The suggestion is in upright full-resolution pixels. If the browser is
    // somehow measuring against a different frame, dropping it is the only
    // safe move - a rescaled guess is worse than no guess.
    var sz = d.image_size || {};
    if (sz.width !== state.fullW || sz.height !== state.fullH) {
      setSuggestState("frame mismatch - suggestion dropped", "warn");
      return;
    }
    if (state.pts.malleolus) {          // the annotator got there first
      setSuggestState("malleolus already placed by hand");
      return;
    }
    setPoint("malleolus", d.malleolus.x, d.malleolus.y);
    if (state.order.indexOf("malleolus") === -1) state.order.push("malleolus");
    state.suggested.malleolus = true;
    // Deliberately NOT selected: the annotator's next click is rig_top, and
    // stealing the selection would send their arrow keys to the wrong point.
    setSuggestState("malleolus pre-filled (" + d.ankle_used + " ankle, score "
                    + d.score.toFixed(2) + (d.cached ? ", cached" : ", "
                    + d.elapsed_ms + " ms") + ")",
                    d.confident ? "ok" : "warn");
    refresh();
  }

  // -------------------------------------------------------------------
  // Queue
  // -------------------------------------------------------------------
  el.load.addEventListener("click", loadImages);

  function loadImages() {
    var side = el.side.value;
    var mode = document.querySelector('input[name="mode"]:checked').value;
    el.load.disabled = true;
    setStatus("Loading queue…", "");
    fetch("/api/v2/images?side=" + encodeURIComponent(side) + "&mode=" + encodeURIComponent(mode))
      .then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok) throw new Error(body.detail || r.statusText);
          return body;
        });
      })
      .then(function (data) {
        state.side = data.side;
        state.queue = data.images;
        state.index = 0;
        state.total = data.total_in_folder;
        state.doneBefore = data.already_annotated;
        state.known = data.known || {};
        state.knownAtLoad = Object.assign({}, state.known);
        state.saved = {};
        fillVocabulary(data.vocabulary || {});
        el.queueInfo.textContent = data.images.length + " queued • "
          + data.already_annotated + " already done under " + data.scheme_version
          + " • " + data.total_in_folder + " in folder";
        if (!data.images.length) {
          el.empty.hidden = false;
          el.empty.textContent = "Nothing left to annotate in this folder for the 5-point scheme.";
          state.img = null;
          el.canvas.classList.remove("ready");
          refresh();
          setStatus("Queue empty.", "ok");
          return;
        }
        el.empty.hidden = true;
        return loadCurrentImage();
      })
      .catch(function (err) { setStatus("Load failed: " + err.message, "err"); })
      .finally(function () { el.load.disabled = false; });
  }

  function fillVocabulary(v) {
    function fill(node, values) {
      node.innerHTML = "";
      (values || []).forEach(function (val) {
        var o = document.createElement("option");
        o.value = val;
        node.appendChild(o);
      });
    }
    fill(el.vocSubject, v.subject_id);
    fill(el.vocPair, v.pair_id);
    fill(el.vocPhase, v.phase);
  }

  function loadCurrentImage() {
    var name = currentName();
    if (!name) return finishQueue();
    // Flicking through images leaves several fetches in flight. Without this
    // token the last one to RESOLVE wins rather than the last one requested,
    // so the canvas can end up showing a different photo than the sidebar
    // names - and landmarks would then be saved against the wrong image.
    var token = ++state.loadToken;
    clearPoints();
    setStatus((state.lastSave ? state.lastSave.msg + "  •  " : "")
              + "loading " + name + "…", state.lastSave ? state.lastSave.kind : "");
    var url = "/api/image/" + encodeURIComponent(state.side) + "/" + encodeURIComponent(name);
    var fw = 0, fh = 0;
    return fetch(url, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        fw = parseInt(r.headers.get("X-Full-Width") || "0", 10);
        fh = parseInt(r.headers.get("X-Full-Height") || "0", 10);
        return r.blob();
      })
      .then(function (blob) {
        return new Promise(function (resolve, reject) {
          var img = new Image();
          img.onload = function () { resolve(img); };
          img.onerror = function () { reject(new Error("decode failed")); };
          img.src = URL.createObjectURL(blob);
        });
      })
      .then(function (img) {
        if (token !== state.loadToken) return;      // superseded by a later load
        state.img = img;
        el.canvas.classList.add("ready");   // style.css hides #canvas without this
        // The headers are authoritative: the blob is a downscaled preview,
        // but every coordinate must be in the FULL upright frame.
        state.fullW = fw || img.naturalWidth;
        state.fullH = fh || img.naturalHeight;
        fitCanvas();
        applyMetadata(name);
        // Clear the "loading..." line either way, or it sticks around as if
        // the image never finished loading.
        setStatus(state.lastSave ? state.lastSave.msg : "",
                  state.lastSave ? state.lastSave.kind : "");
        refresh();
        maybeSuggest(name);      // async; the image is already on screen
      })
      .catch(function (err) {
        if (token !== state.loadToken) return;
        setStatus("Could not load " + name + ": " + err.message, "err");
      });
  }

  /* Prefill from an existing master row, else carry the sticky fields over. */
  function applyMetadata(name) {
    var known = state.known[name];
    if (known) {
      el.subjectId.value = known.subject_id || "";
      el.pairId.value = known.pair_id || "";
      el.phase.value = known.phase || "";
      el.screenAngle.value = known.screen_angle || "";
      el.notes.value = "";
      state.lastSave = { msg: "This image already has a row; its metadata was loaded. "
                              + "Saving will replace that row.", kind: "" };
      return;
    }
    if (!el.stickyMeta.checked) {
      el.subjectId.value = "";
      el.pairId.value = "";
      el.phase.value = "";
    }
    el.screenAngle.value = "";
    el.notes.value = "";
  }

  function finishQueue() {
    state.img = null;
    el.canvas.classList.remove("ready");
    el.empty.hidden = false;
    el.empty.textContent = "Queue complete.";
    el.queueInfo.textContent = "Queue complete.";
    refresh();
    setStatus("Queue complete.", "ok");
  }

  function advance() {
    state.index += 1;
    if (state.index >= state.queue.length) return finishQueue();
    return loadCurrentImage();
  }

  /*
   * Free movement through the queue, in either direction.
   *
   * Moving away discards unsaved work - but it says so, because silently
   * dropping five careful clicks is worse than a blunt sentence about it.
   */
  function goTo(index) {
    if (!state.queue.length) return;
    abortSuggestion();
    index = Math.round(clamp(index, 0, state.queue.length - 1));
    if (index === state.index && state.img) return;
    var abandoned = state.order.length ? currentName() : null;
    state.index = index;
    state.lastSave = abandoned
      ? { msg: "Unsaved clicks on " + abandoned + " were discarded.", kind: "warn" }
      : null;
    return loadCurrentImage();
  }

  function renderNav() {
    var n = state.queue.length;
    el.prev.disabled = !n || state.index <= 0;
    el.next.disabled = !n || state.index >= n - 1;
  }

  function resetClicks() {
    clearPoints();
    setStatus("Clicks reset.", "");
    refresh();
    // Back to how the image opened, which included the auto-suggested
    // malleolus. Served from the server's cache, so it returns immediately.
    if (currentName()) maybeSuggest(currentName());
  }

  el.prev.addEventListener("click", function () { goTo(state.index - 1); });
  el.next.addEventListener("click", function () { goTo(state.index + 1); });
  el.rayToggle.addEventListener("change", schedulePaint);
  el.suggestToggle.addEventListener("change", function () {
    if (el.suggestToggle.checked) {
      if (currentName()) maybeSuggest(currentName());
    } else {
      abortSuggestion();             // drop anything in flight
      setSuggestState("off");
    }
  });
  el.reset.addEventListener("click", resetClicks);
  el.undo.addEventListener("click", undoLast);
  el.zoomToggle.addEventListener("change", schedulePaint);

  // -------------------------------------------------------------------
  // Save
  //
  // The server is the single authority on what is valid. The client shows
  // the same numbers live so nothing is a surprise, but never decides.
  // -------------------------------------------------------------------
  el.save.addEventListener("click", function () { save(false); });

  function save(acknowledged) {
    var p = allPoints();
    if (!p) { setStatus("All five points are required.", "err"); return; }
    var name = currentName();
    el.save.disabled = true;
    setStatus("Saving…", "");

    var body = {
      filename: name,
      side: state.side,
      points: p,
      image_width: state.fullW,
      image_height: state.fullH,
      subject_id: el.subjectId.value,
      pair_id: el.pairId.value,
      phase: el.phase.value,
      screen_angle: el.screenAngle.value,
      notes: el.notes.value,
      acknowledged: !!acknowledged
    };

    fetch("/api/v2/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (r) {
        return r.json().then(function (data) { return { ok: r.ok, status: r.status, data: data }; });
      })
      .then(function (res) {
        if (!res.ok) {
          var detail = res.data && res.data.detail;
          if (detail && detail.warnings) {
            state.pendingSave = true;
            showWarnings(detail.warnings);
            return;
          }
          // A refusal used to land as one line at the bottom of a long
          // sidebar, which is easy to scroll past - and then the 422 in the
          // server log is all you have. Put it in front of the annotator.
          showRefusal(describeDetail(detail));
          return;
        }
        var d = res.data;
        state.known[name] = d.row;
        state.saved[name] = true;
        var msg = "Saved " + name + " (" + d.csv_action + ")  •  angle "
          + Number(d.row.angle_deg).toFixed(2) + "°  •  ±"
          + d.budget.total_deg.toFixed(3) + "° estimated"
          + (d.warnings.length ? "  — saved with warnings" : "");
        state.lastSave = { msg: msg, kind: d.warnings.length ? "warn" : "ok" };
        setStatus(msg, state.lastSave.kind);
        return advance();
      })
      .catch(function (err) { setStatus("Not saved: " + err.message, "err"); })
      .finally(function () { el.save.disabled = !allPoints(); });
  }

  /* FastAPI answers body-validation failures with a LIST of field errors,
     not a string, so JSON.stringify used to leak raw objects into the UI. */
  function describeDetail(detail) {
    if (!detail) return ["The server refused the save but gave no reason."];
    if (typeof detail === "string") return [detail];
    if (Array.isArray(detail)) {
      return detail.map(function (e) {
        var where = Array.isArray(e.loc) ? e.loc.join(" › ") : "";
        return (where ? where + ": " : "") + (e.msg || JSON.stringify(e));
      });
    }
    if (detail.message) return [detail.message];
    return [JSON.stringify(detail)];
  }

  function showRefusal(lines) {
    el.warnTitle.textContent = "Not saved";
    el.warnTitle.className = "modal-title refused";
    el.warnNote.textContent = "Nothing was written. Fix the frame and save again.";
    el.warnList.innerHTML = "";
    lines.forEach(function (w) {
      var li = document.createElement("li");
      li.textContent = w;
      el.warnList.appendChild(li);
    });
    state.pendingSave = null;
    el.warnConfirm.hidden = true;
    el.warnCancel.textContent = "OK";
    el.warnModal.hidden = false;
    el.warnCancel.focus();
    setStatus("Not saved - " + lines[0], "err");
  }

  function showWarnings(list) {
    el.warnTitle.textContent = "This frame has warnings";
    el.warnTitle.className = "modal-title";
    el.warnNote.textContent = "Saving records each warning in the row's notes column.";
    el.warnConfirm.hidden = false;
    el.warnCancel.textContent = "Go back and fix";
    el.warnList.innerHTML = "";
    list.forEach(function (w) {
      var li = document.createElement("li");
      li.textContent = w;
      el.warnList.appendChild(li);
    });
    el.warnModal.hidden = false;
    el.warnConfirm.focus();
    setStatus("Not saved yet — confirm the warnings.", "warn");
  }

  el.warnCancel.addEventListener("click", function () {
    el.warnModal.hidden = true;
    if (state.pendingSave !== null) setStatus("Nothing was saved.", "");
    state.pendingSave = null;
  });

  el.warnConfirm.addEventListener("click", function () {
    el.warnModal.hidden = true;
    if (state.pendingSave) { state.pendingSave = null; save(true); }
  });
})();
