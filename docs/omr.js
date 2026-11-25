/* ELNUR EDU — javob varag'ini kameradan o'qish (app/logic/omr.py ning brauzer nusxasi).
   Varaq o'lchamlari app/logic/sheets.py bilan bir xil bo'lishi shart. */
(function (root) {
  "use strict";
  var W = 595.28, H = 841.89, MARK = 24;
  var MARK_C = [[40, 40], [W - 40, 40], [40, H - 40], [W - 40, H - 40]];
  var ROWS = 20, ROW_H = 24, TOP = 268, COL_W = 165, COL_X0 = 62, BUB_DX = 27, BUB_X0 = 34, BUB_R = 8;
  // QR kodning o'zi (chegara bo'shlig'isiz) varaqdagi burchaklari, pt
  var QR_PTS = [[461.4, 79.4], [530.1, 79.4], [461.4, 148.1], [530.1, 148.1]];   // TL, TR, BL, BR
  
  function bubbleCenters(n) {
    var cols = Math.max(1, Math.ceil(n / ROWS));
    var xs = cols < 3 ? (W - cols * COL_W) / 2 + 10 : COL_X0, out = [];
    for (var i = 0; i < n; i++) {
      var c = Math.floor(i / ROWS), r = i % ROWS, y = TOP + r * ROW_H, x = xs + c * COL_W + BUB_X0, row = [];
      for (var k = 0; k < 4; k++) row.push([x + k * BUB_DX, y]);
      out.push(row);
    }
    return out;
  }

  // ---- v2 universal varaq (app/logic/sheet2.py: geom2 bilan aynan bir xil) ----
  var TOP2 = 236, BOT2 = 785, GX0 = 52, GW = 491, LBL = 36, VAR_Y = 204, VAR_X0 = 150, VAR_DX = 26, VAR_R = 7.5;
  function parseLayout(code) {
    var qs = [], nv = 0, parts = (code || "").split(".");
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      if (!p) continue;
      if (p[0] === "V") { nv = +p.slice(1); continue; }
      var kind = p[0], cn = p.slice(1).split("x"), c = +cn[0], n = +cn[1];
      for (var j = 0; j < n; j++) qs.push([kind, c]);
    }
    return { qs: qs, nv: nv };
  }
  function geom2(code) {
    var L = parseLayout(code), qs = L.qs, n = qs.length, maxb = 0, i;
    for (i = 0; i < n; i++) maxb = Math.max(maxb, qs[i][1]);
    if (!n) maxb = 4;
    var cmax = maxb <= 5 ? 4 : 3, best = null;
    for (var C = 1; C <= cmax; C++) {
      var R = n ? Math.ceil(n / C) : 1;
      var rh = R > 1 ? Math.min(24, (BOT2 - TOP2) / (R - 1)) : 24;
      var dx = Math.min(26, (GW / C - 40) / maxb);
      var r = Math.min(8, rh * 0.36, dx * 0.4);
      if (best === null || r > best[0] + 1e-9) best = [r, C, R, rh, dx];
    }
    var rr = best[0], CC = best[1], RR = best[2], RH = best[3], DX = best[4];
    var colw = Math.min(GW / CC, 40 + maxb * DX + 30), x0 = GX0 + (GW - CC * colw) / 2, rows = [];
    for (i = 0; i < n; i++) {
      var c = Math.floor(i / RR), k = i % RR, y = TOP2 + k * RH, bx = x0 + c * colw + LBL, row = [];
      for (var j = 0; j < qs[i][1]; j++) row.push([bx + j * DX, y]);
      rows.push(row);
    }
    var vr = [];
    for (i = 0; i < L.nv; i++) vr.push([VAR_X0 + i * VAR_DX, VAR_Y]);
    return { rows: rows, kinds: qs, r: rr, dx: DX, nv: L.nv, v: vr };
  }

  // ---- 4 nuqtali perspektiva (gomografiya): src -> dst ----
  function solve(A, b) {
    var n = b.length, i, j, k;
    for (i = 0; i < n; i++) A[i].push(b[i]);
    for (i = 0; i < n; i++) {
      var p = i;
      for (j = i + 1; j < n; j++) if (Math.abs(A[j][i]) > Math.abs(A[p][i])) p = j;
      var t = A[i]; A[i] = A[p]; A[p] = t;
      if (Math.abs(A[i][i]) < 1e-12) return null;
      for (j = i + 1; j < n; j++) {
        var f = A[j][i] / A[i][i];
        for (k = i; k <= n; k++) A[j][k] -= f * A[i][k];
      }
    }
    var x = new Array(n);
    for (i = n - 1; i >= 0; i--) {
      var s = A[i][n];
      for (j = i + 1; j < n; j++) s -= A[i][j] * x[j];
      x[i] = s / A[i][i];
    }
    return x;
  }
  function homography(src, dst) {
    var A = [], b = [];
    for (var i = 0; i < 4; i++) {
      var x = src[i][0], y = src[i][1], u = dst[i][0], v = dst[i][1];
      A.push([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.push(u);
      A.push([0, 0, 0, x, y, 1, -v * x, -v * y]); b.push(v);
    }
    var h = solve(A, b);
    return h ? h.concat([1]) : null;
  }
  function apply(h, x, y) {
    var d = h[6] * x + h[7] * y + h[8];
    return [(h[0] * x + h[1] * y + h[2]) / d, (h[3] * x + h[4] * y + h[5]) / d];
  }

  function toGray(rgba, w, h) {
    var g = new Uint8Array(w * h);
    for (var i = 0, j = 0; i < g.length; i++, j += 4) g[i] = (rgba[j] * 77 + rgba[j + 1] * 150 + rgba[j + 2] * 29) >> 8;
    return g;
  }

  // ---- qora kvadrat burchak belgilarini topish: kutilgan joy atrofida «qora kvadrat, atrofi oq» shablon ----
  function integral(g, w, h) {
    var I = new Float64Array((w + 1) * (h + 1));
    for (var y = 0; y < h; y++) {
      var s = 0;
      for (var x = 0; x < w; x++) { s += g[y * w + x]; I[(y + 1) * (w + 1) + x + 1] = I[y * (w + 1) + x + 1] + s; }
    }
    return I;
  }
  function boxMean(I, w, h, cx, cy, hw) {
    var x0 = Math.max(0, Math.round(cx - hw)), x1 = Math.min(w, Math.round(cx + hw)),
        y0 = Math.max(0, Math.round(cy - hw)), y1 = Math.min(h, Math.round(cy + hw));
    if (x1 <= x0 || y1 <= y0) return [0, 0];
    var W1 = w + 1;
    return [I[y1 * W1 + x1] - I[y0 * W1 + x1] - I[y1 * W1 + x0] + I[y0 * W1 + x0], (x1 - x0) * (y1 - y0)];
  }
  function markScore(I, w, h, cx, cy, s) {
    var a = boxMean(I, w, h, cx, cy, s * 0.32), b = boxMean(I, w, h, cx, cy, s * 0.78), c = boxMean(I, w, h, cx, cy, s * 0.9);
    if (!a[1] || c[1] - b[1] <= 0) return -1;
    var inner = a[0] / a[1], ring = (c[0] - b[0]) / (c[1] - b[1]);
    return ring - inner;
  }
  function findMark(g, I, w, h, px, py, s, R) {
    var best = -1, bx = 0, by = 0, x, y, st = Math.max(1, Math.round(s / 10));
    for (y = py - R; y <= py + R; y += st)
      for (x = px - R; x <= px + R; x += st) {
        if (x < s || y < s || x > w - s || y > h - s) continue;
        var v = markScore(I, w, h, x, y, s);
        if (v > best) { best = v; bx = x; by = y; }
      }
    if (best < 50) return null;
    for (y = by - st; y <= by + st; y++)                     // aniqlashtirish
      for (x = bx - st; x <= bx + st; x++) { var v2 = markScore(I, w, h, x, y, s); if (v2 > best) { best = v2; bx = x; by = y; } }
    // markaz: qora piksellar og'irlik markazi
    var a = boxMean(I, w, h, bx, by, s * 0.3), inner = a[0] / a[1], thr = inner + best / 2, sx = 0, sy = 0, n = 0, hw = Math.round(s * 0.8);
    for (y = Math.max(0, by - hw); y < Math.min(h, by + hw); y++)
      for (x = Math.max(0, bx - hw); x < Math.min(w, bx + hw); x++)
        if (g[y * w + x] < thr) { sx += x; sy += y; n++; }
    if (n < s * s * 0.5 || n > s * s * 1.6) return null;      // kvadrat o'lchamiga mos emas
    return [sx / n, sy / n];
  }

  function sampler(g, w, h, H) {
    return function (x, y) {
      var p = apply(H, x, y), u = Math.round(p[0]), v = Math.round(p[1]);
      if (u < 0 || v < 0 || u >= w || v >= h) return -1;
      return g[v * w + u];
    };
  }

  // Varaqdagi doirachalar sonini aniqlash (eski varaqlarda QR da yo'q): bosilgan halqalar bor-yo'qligi bo'yicha
  function ringScore(at, cx, y, R) {
    R = R || 8;
    var ring = 0, out = 0, rn = 0, on = 0;
    for (var a = 0; a < 24; a++) {
      var t = a * Math.PI / 12, m = 256, cs = Math.cos(t), sn = Math.sin(t);
      for (var rr = R - 1; rr <= R + 1.01; rr += 0.5) {
        var v = at(cx + rr * cs, y + rr * sn);
        if (v >= 0 && v < m) m = v;
      }
      if (m < 256) { ring += m; rn++; }
      var o = at(cx + (R + 3) * cs, y + (R + 3) * sn);
      if (o >= 0) { out += o; on++; }
    }
    if (!rn || !on) return 0;
    return 1 - (ring / rn) / (out / on);          // 0 — bo'sh qog'oz, katta — halqa bor
  }
  // Eski varaqlarda QR da savollar soni yo'q: ustunlar sonini birinchi qatorlardagi halqalardan aniqlaymiz.
  // Har ustun 20 savol; ortiqchasini bot testdagi haqiqiy savollar soni bo'yicha kesadi.
  function detectCols(at) {
    for (var cols = 3; cols >= 1; cols--) {
      var xs = cols < 3 ? (W - cols * COL_W) / 2 + 10 : COL_X0, hits = 0;
      for (var r = 0; r < 4; r++)
        for (var k = 0; k < 4; k++) if (ringScore(at, xs + BUB_X0 + k * BUB_DX, TOP + r * ROW_H) > 0.12) hits++;
      if (hits >= 12) return cols;
    }
    return 0;
  }

  // app/logic/omr.py dagi decide() bilan bir xil
  function decide(f) {
    var o = [0, 1, 2, 3].sort(function (a, b) { return f[b] - f[a]; }), b = f[o[0]], s2 = f[o[1]];
    if (b < 0.30) return "-";
    if (s2 >= 0.45) return "*";
    if ((b >= 0.38 && s2 < 0.30 && b - s2 >= 0.20) || (b >= 0.50 && s2 < b * 0.6)) return "ABCD"[o[0]];
    return b >= 0.38 ? "abcd"[o[0]] : "?";
  }

  // app/logic/omr.py dagi decide_k() bilan bir xil: [belgi, aniqmi]
  function decideK(f, labels) {
    if (f.length === 1) {
      if (f[0] >= 0.45) return [labels[0], true];
      if (f[0] < 0.30) return ["-", true];
      return [labels[0], false];
    }
    var o = [];
    for (var i = 0; i < f.length; i++) o.push(i);
    o.sort(function (a, b) { return f[b] - f[a]; });
    var b = f[o[0]], s2 = f[o[1]];
    if (b < 0.30) return ["-", true];
    if (s2 >= 0.45) return ["*", true];
    if ((b >= 0.38 && s2 < 0.30 && b - s2 >= 0.20) || (b >= 0.50 && s2 < b * 0.6)) return [labels[o[0]], true];
    return b >= 0.38 ? [labels[o[0]], false] : ["?", false];
  }
  // noaniq belgini bitta harfga o'raymiz: A..F → a..f, 1..5 → p..t (bot qayta ochadi)
  var GUESS = { A: "a", B: "b", C: "c", D: "d", E: "e", F: "f", "1": "p", "2": "q", "3": "r", "4": "s", "5": "t" };

  function percentile(arr, q) {
    if (!arr.length) return 255;
    arr.sort(function (a, b) { return a - b; });
    return arr[Math.min(arr.length - 1, Math.floor(q * arr.length))];
  }

  /* rgba: Uint8ClampedArray, qrFn: jsQR. Qaytaradi: {pid, n, answers, unsure, marks, error} */
  function readFrame(rgba, w, h, qrFn) {
    var q = qrFn(rgba, w, h, { inversionAttempts: "dontInvert" });
    if (!q) return { error: "noqr" };
    var m = /^ELN:(\d+)(?::(\d+))?$/.exec(q.data || ""), m2 = /^E2:(\d+):([A-Za-z0-9x.]+)$/.exec(q.data || "");
    if (!m && !m2) return { error: "foreign" };
    var pid = m ? +m[1] : "m" + m2[1], nQr = m && m[2] ? +m[2] : 0, L = q.location;
    var QP = QR_PTS;
    if (m2) {          // v2: QR o'lchami versiyaga bog'liq: ichki kvadrat burchaklari
      var mods = 17 + 4 * (q.version || 1), off = 96 * 4 / (mods + 8), sz = 96 * mods / (mods + 8);
      QP = [[448 + off, 66 + off], [448 + off + sz, 66 + off], [448 + off, 66 + off + sz], [448 + off + sz, 66 + off + sz]];
    }
    var Hq = homography(QP, [[L.topLeftCorner.x, L.topLeftCorner.y], [L.topRightCorner.x, L.topRightCorner.y],
                                 [L.bottomLeftCorner.x, L.bottomLeftCorner.y], [L.bottomRightCorner.x, L.bottomRightCorner.y]]);
    if (!Hq) return { pid: pid, error: "marks" };
    var g = toGray(rgba, w, h);
    var qside = Math.hypot(L.topRightCorner.x - L.topLeftCorner.x, L.topRightCorner.y - L.topLeftCorner.y);
    var markPx = qside * MARK / (QP[1][0] - QP[0][0]);
    var I = integral(g, w, h), pts = [], sheetW = qside * W / (QP[1][0] - QP[0][0]);
    for (var i = 0; i < 4; i++) {
      var pr = apply(Hq, MARK_C[i][0], MARK_C[i][1]);
      var p = findMark(g, I, w, h, Math.round(pr[0]), Math.round(pr[1]), markPx, Math.round(sheetW * 0.16));
      if (!p) return { pid: pid, error: "marks" };
      pts.push(p);
    }
    for (i = 0; i < 4; i++) for (var j = i + 1; j < 4; j++)
      if (Math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]) < sheetW * 0.3) return { pid: pid, error: "marks" };
    var Hs = homography(MARK_C, pts);
    if (!Hs) return { pid: pid, error: "marks" };
    // tekshiruv 1: QR kod burchaklari topilgan belgilar bo'yicha o'z joyiga tushishi kerak
    var qc = [L.topLeftCorner, L.topRightCorner, L.bottomLeftCorner, L.bottomRightCorner];
    for (i = 0; i < 4; i++) {
      var pq = apply(Hs, QP[i][0], QP[i][1]);
      if (Math.hypot(pq[0] - qc[i].x, pq[1] - qc[i].y) > qside * 0.12) return { pid: pid, error: "marks" };
    }
    var at = sampler(g, w, h, Hs);
    if (m2) return readV2(at, pid, m2[2], pts);
    var n = nQr;
    if (!n) { var cols = detectCols(at); if (!cols) return { pid: pid, error: "marks" }; n = cols * ROWS; }
    // tekshiruv 2: doirachalar halqalari kutilgan joyda bo'lishi kerak
    var chk = bubbleCenters(n), seen = 0, all = 0, lim = nQr ? chk.length : 4;
    for (i = 0; i < lim; i += Math.max(1, Math.floor(lim / 12)))
      for (var kk = 0; kk < 4; kk += 3) { all++; if (ringScore(at, chk[i][kk][0], chk[i][kk][1]) > 0.12) seen++; }
    if (seen < all * 0.8) return { pid: pid, error: "marks" };
    var rows = bubbleCenters(n), r = BUB_R * 0.62, answers = "", unsure = [], allFills = [];
    for (i = 0; i < n; i++) {
      var row = rows[i], y0 = row[0][1], band = [];
      for (var yy = y0 - 3 * r; yy <= y0 + 3 * r; yy += 2)
        for (var xx = row[0][0] - 3 * r; xx <= row[3][0] + 3 * r; xx += 2) { var v = at(xx, yy); if (v >= 0) band.push(v); }
      var paper = percentile(band, 0.9), fills = [];
      for (var k = 0; k < 4; k++) {
        var s = 0, c = 0;
        for (var dy = -r; dy <= r; dy += 0.75)
          for (var dx = -r; dx <= r; dx += 0.75) {
            if (dx * dx + dy * dy > r * r) continue;
            var vv = at(row[k][0] + dx, row[k][1] + dy);
            if (vv >= 0) { s += vv; c++; }
          }
        fills.push(c && paper > 0 ? Math.max(0, 1 - s / c / paper) : 0);
      }
      allFills.push(fills);
      var ch = decide(fills);
      answers += ch;
      if ("ABCD-*".indexOf(ch) < 0) unsure.push(i + 1);   // "*" — ikkita belgilangan: xato
    }
    return { pid: pid, n: n, answers: answers, unsure: unsure, marks: pts, fills: allFills };
  }

  function fillsAt(at, centers, r) {
    var y0 = centers[0][1], band = [], fills = [], rs = r * 0.62;
    for (var yy = y0 - 3 * rs; yy <= y0 + 3 * rs; yy += 2)
      for (var xx = centers[0][0] - 3 * rs; xx <= centers[centers.length - 1][0] + 3 * rs; xx += 2) {
        var v = at(xx, yy); if (v >= 0) band.push(v);
      }
    var paper = percentile(band, 0.9);
    for (var k = 0; k < centers.length; k++) {
      var s = 0, c = 0;
      for (var dy = -rs; dy <= rs; dy += 0.75)
        for (var dx = -rs; dx <= rs; dx += 0.75) {
          if (dx * dx + dy * dy > rs * rs) continue;
          var vv = at(centers[k][0] + dx, centers[k][1] + dy);
          if (vv >= 0) { s += vv; c++; }
        }
      fills.push(c && paper > 0 ? Math.max(0, 1 - s / c / paper) : 0);
    }
    return fills;
  }

  function readV2(at, pid, layout, pts) {
    var g = geom2(layout), rows = g.rows, n = rows.length, i, seen = 0, all = 0;
    if (!n) return { pid: pid, error: "marks" };
    for (i = 0; i < n; i += Math.max(1, Math.floor(n / 12))) {
      var row = rows[i];
      all += 2;
      if (ringScore(at, row[0][0], row[0][1], g.r) > 0.12) seen++;
      if (ringScore(at, row[row.length - 1][0], row[row.length - 1][1], g.r) > 0.12) seen++;
    }
    if (seen < all * 0.8) return { pid: pid, error: "marks" };
    var answers = "", unsure = [];
    for (i = 0; i < n; i++) {
      var kind = g.kinds[i][0], nb = g.kinds[i][1];
      var labels = kind === "t" ? "ABCDEF".slice(0, nb) : "123456789".slice(0, nb);
      var d = decideK(fillsAt(at, rows[i], g.r), labels);
      if (d[1]) answers += d[0];
      else { answers += d[0] === "?" ? "?" : (GUESS[d[0]] || "?"); unsure.push(i + 1); }
    }
    if (g.nv) {
      var dv = decideK(fillsAt(at, g.v, VAR_R), "ABCDEF".slice(0, g.nv));
      answers += "@" + (dv[1] && dv[0].length === 1 && dv[0] !== "-" && dv[0] !== "*" ? dv[0] : "?");
    }
    return { pid: pid, n: n, answers: answers, unsure: unsure, marks: pts };
  }

  var api = { readFrame: readFrame, geom2: geom2, bubbleCenters: bubbleCenters, homography: homography, apply: apply };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.ElnOmr = api;
})(this);
