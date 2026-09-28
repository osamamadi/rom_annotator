/* ROM Annotator - vanilla frontend */
(function () {
  "use strict";

  var MAX_UPSCALE = 3;          // don't blow tiny images up past this
  var LOUPE_SIZE = 150;         // px on screen
  var LOUPE_ZOOM = 4;           // magnification relative to displayed scale
  var HIT_RADIUS = 12;          // screen px within which a point can be grabbed
  var LABEL_FONT = "600 13px 'Segoe UI', system-ui, sans-serif";

  // Shin magnet: edge search along the circle, around where you click.
  var EDGE_WINDOW_SCREEN = 25;  // search half-width along the arc, in screen px
  var EDGE_BAND = 2;            // preview px either side of the circle averaged per sample
  // The leg silhouette is a SUSTAINED brightness change - skin on one side,
  // background on the other - while hair and skin texture only make local
  // flicker. So each position is scored by the difference between the mean
  // brightness of the EDGE_SIDE samples before it and after it. Checked on all
  // labelled photos: confident on 32 of 37, and on every one of those it lands
  // on the posterior edge (hairy and pale legs alike). A plain local gradient
  // managed 21 of 37 and locked onto hair.
  var EDGE_SIDE = 12;           // samples averaged on each side of a candidate step
  var EDGE_MIN_STEP = 8;        // mean luminance step (0-255) below which nothing is trusted
  var EDGE_DOMINANCE = 2.0;     // winning step must be this many times the runner-up
  var EDGE_PEAK_SEPARATION = 24; // samples that make another step a different edge
  var STORE_FACTOR = "rom.magnetFactor";
  var STORE_EDGE = "rom.magnetEdgeSnap";

  var el = {
    side: document.getElementById("sideSelect"),
    load: document.getElementById("loadBtn"),
    save: document.getElementById("saveBtn"),
    reset: document.getElementById("resetBtn"),
    skip: document.getElementById("skipBtn"),
    undo: document.getElementById("undoBtn"),
    loupe: document.getElementById("loupeToggle"),
    canvas: document.getElementById("canvas"),
    stage: document.getElementById("stage"),
    empty: document.getElementById("emptyState"),
    progressText: document.getElementById("progressText"),
    progressFill: document.getElementById("progressFill"),
    filename: document.getElementById("filenameText"),
    dims: document.getElementById("dimsText"),
    list: document.getElementById("landmarkList"),
    angleReadout: document.getElementById("angleReadout"),
    magnetFactor: document.getElementById("magnetFactor"),
    magnetFactorValue: document.getElementById("magnetFactorValue"),
    magnetPx: document.getElementById("magnetPx"),
    edgeSnap: document.getElementById("edgeSnap"),
    gtNote: document.getElementById("gtNote"),
    sequenceTitle: document.getElementById("sequenceTitle"),
    nextHint: document.getElementById("nextHint"),
    queueInfo: document.getElementById("queueInfo"),
    pathInfo: document.getElementById("pathInfo"),
    status: document.getElementById("status")
  };

  var ctx = el.canvas.getContext("2d");
  var buffer = document.createElement("canvas");   // base image + committed points
  var bctx = buffer.getContext("2d");

  var state = {
    landmarks: [],
    guides: [],
    angle: null,
    magnet: null,        // indices + limits from /api/config
    magnetFactor: 1,     // radius in foot lengths
    shinTheta: null,     // direction of the shin from bottom_heel, full-image radians
    ghost: null,         // where a click would land right now: {point, edge}
    pixels: null,        // preview RGBA, for the edge search
    shinOffImage: false, // shin's circle position fell outside the image
    gtDefault: "manual", // gt_source for images with nothing on record
    gtKnown: {},         // gt_source already recorded, per queued image
    side: "left",
    mode: "resume",
    queue: [],
    index: 0,
    points: [],          // full-resolution upright image coordinates
    img: null,           // downscaled preview, already rotated upright
    fullW: 0,            // full upright size the coordinates refer to
    fullH: 0,
    objectUrl: null,
    scale: 1,            // displayed px per FULL image px
    dispW: 0,
    dispH: 0,
    dpr: window.devicePixelRatio || 1,
    saving: false,
    cursor: null
  };

  var drag = { active: false, index: -1, moved: false };
  var suppressClick = false;

  // ---------------------------------------------------------------- utils

  function setStatus(msg, kind) {
    el.status.textContent = msg || "";
    el.status.className = "status" + (kind ? " " + kind : "");
  }

  function selectedMode() {
    var checked = document.querySelector('input[name="mode"]:checked');
    return checked ? checked.value : "resume";
  }

  function currentName() {
    return state.queue[state.index];
  }

  /* Ground-truth source for the current image: what is on record, else the default. */
  function applyGtSource(name) {
    var known = state.gtKnown[name];
    var value = known || state.gtDefault;
    var radio = document.querySelector('input[name="gtSource"][value="' + value + '"]');
    if (radio) radio.checked = true;
    if (el.gtNote) {
      el.gtNote.textContent = known
        ? "Loaded from this image's existing label: " + known + "."
        : "New image - defaults to " + state.gtDefault +
          ". Screen = angle photographed on the device display; manual = recorded by hand.";
    }
  }

  function selectedGtSource() {
    var checked = document.querySelector('input[name="gtSource"]:checked');
    return checked ? checked.value : null;
  }

  function clamp(v, lo, hi) {
    return v < lo ? lo : (v > hi ? hi : v);
  }

  // ---------------------------------------------------------------- config

  var config = null;

  function renderPathInfo() {
    if (!config) return;
    el.pathInfo.textContent =
      "in: " + config.input_dirs[el.side.value] +
      "   |   out: " + config.output_dir + "\\" + el.side.value + "\\" + config.today;
  }

  fetch("/api/config")
    .then(function (r) { return r.json(); })
    .then(function (cfg) {
      config = cfg;
      state.landmarks = cfg.landmarks;
      state.guides = cfg.guides || [];
      state.angle = cfg.angle || null;
      initMagnet(cfg.magnet);
      if (cfg.gt_source && cfg.gt_source.default) state.gtDefault = cfg.gt_source.default;
      renderPathInfo();
      renderLandmarkList();
    })
    .catch(function (err) { setStatus("Could not load config: " + err, "err"); });

  // ---------------------------------------------------------------- sidebar

  function renderLandmarkList() {
    if (el.sequenceTitle && state.landmarks.length) {
      el.sequenceTitle.textContent =
        "Click sequence - " + state.landmarks.length + " points, in order";
    }
    el.list.innerHTML = "";
    state.landmarks.forEach(function (lm, i) {
      var li = document.createElement("li");

      var step = document.createElement("span");
      step.className = "step";
      step.textContent = (i + 1);
      li.appendChild(step);

      var dot = document.createElement("span");
      dot.className = "dot";
      dot.style.background = lm.color;
      li.appendChild(dot);

      var name = document.createElement("span");
      name.textContent = lm.label;
      li.appendChild(name);

      var coords = document.createElement("span");
      coords.className = "coords";
      if (state.points[i]) {
        coords.textContent = state.points[i].x + ", " + state.points[i].y;
        li.classList.add("done");
        if (drag.active && drag.index === i) li.classList.add("dragging");
      } else if (i === state.points.length && state.img) {
        li.classList.add("active");
        coords.textContent = "click now";
      }
      li.appendChild(coords);

      el.list.appendChild(li);
    });

    if (!state.img) {
      el.nextHint.textContent = "";
    } else if (state.points.length < state.landmarks.length) {
      el.nextHint.textContent =
        "Next click (" + (state.points.length + 1) + "/" + state.landmarks.length + "): " +
        state.landmarks[state.points.length].label +
        (isTarget(state.points.length)
          ? " — click near the back edge of the leg; it snaps onto the circle."
          : "");
    } else if (state.magnet && !shinOnCircle()) {
      el.nextHint.textContent = "The shin is off the image at this radius. " +
        "Lower the magnet factor or drag the shin.";
    } else {
      el.nextHint.textContent = "All " + state.landmarks.length +
        " placed. Drag any point to adjust, then Save & Next.";
    }

    var complete = state.img && state.points.length === state.landmarks.length;
    el.save.disabled = !complete || (state.magnet && !shinOnCircle()) || state.saving;
    el.undo.disabled = state.points.length === 0;

    renderAngleReadout();
    renderMagnet();
  }

  function renderAngleReadout() {
    if (!el.angleReadout) return;
    var ang = ankleAngle();
    var ready = state.img && state.points.length === state.landmarks.length;
    if (!ang || !ready) {
      el.angleReadout.textContent = "—";
      el.angleReadout.classList.remove("live");
      return;
    }
    el.angleReadout.textContent =
      "Ankle Angle: " + ang.theta.toFixed(1) + "°  |  DF: " +
      (ang.df >= 0 ? "+" : "") + ang.df.toFixed(1) + "°";
    el.angleReadout.classList.add("live");
  }

  function renderProgress() {
    if (!state.queue.length) {
      el.progressText.textContent = "—";
      el.progressFill.style.width = "0";
      el.filename.textContent = "";
      el.dims.textContent = "";
      return;
    }
    el.progressText.textContent = "Image " + (state.index + 1) + " of " + state.queue.length;
    el.progressFill.style.width = ((state.index) / state.queue.length * 100) + "%";
    el.filename.textContent = currentName() || "";
    el.dims.textContent = state.img
      ? state.fullW + " x " + state.fullH + " px  (view " +
        Math.round(state.scale * 100) + "%)"
      : "";
  }

  // ---------------------------------------------------------------- canvas

  function fitCanvas() {
    if (!state.img) return;

    var cs = window.getComputedStyle(el.stage);
    var padX = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
    var padY = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    var availW = Math.max(50, el.stage.clientWidth - padX);
    var availH = Math.max(50, el.stage.clientHeight - padY);

    // Fit against the FULL upright size, so scale maps screen px -> full px.
    var iw = state.fullW;
    var ih = state.fullH;
    var scale = Math.min(availW / iw, availH / ih);
    if (scale > MAX_UPSCALE) scale = MAX_UPSCALE;

    state.dispW = Math.max(1, Math.round(iw * scale));
    state.dispH = Math.max(1, Math.round(ih * scale));
    // Use the post-rounding ratio so screen px -> full px is exact.
    state.scale = state.dispW / iw;
    state.dpr = window.devicePixelRatio || 1;

    [el.canvas, buffer].forEach(function (c) {
      c.width = Math.round(state.dispW * state.dpr);
      c.height = Math.round(state.dispH * state.dpr);
      c.style.width = state.dispW + "px";
      c.style.height = state.dispH + "px";
    });

    bctx.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
    ctx.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);

    drawBuffer();
    paint();
    renderProgress();   // the zoom % in the sidebar tracks the fit
  }

  // ---------------------------------------------------- label placement

  function overlapArea(rect, obstacles) {
    var total = 0;
    for (var i = 0; i < obstacles.length; i++) {
      var b = obstacles[i];
      var w = Math.min(rect[2], b[2]) - Math.max(rect[0], b[0]);
      var h = Math.min(rect[3], b[3]) - Math.max(rect[1], b[1]);
      if (w > 0 && h > 0) total += w * h;
    }
    return total;
  }

  /*
   * Pick a label box near (x, y) that overlaps nothing else. Tries eight
   * positions around the point, then nudges vertically. Falls back to the
   * least-overlapping box when the area is too crowded.
   */
  function placeLabel(x, y, w, h, off, boundW, boundH, obstacles) {
    var candidates = [
      [x + off, y - h / 2],
      [x - off - w, y - h / 2],
      [x - w / 2, y - off - h],
      [x - w / 2, y + off],
      [x + off, y - off - h],
      [x - off - w, y - off - h],
      [x + off, y + off],
      [x - off - w, y + off]
    ];

    var best = null;
    var bestScore = Infinity;

    for (var i = 0; i < candidates.length; i++) {
      var cx = clamp(candidates[i][0], 0, Math.max(0, boundW - w));
      var cy = clamp(candidates[i][1], 0, Math.max(0, boundH - h));
      var rect = [cx, cy, cx + w, cy + h];
      var score = overlapArea(rect, obstacles);
      if (score === 0) return rect;
      if (score < bestScore) { best = rect; bestScore = score; }
    }

    var step = h + off / 2;
    for (var k = 1; k < 40; k++) {
      for (var s = 0; s < 2; s++) {
        var ny = best[1] + (s === 0 ? 1 : -1) * k * step;
        if (ny < 0 || ny + h > boundH) continue;
        var r2 = [best[0], ny, best[0] + w, ny + h];
        if (overlapArea(r2, obstacles) === 0) return r2;
      }
    }
    return best;
  }

  // ---------------------------------------------------- ankle angle

  /*
   * Angle at the ankle between the leg vector and the foot vector.
   *
   * Image y grows downward; the clinical convention is y up, so every y is
   * flipped to y_math = fullH - y before the vectors are formed. app.py runs
   * this identical arithmetic, so the live readout and the burned-in number
   * cannot disagree.
   */
  function ankleAngle() {
    var A = state.angle;
    if (!A) return null;
    var need = [A.vertex, A.arm_leg, A.arm_foot];
    for (var i = 0; i < need.length; i++) {
      if (!state.points[need[i]]) return null;
    }

    var px = function (i) { return state.points[i].x; };
    var py = function (i) { return state.fullH - state.points[i].y; };

    var ox = px(A.vertex), oy = py(A.vertex);          // vertex is the ankle
    var ux = px(A.arm_leg) - ox, uy = py(A.arm_leg) - oy;    // leg vector
    var vx = px(A.arm_foot) - ox, vy = py(A.arm_foot) - oy;  // foot vector

    var dot = (ux * vx) + (uy * vy);
    var cross = (ux * vy) - (uy * vx);
    var theta = Math.abs(Math.atan2(Math.abs(cross), dot) * (180 / Math.PI));

    return { theta: theta, df: 90 - theta };
  }

  function rectsOverlap(a, b) {
    return !(a[2] <= b[0] || b[2] <= a[0] || a[3] <= b[1] || b[3] <= a[1]);
  }

  /* Arc at the ankle plus the angle text, drawn clear of the dots. */
  function drawAngleOverlay(screenPts) {
    var A = state.angle;
    var ang = ankleAngle();
    if (!A || !ang || state.points.length < state.landmarks.length) return;

    var c = screenPts[A.vertex];
    var leg = screenPts[A.arm_leg];
    var foot = screenPts[A.arm_foot];

    var aLeg = Math.atan2(leg.y - c.y, leg.x - c.x);
    var aFoot = Math.atan2(foot.y - c.y, foot.x - c.x);
    // Sweep the SHORT way round, which is the angle actually being measured.
    var delta = aFoot - aLeg;
    delta = (((delta + Math.PI) % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI) - Math.PI;

    var r = A.arc_radius;
    bctx.save();
    bctx.lineCap = "butt";
    bctx.beginPath();
    bctx.arc(c.x, c.y, r, aLeg, aFoot, delta < 0);
    bctx.strokeStyle = "rgba(0,0,0,.8)";
    bctx.lineWidth = 6;
    bctx.stroke();
    bctx.strokeStyle = A.arc_color;
    bctx.lineWidth = 2.5;
    bctx.stroke();

    // Text along the bisector, pushed out until it clears every marker.
    var unit = function (p) {
      var dx = p.x - c.x, dy = p.y - c.y;
      var n = Math.hypot(dx, dy) || 1;
      return { x: dx / n, y: dy / n };
    };
    var ul = unit(leg), uf = unit(foot);
    var bx = ul.x + uf.x, by = ul.y + uf.y;
    var n = Math.hypot(bx, by);
    if (n < 1e-6) { bx = -ul.y; by = ul.x; n = 1; }   // arms opposite
    bx /= n; by /= n;

    var text = ang.theta.toFixed(1) + "°";
    bctx.font = "700 15px 'Segoe UI', system-ui, sans-serif";
    bctx.textAlign = "center";
    bctx.textBaseline = "middle";
    var tw = bctx.measureText(text).width;
    var th = 17;

    var arm = 14;
    var obstacles = screenPts.map(function (p) {
      return [p.x - arm, p.y - arm, p.x + arm, p.y + arm];
    });

    var tx = c.x, ty = c.y;
    for (var i = 0; i < 40; i++) {
      var d = r * 1.45 + i * 8;
      tx = c.x + bx * d;
      ty = c.y + by * d;
      var box = [tx - tw / 2 - 3, ty - th / 2 - 3, tx + tw / 2 + 3, ty + th / 2 + 3];
      var inside = box[0] >= 0 && box[1] >= 0 &&
                   box[2] <= state.dispW && box[3] <= state.dispH;
      var clash = obstacles.some(function (o) { return rectsOverlap(box, o); });
      if (inside && !clash) break;
    }
    tx = clamp(tx, tw / 2 + 4, state.dispW - tw / 2 - 4);
    ty = clamp(ty, th / 2 + 4, state.dispH - th / 2 - 4);

    bctx.lineJoin = "round";
    bctx.strokeStyle = "#000";
    bctx.lineWidth = 4;
    bctx.strokeText(text, tx, ty);
    bctx.fillStyle = A.text_color;
    bctx.fillText(text, tx, ty);
    bctx.restore();
  }

  // ---------------------------------------------------- shin magnet
  //
  // The shin is constrained to a circle centred on bottom_heel, radius =
  // factor x foot length (bottom_heel -> 5th_metatarsal). A click projects
  // onto the circle along the ray from the heel, then (optionally) slides
  // along the circle to the strongest nearby edge in the image. The circle is
  // drawn on the canvas only; the server never draws it on the export.

  function readStore(key, fallback) {
    try {
      var v = window.localStorage.getItem(key);
      return v === null ? fallback : v;
    } catch (e) { return fallback; }
  }

  function writeStore(key, value) {
    try { window.localStorage.setItem(key, String(value)); } catch (e) { /* ignore */ }
  }

  function initMagnet(cfg) {
    state.magnet = cfg || null;
    if (!state.magnet) return;
    var M = state.magnet;
    var saved = parseFloat(readStore(STORE_FACTOR, ""));
    state.magnetFactor = isFinite(saved)
      ? clamp(saved, M.factor_min, M.factor_max)
      : M.factor_default;
    if (el.magnetFactor) {
      el.magnetFactor.min = M.factor_min;
      el.magnetFactor.max = M.factor_max;
      el.magnetFactor.step = M.factor_step;
      el.magnetFactor.value = state.magnetFactor;
    }
    if (el.edgeSnap) el.edgeSnap.checked = readStore(STORE_EDGE, "1") === "1";
    renderMagnet();
  }

  function isTarget(i) { return !!state.magnet && i === state.magnet.target; }

  /* Heel and 5th metatarsal both placed, so the circle exists. */
  function magnetReady() {
    var M = state.magnet;
    return !!(M && state.img && state.points[M.center] && state.points[M.reference]);
  }

  function footLength() {
    var M = state.magnet;
    var c = state.points[M.center], r = state.points[M.reference];
    return Math.hypot(r.x - c.x, r.y - c.y);
  }

  /* Radius in full-image px, or null when the circle is not defined yet. */
  function magnetRadius() {
    if (!magnetReady()) return null;
    var foot = footLength();
    return foot >= 1 ? state.magnetFactor * foot : null;
  }

  /*
   * The whole pixel closest to the exact circle point. Coordinates are
   * integers, so this keeps the stored shin within ~0.7 px of the radius.
   * Null if the circle leaves the image in that direction.
   */
  function pixelOnCircle(theta) {
    var c = state.points[state.magnet.center];
    var R = magnetRadius();
    var fx = c.x + R * Math.cos(theta);
    var fy = c.y + R * Math.sin(theta);
    var best = null, bestErr = Infinity;
    [Math.floor(fx), Math.ceil(fx)].forEach(function (x) {
      [Math.floor(fy), Math.ceil(fy)].forEach(function (y) {
        if (x < 0 || y < 0 || x > state.fullW - 1 || y > state.fullH - 1) return;
        var err = Math.abs(Math.hypot(x - c.x, y - c.y) - R);
        if (err < bestErr) { bestErr = err; best = { x: x, y: y }; }
      });
    });
    return best;
  }

  /* Direction from the heel to a screen position, in full-image radians. */
  function thetaFromScreen(pos) {
    var c = state.points[state.magnet.center];
    return Math.atan2(pos.y / state.scale - c.y, pos.x / state.scale - c.x);
  }

  function readPixels(img) {
    try {
      var cv = document.createElement("canvas");
      cv.width = img.naturalWidth;
      cv.height = img.naturalHeight;
      var g = cv.getContext("2d", { willReadFrequently: true });
      g.drawImage(img, 0, 0);
      return { data: g.getImageData(0, 0, cv.width, cv.height).data,
               w: cv.width, h: cv.height };
    } catch (e) {
      return null;       // edge snap unavailable; the plain circle still works
    }
  }

  /* Luminance of the preview at a full-image position, or null off-image. */
  function lumAt(fx, fy) {
    var P = state.pixels;
    var x = Math.round(fx * P.w / state.fullW);
    var y = Math.round(fy * P.h / state.fullH);
    if (x < 0 || y < 0 || x >= P.w || y >= P.h) return null;
    var i = (y * P.w + x) * 4;
    return 0.299 * P.data[i] + 0.587 * P.data[i + 1] + 0.114 * P.data[i + 2];
  }

  /*
   * Slide along the circle to the strongest luminance step near theta.
   *
   * Samples run ALONG the arc, so they respond to edges that cross it - the
   * leg's silhouette - and are largely blind to edges running parallel to it,
   * like the top of an ankle strap. Each sample averages a thin radial band to
   * damp hair and skin texture; nearer edges are preferred over farther ones.
   * Returns {theta, snapped, contrast}.
   */
  function edgeTheta(theta) {
    if (!state.pixels) return { theta: theta, snapped: false, contrast: 0 };
    var c = state.points[state.magnet.center];
    var R = magnetRadius();
    var step = Math.max(1, state.fullW / state.pixels.w);    // ~1 preview px
    var half = Math.max(3, Math.round((EDGE_WINDOW_SCREEN / state.scale) / step));
    // Sample EDGE_SIDE (+ smoothing) beyond the window so a step right at its
    // boundary still has a full mean on both sides.
    var pad = EDGE_SIDE + 2;

    var prof = [];
    for (var i = -half - pad; i <= half + pad; i++) {
      var a = theta + (i * step) / R;
      var sum = 0, n = 0;
      for (var b = -EDGE_BAND; b <= EDGE_BAND; b++) {
        var rr = R + b * step;
        var v = lumAt(c.x + rr * Math.cos(a), c.y + rr * Math.sin(a));
        if (v !== null) { sum += v; n++; }
      }
      prof.push(n ? sum / n : null);
    }

    var sm = prof.map(function (_v, k) {           // 5-sample box smooth
      var s = 0, n = 0;
      for (var j = k - 2; j <= k + 2; j++) {
        if (j >= 0 && j < prof.length && prof[j] !== null) { s += prof[j]; n++; }
      }
      return n ? s / n : null;
    });

    function meanOf(from, to) {                     // [from, to)
      var s = 0, n = 0;
      for (var j = from; j < to; j++) {
        if (j >= 0 && j < sm.length && sm[j] !== null) { s += sm[j]; n++; }
      }
      return n >= EDGE_SIDE / 2 ? s / n : null;
    }

    // Score = difference in mean brightness either side of each position.
    var centre = pad + half;
    var stepAt = {};
    var best = null;
    for (var k = pad; k <= pad + 2 * half; k++) {
      var before = meanOf(k - EDGE_SIDE, k);
      var after = meanOf(k + 1, k + 1 + EDGE_SIDE);
      if (before === null || after === null) continue;
      var g = Math.abs(after - before);
      stepAt[k] = g;
      var score = g * (1 - 0.5 * Math.abs(k - centre) / half);   // prefer near the click
      if (!best || score > best.score) best = { k: k, g: g, score: score };
    }
    if (!best) return { theta: theta, snapped: false, contrast: 0, rival: 0 };

    // Strongest step that is a genuinely different feature.
    var rival = 0;
    Object.keys(stepAt).forEach(function (key) {
      var q = +key;
      if (Math.abs(q - best.k) >= EDGE_PEAK_SEPARATION && stepAt[q] > rival) rival = stepAt[q];
    });
    var confident = best.g >= EDGE_MIN_STEP && best.g >= EDGE_DOMINANCE * rival;
    if (!confident) {
      return { theta: theta, snapped: false, contrast: best.g, rival: rival };
    }
    return { theta: theta + ((best.k - centre) * step) / R,
             snapped: true, contrast: best.g, rival: rival };
  }

  /* Where a click at a screen position would put the shin. */
  function snapShin(pos) {
    var theta = thetaFromScreen(pos);
    var edge = null;
    if (el.edgeSnap && el.edgeSnap.checked) {
      edge = edgeTheta(theta);
      theta = edge.theta;
    }
    return { theta: theta, point: pixelOnCircle(theta), edge: edge };
  }

  /*
   * Put an already-placed shin back on the circle after the heel, the 5th
   * metatarsal or the factor changed. Keeps its direction from the heel, so
   * nothing jumps sideways; flags it if that spot is now outside the image.
   */
  function reprojectShin() {
    var M = state.magnet;
    if (!M || !state.points[M.target] || state.shinTheta === null) return;
    if (!magnetReady() || magnetRadius() === null) {
      state.shinOffImage = true;
      return;
    }
    var p = pixelOnCircle(state.shinTheta);
    state.shinOffImage = !p;
    if (p) state.points[M.target] = p;
  }

  function shinOnCircle() {
    var M = state.magnet;
    if (!M || !state.points[M.target]) return false;
    var R = magnetRadius();
    if (R === null || state.shinOffImage) return false;
    var c = state.points[M.center], t = state.points[M.target];
    return Math.abs(Math.hypot(t.x - c.x, t.y - c.y) - R) <= M.tolerance_px;
  }

  function setFactor(value, announce) {
    var M = state.magnet;
    if (!M) return;
    var f = clamp(Math.round(value / M.factor_step) * M.factor_step,
                  M.factor_min, M.factor_max);
    f = Math.round(f * 100) / 100;
    if (f === state.magnetFactor && !announce) return;
    state.magnetFactor = f;
    if (el.magnetFactor) el.magnetFactor.value = f;
    writeStore(STORE_FACTOR, f);
    reprojectShin();
    state.ghost = null;
    drawBuffer();
    paint();
    renderLandmarkList();
  }

  function renderMagnet() {
    if (!el.magnetFactorValue) return;
    el.magnetFactorValue.textContent = state.magnetFactor.toFixed(2) + "×";
    var R = magnetRadius();
    if (R === null) {
      el.magnetPx.textContent = "Place bottom_heel and 5th_metatarsal to show the circle.";
      el.magnetPx.className = "magnet-px";
    } else if (state.shinOffImage) {
      el.magnetPx.textContent = "R = " + R.toFixed(1) +
        " px — shin is off the image at this radius; lower it.";
      el.magnetPx.className = "magnet-px bad";
    } else {
      el.magnetPx.textContent = "R = " + R.toFixed(1) + " px  (foot " +
        footLength().toFixed(1) + " px)";
      el.magnetPx.className = "magnet-px live";
    }
  }

  /* Draw image + placed points into the offscreen buffer. */
  function drawBuffer() {
    bctx.clearRect(0, 0, state.dispW, state.dispH);
    if (!state.img) return;
    bctx.drawImage(state.img, 0, 0, state.dispW, state.dispH);

    var arm = 14;
    var screenPts = state.points.map(function (p) {
      return { x: p.x * state.scale, y: p.y * state.scale };
    });

    // Magnet circle: canvas only, never part of the exported image.
    var R = magnetRadius();
    if (R !== null) {
      var mc = screenPts[state.magnet.center];
      bctx.save();
      bctx.beginPath();
      bctx.arc(mc.x, mc.y, R * state.scale, 0, Math.PI * 2);
      bctx.setLineDash([]);
      bctx.strokeStyle = "rgba(0,0,0,.7)";
      bctx.lineWidth = 3.5;
      bctx.stroke();
      bctx.setLineDash([7, 5]);
      bctx.strokeStyle = state.shinOffImage ? "#ff5c5c" : "rgba(255,255,255,.95)";
      bctx.lineWidth = 1.5;
      bctx.stroke();
      bctx.restore();
    }

    // Alignment guides UNDER the markers, so a dot is never hidden by a line.
    // A segment appears the moment both of its endpoints exist.
    var live = state.guides.filter(function (g) {
      return screenPts[g.from] && screenPts[g.to];
    });
    if (live.length) {
      bctx.save();
      bctx.lineCap = "round";
      bctx.lineJoin = "round";

      var pathFor = function (subset) {
        bctx.beginPath();
        subset.forEach(function (g) {
          bctx.moveTo(screenPts[g.from].x, screenPts[g.from].y);
          bctx.lineTo(screenPts[g.to].x, screenPts[g.to].y);
        });
        bctx.stroke();
      };

      // Every casing first, so a casing never cuts across the bright core of
      // the segment beside it; then one pass per colour.
      bctx.strokeStyle = "rgba(0,0,0,.8)";
      bctx.lineWidth = 5;
      pathFor(live);

      bctx.lineWidth = 2;
      var byColor = {};
      live.forEach(function (g) {
        (byColor[g.color] = byColor[g.color] || []).push(g);
      });
      Object.keys(byColor).forEach(function (colour) {
        bctx.strokeStyle = colour;
        pathFor(byColor[colour]);
      });
      bctx.restore();
    }

    drawAngleOverlay(screenPts);

    // Markers next, so labels can be routed around all of them.
    screenPts.forEach(function (p, i) {
      var lm = state.landmarks[i];
      var active = drag.active && drag.index === i;

      bctx.save();
      bctx.strokeStyle = lm.color;
      bctx.lineWidth = 1.5;
      bctx.beginPath();
      bctx.moveTo(p.x - arm, p.y); bctx.lineTo(p.x - 4, p.y);
      bctx.moveTo(p.x + 4, p.y);   bctx.lineTo(p.x + arm, p.y);
      bctx.moveTo(p.x, p.y - arm); bctx.lineTo(p.x, p.y - 4);
      bctx.moveTo(p.x, p.y + 4);   bctx.lineTo(p.x, p.y + arm);
      bctx.stroke();

      if (active) {   // halo while dragging
        bctx.beginPath();
        bctx.arc(p.x, p.y, 10, 0, Math.PI * 2);
        bctx.strokeStyle = "rgba(255,255,255,.9)";
        bctx.lineWidth = 1;
        bctx.stroke();
      }

      bctx.beginPath();
      bctx.arc(p.x, p.y, active ? 5.5 : 4.5, 0, Math.PI * 2);
      bctx.fillStyle = lm.color;
      bctx.fill();
      bctx.strokeStyle = "#fff";
      bctx.lineWidth = 1.5;
      bctx.stroke();
      bctx.restore();
    });

    // Then labels, each avoiding the markers and the labels already placed.
    bctx.font = LABEL_FONT;
    bctx.textBaseline = "top";

    var pad = 4;
    var obstacles = screenPts.map(function (p) {
      return [p.x - arm, p.y - arm, p.x + arm, p.y + arm];
    });

    screenPts.forEach(function (p, i) {
      var lm = state.landmarks[i];
      var w = bctx.measureText(lm.label).width + pad * 2;
      var h = 19;
      var box = placeLabel(p.x, p.y, w, h, arm + pad, state.dispW, state.dispH, obstacles);
      obstacles.push(box);

      bctx.save();
      bctx.fillStyle = "rgba(0,0,0,.75)";
      bctx.fillRect(box[0], box[1], w, h);
      bctx.fillStyle = lm.color;
      bctx.fillText(lm.label, box[0] + pad, box[1] + pad - 1);
      bctx.restore();
    });
  }

  /* Blit the buffer to the visible canvas, plus the live magnifier. */
  function paint() {
    ctx.clearRect(0, 0, state.dispW, state.dispH);
    if (!state.img) return;
    ctx.drawImage(buffer, 0, 0, state.dispW, state.dispH);

    // Ghost: exactly where a click would put the shin right now.
    var ghost = (placingShin() && !drag.active && state.cursor && state.ghost &&
                 state.ghost.point) ? state.ghost : null;
    if (ghost) {
      drawGhost(ctx, ghost.point.x * state.scale, ghost.point.y * state.scale,
                ghost.edge, 1);
    }

    var wantLoupe = el.loupe.checked && state.cursor &&
      (drag.active || state.points.length < state.landmarks.length);
    if (!wantLoupe) return;

    // The loupe parks beside the cursor but magnifies the spot the shin will
    // actually occupy - the snapped point, not the raw mouse position.
    var focus = state.cursor;
    if (drag.active && isTarget(drag.index) && state.points[drag.index]) {
      focus = { x: state.points[drag.index].x * state.scale,
                y: state.points[drag.index].y * state.scale };
    } else if (ghost) {
      focus = { x: ghost.point.x * state.scale, y: ghost.point.y * state.scale };
    }

    var cx = state.cursor.x;
    var cy = state.cursor.y;
    var half = LOUPE_SIZE / 2;
    var srcHalf = half / LOUPE_ZOOM;

    // Park the loupe away from the cursor, flipping near the edges.
    var lx = cx + 24;
    var ly = cy + 24;
    if (lx + LOUPE_SIZE > state.dispW) lx = cx - 24 - LOUPE_SIZE;
    if (ly + LOUPE_SIZE > state.dispH) ly = cy - 24 - LOUPE_SIZE;
    lx = clamp(lx, 0, Math.max(0, state.dispW - LOUPE_SIZE));
    ly = clamp(ly, 0, Math.max(0, state.dispH - LOUPE_SIZE));

    // Sample the source preview, not the shrunken canvas, so magnifying
    // actually reveals detail instead of enlarging display pixels.
    var pv = state.img.naturalWidth / state.dispW;   // preview px per screen px
    var originX = focus.x - srcHalf;
    var originY = focus.y - srcHalf;

    ctx.save();
    ctx.beginPath();
    ctx.rect(lx, ly, LOUPE_SIZE, LOUPE_SIZE);
    ctx.clip();
    ctx.fillStyle = "#000";
    ctx.fillRect(lx, ly, LOUPE_SIZE, LOUPE_SIZE);
    ctx.drawImage(
      state.img,
      originX * pv, originY * pv, srcHalf * 2 * pv, srcHalf * 2 * pv,
      lx, ly, LOUPE_SIZE, LOUPE_SIZE
    );

    // Show any already-placed points that fall inside the magnified window.
    state.points.forEach(function (p, i) {
      var px = p.x * state.scale;
      var py = p.y * state.scale;
      if (px < originX || px > originX + srcHalf * 2) return;
      if (py < originY || py > originY + srcHalf * 2) return;
      ctx.beginPath();
      ctx.arc(lx + (px - originX) * LOUPE_ZOOM, ly + (py - originY) * LOUPE_ZOOM,
              4, 0, Math.PI * 2);
      ctx.fillStyle = state.landmarks[i].color;
      ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 1;
      ctx.stroke();
    });

    // The magnet circle and the ghost, magnified, so the snap can be checked
    // against the actual skin edge before clicking.
    var R = magnetRadius();
    if (R !== null) {
      var mc = state.points[state.magnet.center];
      ctx.beginPath();
      ctx.arc(lx + (mc.x * state.scale - originX) * LOUPE_ZOOM,
              ly + (mc.y * state.scale - originY) * LOUPE_ZOOM,
              R * state.scale * LOUPE_ZOOM, 0, Math.PI * 2);
      ctx.setLineDash([6, 4]);
      ctx.strokeStyle = "rgba(255,255,255,.9)";
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (ghost) {
      drawGhost(ctx, lx + (ghost.point.x * state.scale - originX) * LOUPE_ZOOM,
                ly + (ghost.point.y * state.scale - originY) * LOUPE_ZOOM,
                ghost.edge, 1.4);
    }
    ctx.restore();

    ctx.save();
    ctx.strokeStyle = "rgba(255,255,255,.85)";
    ctx.lineWidth = 1;
    ctx.strokeRect(lx + .5, ly + .5, LOUPE_SIZE - 1, LOUPE_SIZE - 1);
    var focus = drag.active ? state.landmarks[drag.index] : state.landmarks[state.points.length];
    ctx.strokeStyle = focus ? focus.color : "#fff";
    ctx.beginPath();
    ctx.moveTo(lx + half - 10, ly + half); ctx.lineTo(lx + half + 10, ly + half);
    ctx.moveTo(lx + half, ly + half - 10); ctx.lineTo(lx + half, ly + half + 10);
    ctx.stroke();
    ctx.restore();
  }

  /* The next click places the shin (heel and 5th metatarsal already down). */
  function placingShin() {
    return magnetReady() && state.points.length === state.magnet.target;
  }

  /*
   * Hollow marker in the shin colour. A solid ring means it locked onto an
   * image edge; a dashed ring means no clear edge was found nearby, so it sits
   * where the click's direction meets the circle.
   */
  function drawGhost(g, x, y, edge, s) {
    var colour = state.landmarks[state.magnet.target].color;
    g.save();
    g.beginPath();
    g.arc(x, y, 7 * s, 0, Math.PI * 2);
    g.strokeStyle = "rgba(0,0,0,.8)";
    g.lineWidth = 4 * s;
    g.stroke();
    g.setLineDash(edge && edge.snapped ? [] : [3 * s, 3 * s]);
    g.strokeStyle = colour;
    g.lineWidth = 2 * s;
    g.stroke();
    g.setLineDash([]);
    g.beginPath();
    g.arc(x, y, 1.6 * s, 0, Math.PI * 2);
    g.fillStyle = colour;
    g.fill();
    g.restore();
  }

  var rafPending = false;
  function schedulePaint() {
    if (rafPending) return;
    rafPending = true;
    window.requestAnimationFrame(function () { rafPending = false; paint(); });
  }

  // ---------------------------------------------------------------- queue

  function loadImages() {
    var side = el.side.value;
    var mode = selectedMode();
    el.load.disabled = true;
    setStatus("Loading queue...");

    fetch("/api/images?side=" + encodeURIComponent(side) + "&mode=" + encodeURIComponent(mode))
      .then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok) throw new Error(body.detail || r.statusText);
          return body;
        });
      })
      .then(function (data) {
        state.side = data.side;
        state.mode = data.mode;
        state.queue = data.images;
        state.gtKnown = data.gt_source || {};
        state.index = 0;
        state.points = [];
        state.img = null;

        el.queueInfo.textContent =
          data.total_in_folder + " image(s) in " + data.side + " folder · " +
          data.already_annotated + " already annotated · " +
          data.images.length + " queued (" + data.mode + " mode)";

        if (!data.images.length) {
          el.canvas.classList.remove("ready");
          el.empty.style.display = "";
          el.empty.textContent = data.total_in_folder === 0
            ? "No images found in " + data.input_dir
            : "Nothing left to annotate on this side. Switch to Start Fresh to redo them.";
          renderProgress();
          renderLandmarkList();
          setStatus("Queue is empty.", "warn");
          return;
        }

        setStatus("Loaded " + data.images.length + " image(s).", "ok");
        loadCurrentImage();
      })
      .catch(function (err) { setStatus("Load failed: " + err.message, "err"); })
      .finally(function () { el.load.disabled = false; });
  }

  function loadCurrentImage() {
    var name = currentName();
    if (!name) {
      finishQueue();
      return;
    }

    state.points = [];
    state.cursor = null;
    state.img = null;
    state.pixels = null;
    state.shinTheta = null;
    state.ghost = null;
    state.shinOffImage = false;
    drag.active = false;
    drag.index = -1;

    if (state.objectUrl) {
      URL.revokeObjectURL(state.objectUrl);
      state.objectUrl = null;
    }

    applyGtSource(name);
    setStatus("Loading " + name + "...");
    renderProgress();
    renderLandmarkList();

    var url = "/api/image/" + encodeURIComponent(state.side) + "/" + encodeURIComponent(name);

    fetch(url)
      .then(function (r) {
        if (!r.ok) throw new Error(r.statusText);
        // The server rotates the image upright and reports its full size here;
        // the bytes themselves are a downscaled preview.
        state.fullW = parseInt(r.headers.get("X-Full-Width"), 10) || 0;
        state.fullH = parseInt(r.headers.get("X-Full-Height"), 10) || 0;
        return r.blob();
      })
      .then(function (blob) {
        return new Promise(function (resolve, reject) {
          var img = new Image();
          state.objectUrl = URL.createObjectURL(blob);
          img.onload = function () { resolve(img); };
          img.onerror = function () { reject(new Error("decode failed")); };
          img.src = state.objectUrl;
        });
      })
      .then(function (img) {
        if (currentName() !== name) return;   // queue moved on while loading
        // Fall back to the decoded size if the headers were unavailable.
        if (!state.fullW || !state.fullH) {
          state.fullW = img.naturalWidth;
          state.fullH = img.naturalHeight;
        }
        state.img = img;
        state.pixels = readPixels(img);
        el.empty.style.display = "none";
        el.canvas.classList.add("ready");
        fitCanvas();
        renderProgress();
        renderLandmarkList();
        setStatus("");
      })
      .catch(function (err) {
        setStatus("Could not load image " + name + ": " + err.message, "err");
      });
  }

  function finishQueue() {
    state.img = null;
    if (state.objectUrl) {
      URL.revokeObjectURL(state.objectUrl);
      state.objectUrl = null;
    }
    el.canvas.classList.remove("ready");
    el.empty.style.display = "";
    el.empty.textContent = "Queue complete. All images on this side are done.";
    el.progressText.textContent = "Done";
    el.progressFill.style.width = "100%";
    el.filename.textContent = "";
    el.dims.textContent = "";
    renderLandmarkList();
    setStatus("Finished the queue.", "ok");
  }

  function advance() {
    state.index += 1;
    if (state.index >= state.queue.length) {
      finishQueue();
    } else {
      loadCurrentImage();
    }
  }

  // ---------------------------------------------------------------- points

  function canvasPos(evt) {
    var rect = el.canvas.getBoundingClientRect();
    return { x: evt.clientX - rect.left, y: evt.clientY - rect.top };
  }

  /* Index of the placed point under a screen position, or -1. */
  function hitTest(pos) {
    var best = -1;
    var bestDist = HIT_RADIUS;
    for (var i = 0; i < state.points.length; i++) {
      var dx = state.points[i].x * state.scale - pos.x;
      var dy = state.points[i].y * state.scale - pos.y;
      var dist = Math.sqrt(dx * dx + dy * dy);
      if (dist <= bestDist) { bestDist = dist; best = i; }
    }
    return best;
  }

  /* Screen position -> full upright image pixels, clamped to the image. */
  function toOriginal(pos) {
    return {
      x: clamp(Math.round(pos.x / state.scale), 0, state.fullW - 1),
      y: clamp(Math.round(pos.y / state.scale), 0, state.fullH - 1)
    };
  }

  function onMouseDown(evt) {
    if (!state.img || evt.button !== 0) return;
    var hit = hitTest(canvasPos(evt));
    if (hit < 0) return;

    drag.active = true;
    drag.index = hit;
    drag.moved = false;
    evt.preventDefault();
    el.canvas.style.cursor = "grabbing";
    drawBuffer();
    paint();
    renderLandmarkList();
  }

  function onMouseMove(evt) {
    if (!state.img) return;
    var pos = canvasPos(evt);
    state.cursor = pos;

    if (drag.active) {
      drag.moved = true;
      if (isTarget(drag.index) && magnetReady()) {
        // Dragging the shin slides it along the circle. No edge snap here:
        // a drag is a deliberate manual adjustment.
        var theta = thetaFromScreen(pos);
        var p = pixelOnCircle(theta);
        if (p) {
          state.points[drag.index] = p;
          state.shinTheta = theta;
          state.shinOffImage = false;
        }
      } else {
        state.points[drag.index] = toOriginal(pos);
        reprojectShin();           // heel or 5th metatarsal moved: shin follows
      }
      drawBuffer();
      renderLandmarkList();
      schedulePaint();
      return;
    }

    state.ghost = placingShin() ? snapShin(pos) : null;
    el.canvas.style.cursor = hitTest(pos) >= 0 ? "grab" : "crosshair";
    schedulePaint();
  }

  function onMouseUp() {
    if (!drag.active) return;
    var lm = state.landmarks[drag.index];
    var p = state.points[drag.index];
    // A click event fires right after this; don't let it drop a new point.
    suppressClick = true;
    if (drag.moved) {
      setStatus("Moved " + lm.label + " to (" + p.x + ", " + p.y + ")", "ok");
    }
    drag.active = false;
    drag.index = -1;
    el.canvas.style.cursor = "crosshair";
    drawBuffer();
    paint();
    renderLandmarkList();
  }

  function onCanvasClick(evt) {
    if (suppressClick) { suppressClick = false; return; }
    if (!state.img) return;
    if (state.points.length >= state.landmarks.length) {
      setStatus("All " + state.landmarks.length +
                " points are placed. Drag one to adjust, or Reset.", "warn");
      return;
    }

    var lm = state.landmarks[state.points.length];

    if (isTarget(state.points.length)) {
      if (!magnetReady() || magnetRadius() === null) {
        setStatus("Place bottom_heel and 5th_metatarsal first: they define the " +
                  "shin circle.", "warn");
        return;
      }
      var snap = snapShin(canvasPos(evt));
      if (!snap.point) {
        setStatus("The circle leaves the image in that direction. Click elsewhere " +
                  "or lower the magnet factor.", "warn");
        return;
      }
      state.points.push(snap.point);
      state.shinTheta = snap.theta;
      state.shinOffImage = false;
      state.ghost = null;
      drawBuffer();
      paint();
      renderLandmarkList();
      var how = snap.edge && snap.edge.snapped
        ? "snapped to the leg edge (contrast " + Math.round(snap.edge.contrast) +
          " vs " + Math.round(snap.edge.rival) + ")"
        : (snap.edge ? "NOT edge-snapped - no single clear edge nearby (" +
                       Math.round(snap.edge.contrast) + " vs " +
                       Math.round(snap.edge.rival) + "); placed where you clicked, " +
                       "on the circle. Check it in the magnifier"
                     : "placed on the circle");
      setStatus(lm.label + " " + how + " at (" + snap.point.x + ", " + snap.point.y +
                "), R = " + magnetRadius().toFixed(1) + " px", "ok");
      return;
    }

    var p = toOriginal(canvasPos(evt));
    state.points.push(p);

    drawBuffer();
    paint();
    renderLandmarkList();
    setStatus(lm.label + " set at (" + p.x + ", " + p.y + ")", "ok");
  }

  function resetClicks() {
    if (!state.img) return;
    state.points = [];
    state.shinTheta = null;
    state.ghost = null;
    state.shinOffImage = false;
    drag.active = false;
    drawBuffer();
    paint();
    renderLandmarkList();
    setStatus("Cleared all points.");
  }

  function undoPoint() {
    if (!state.points.length) return;
    var removed = state.points.pop();
    if (state.magnet && state.points.length <= state.magnet.target) {
      state.shinTheta = null;
      state.shinOffImage = false;
    }
    state.ghost = null;
    drag.active = false;
    drawBuffer();
    paint();
    renderLandmarkList();
    setStatus("Removed point at (" + removed.x + ", " + removed.y + ").");
  }

  function skipImage() {
    if (!state.queue.length || !currentName()) return;
    setStatus("Skipped " + currentName() + ".", "warn");
    advance();
  }

  function saveAndNext() {
    if (state.saving) return;
    if (!state.img || state.points.length !== state.landmarks.length) {
      setStatus("Place all " + state.landmarks.length + " points first.", "warn");
      return;
    }
    if (!shinOnCircle()) {
      setStatus("The shin is not on the magnet circle (it may be off the image at " +
                "this radius). Lower the factor or drag the shin, then save.", "warn");
      return;
    }

    var name = currentName();
    state.saving = true;
    el.save.disabled = true;
    setStatus("Saving " + name + "...");

    fetch("/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: name,
        side: state.side,
        points: state.points,
        // The frame these coordinates were measured against, so the server can
        // detect a mismatch instead of trusting bad numbers.
        image_width: state.fullW,
        image_height: state.fullH,
        // The magnet radius the shin sits on; the server re-checks it.
        shin_radius_px: Math.round(magnetRadius() * 100) / 100,
        gt_source: selectedGtSource()
      })
    })
      .then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok) throw new Error(body.detail || r.statusText);
          return body;
        });
      })
      .then(function (data) {
        if (data.note) {
          setStatus(data.note, "warn");
        } else {
          setStatus("Saved " + name + " (" + data.row.gt_source + ", CSV row " +
                    data.csv_action + ").", "ok");
        }
        advance();
      })
      .catch(function (err) { setStatus("Save failed: " + err.message, "err"); })
      .finally(function () {
        state.saving = false;
        renderLandmarkList();
      });
  }

  // ---------------------------------------------------------------- events

  el.load.addEventListener("click", loadImages);
  el.save.addEventListener("click", saveAndNext);
  el.reset.addEventListener("click", resetClicks);
  el.skip.addEventListener("click", skipImage);
  el.undo.addEventListener("click", undoPoint);
  el.loupe.addEventListener("change", paint);
  el.side.addEventListener("change", renderPathInfo);

  if (el.magnetFactor) {
    el.magnetFactor.addEventListener("input", function () {
      setFactor(parseFloat(el.magnetFactor.value), false);
    });
  }
  if (el.edgeSnap) {
    el.edgeSnap.addEventListener("change", function () {
      writeStore(STORE_EDGE, el.edgeSnap.checked ? "1" : "0");
      state.ghost = placingShin() && state.cursor ? snapShin(state.cursor) : null;
      paint();
    });
  }

  // Scroll over the image resizes the circle once it exists (Shift = x5).
  el.canvas.addEventListener("wheel", function (evt) {
    if (!magnetReady() || !state.magnet) return;
    evt.preventDefault();
    var step = state.magnet.factor_step * (evt.shiftKey ? 5 : 1);
    setFactor(state.magnetFactor + (evt.deltaY < 0 ? step : -step), false);
    if (placingShin() && state.cursor) {
      state.ghost = snapShin(state.cursor);
      paint();
    }
  }, { passive: false });

  el.canvas.addEventListener("mousedown", onMouseDown);
  el.canvas.addEventListener("mousemove", onMouseMove);
  el.canvas.addEventListener("click", onCanvasClick);
  window.addEventListener("mouseup", onMouseUp);   // catch releases off-canvas

  el.canvas.addEventListener("mouseleave", function () {
    if (drag.active) return;   // keep the loupe alive mid-drag
    state.cursor = null;
    state.ghost = null;
    schedulePaint();
  });

  window.addEventListener("resize", fitCanvas);

  document.addEventListener("keydown", function (evt) {
    var tag = (evt.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea") return;

    if (evt.key === "Enter") { evt.preventDefault(); saveAndNext(); }
    else if (evt.key === "r" || evt.key === "R") { resetClicks(); }
    else if (evt.key === "s" || evt.key === "S") { skipImage(); }
    else if (evt.key === "Backspace") { evt.preventDefault(); undoPoint(); }
  });

  renderProgress();
})();
