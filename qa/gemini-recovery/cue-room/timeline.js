/**
 * Cue Room — Multitrack Arrangement Timeline View & Direct Manipulation Engine
 * Browser-global `CueTimeline` + CommonJS UMD module.
 */
(function (root, factory) {
  'use strict';
  var m = null;
  if (typeof module !== 'undefined' && module.exports) {
    try { m = require('./model.js'); } catch (_) { m = null; }
  } else if (root && root.CueModel) {
    m = root.CueModel;
  }
  var api = factory(m, root);
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.CueTimeline = api;
})(typeof globalThis !== 'undefined' ? globalThis : typeof window !== 'undefined' ? window : this, function (defaultModel, rootObj) {
  'use strict';

  var EPS = 1e-6;
  var STEPS_PER_BEAT = 4;
  var ZOOM_LEVELS = Object.freeze([0.5, 0.75, 1, 1.25, 1.5, 2]);
  var MAX_VISUAL_NOTES = 128;
  var KIND_LABELS = { kick: 'Kick', snare: 'Snare', hat: 'Hat', bass: 'Bass', pad: 'Pad', synth: 'Synth' };

  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
  function r2(v) { return Math.round(v * 100) / 100; }
  function r4(v) { return Math.round(v * 10000) / 10000; }
  function resolveModel(o) {
    var m = o || defaultModel || (rootObj && rootObj.CueModel);
    if (!m) throw new Error('CueModel is required for CueTimeline');
    return m;
  }

  // --- Pure Formatting, Deterministic Pattern Visualization & Drag Clamping ---

  function formatBeatPosition(beat, bpb) {
    bpb = bpb || 4;
    var safe = Math.max(0, Number.isFinite(beat) ? beat : 0);
    var bar = Math.floor(safe / bpb) + 1;
    var bib = (safe % bpb) + 1, whole = Math.floor(bib + EPS), frac = bib - whole;
    return 'Bar ' + bar + ', Beat ' + whole + (frac > 0.05 ? '.' + Math.round(frac * 4) : '');
  }

  function formatClipLength(dur, bpb) {
    bpb = bpb || 4;
    var d = Math.max(0, Number.isFinite(dur) ? dur : 0);
    if (d >= bpb - EPS) {
      var bars = d / bpb;
      return (Math.abs(bars - Math.round(bars)) < 1e-4 ? Math.round(bars) : r2(bars)) + 'b';
    }
    return r2(d) + 'bt';
  }

  function clipAriaLabel(cl, tr, bpb) {
    return cl.name + ' on ' + (tr ? tr.name : cl.trackId) + ', bar ' + r2(cl.startBeat / bpb + 1) +
      ' to bar ' + r2((cl.startBeat + cl.durationBeats) / bpb + 1) + ' (' + formatClipLength(cl.durationBeats, bpb) + ')';
  }

  function noteYPercent(type, n, minP, span) {
    if (type === 'kick') return n.velocity < 0.72 ? 70 : 60;
    if (type === 'snare') return n.pitch === 39 ? 24 : (n.velocity < 0.55 ? 60 : 46);
    if (type === 'hat') return (n.pitch === 46 || n.durationSteps >= 0.9) ? 22 : (n.velocity >= 0.65 ? 40 : 56);
    return r2(clamp(76 - ((n.pitch - minP) / span) * 60, 14, 78));
  }

  function buildClipPatternVisuals(clip, track) {
    var notes = Array.isArray(clip && clip.notes) ? clip.notes : [];
    if (!notes.length) return [];
    var type = track ? track.type : 'synth';
    var totalSteps = Math.max(1, Math.round((clip.durationBeats || 4) * STEPS_PER_BEAT));
    var patSteps = Math.max(1, clip.patternSteps || 16), cycles = Math.ceil(totalSteps / patSteps);
    var gain = Number.isFinite(clip.gain) ? clamp(clip.gain, 0, 1) : 1;

    var minP = Infinity, maxP = -Infinity, i, cy, n;
    for (i = 0; i < notes.length; i++) {
      if (notes[i].pitch < minP) minP = notes[i].pitch;
      if (notes[i].pitch > maxP) maxP = notes[i].pitch;
    }
    var span = maxP - minP;
    if (span < 12) { minP -= (12 - span) / 2; span = 12; }

    var out = [];
    for (cy = 0; cy < cycles; cy++) {
      for (n = 0; n < notes.length; n++) {
        var nt = notes[n], abs = cy * patSteps + nt.step;
        if (abs >= totalSteps) continue;
        var dur = Math.min(nt.durationSteps, totalSteps - abs);
        if (dur <= 0) continue;
        var x = (abs / totalSteps) * 100;
        var w = Math.min(100 - x, Math.max(1.4, (dur / totalSteps) * 100 * 0.88));
        out.push({ xPct: r2(x), wPct: r2(w), yPct: noteYPercent(type, nt, minP, span), v: r2(clamp(nt.velocity * gain, 0.12, 1)) });
      }
    }
    if (out.length <= MAX_VISUAL_NOTES) return out;
    var sampled = [], stride = out.length / MAX_VISUAL_NOTES;
    for (i = 0; i < MAX_VISUAL_NOTES; i++) sampled.push(out[Math.floor(i * stride)]);
    return sampled;
  }

  function patternSig(cl, type) {
    var ns = cl.notes || [], a = [type, cl.durationBeats, cl.patternSteps, cl.gain, ns.length];
    for (var i = 0; i < ns.length; i++) a.push(ns[i].step + ':' + ns[i].pitch + ':' + ns[i].velocity + ':' + ns[i].durationSteps);
    return a.join('|');
  }

  function otherClips(p, tid, ex) {
    var ids = (p.trackClipIds && p.trackClipIds[tid]) || [], out = [];
    for (var i = 0; i < ids.length; i++) if (ids[i] !== ex && p.clipsById[ids[i]]) out.push(p.clipsById[ids[i]]);
    return out;
  }

  function overlaps(clips, s, d) {
    var e = s + d;
    for (var i = 0; i < clips.length; i++) if (s < clips[i].startBeat + clips[i].durationBeats - EPS && e > clips[i].startBeat + EPS) return true;
    return false;
  }

  function resolveMoveStart(p, tid, cid, desired, dur, m) {
    var tot = m.getTotalBeats(p);
    if (dur > tot + EPS) return null;
    var tgt = clamp(m.quantizeBeat(desired), 0, m.quantizeBeat(tot - dur));
    var oth = otherClips(p, tid, cid);
    if (!overlaps(oth, tgt, dur)) return tgt;
    var cs = [], end = tgt + dur;
    for (var i = 0; i < oth.length; i++) {
      var o = oth[i], oE = o.startBeat + o.durationBeats;
      if (tgt < oE - EPS && end > o.startBeat + EPS) {
        var L = m.quantizeBeat(o.startBeat - dur), R = m.quantizeBeat(oE);
        if (L >= -EPS && L + dur <= tot + EPS && !overlaps(oth, L, dur)) cs.push(Math.max(0, L));
        if (R >= -EPS && R + dur <= tot + EPS && !overlaps(oth, R, dur)) cs.push(R);
      }
    }
    if (!cs.length) return null;
    cs.sort(function (a, b) { var d = Math.abs(a - tgt) - Math.abs(b - tgt); return Math.abs(d) > EPS ? d : a - b; });
    return cs[0];
  }

  function computeDragPreview(p, drag, deltaBeats, hoverTid, mOverride) {
    var m = resolveModel(mOverride), tot = m.getTotalBeats(p), minD = m.LIMITS.MIN_CLIP_BEATS;
    if (drag.mode === 'move') {
      var tId = (hoverTid && p.tracksById[hoverTid]) ? hoverTid : drag.origTrackId;
      var des = drag.origStartBeat + deltaBeats;
      var s = resolveMoveStart(p, tId, drag.clipId, des, drag.origDurationBeats, m);
      if (s !== null) return { valid: true, trackId: tId, startBeat: s, durationBeats: drag.origDurationBeats };
      if (tId !== drag.origTrackId) {
        var fb = resolveMoveStart(p, drag.origTrackId, drag.clipId, des, drag.origDurationBeats, m);
        if (fb !== null) return { valid: true, trackId: drag.origTrackId, startBeat: fb, durationBeats: drag.origDurationBeats };
      }
      return { valid: false, trackId: drag.previewTrackId, startBeat: drag.previewStartBeat, durationBeats: drag.origDurationBeats };
    }
    var ids = (p.trackClipIds && p.trackClipIds[drag.origTrackId]) || [], idx = ids.indexOf(drag.clipId);
    var prev = idx > 0 ? p.clipsById[ids[idx - 1]] : null;
    var next = idx !== -1 && idx < ids.length - 1 ? p.clipsById[ids[idx + 1]] : null;
    var minS = prev ? m.quantizeBeat(prev.startBeat + prev.durationBeats) : 0;
    var maxE = next ? m.quantizeBeat(next.startBeat) : tot;
    var origE = m.quantizeBeat(drag.origStartBeat + drag.origDurationBeats);
    if (drag.mode === 'resize-left') {
      var nS = clamp(m.quantizeBeat(drag.origStartBeat + deltaBeats), minS, m.quantizeBeat(origE - minD));
      return { valid: true, trackId: drag.origTrackId, startBeat: nS, durationBeats: m.quantizeBeat(origE - nS) };
    }
    var nD = clamp(m.quantizeBeat(drag.origDurationBeats + deltaBeats), minD, m.quantizeBeat(maxE - drag.origStartBeat));
    return { valid: true, trackId: drag.origTrackId, startBeat: drag.origStartBeat, durationBeats: nD };
  }

  // --- DOM Timeline Controller Factory ---

  function createTimeline(options) {
    var opts = options || {}, model = resolveModel(opts.model), store = opts.store || null;
    var doc = opts.document || (typeof document !== 'undefined' ? document : null);
    var win = opts.window || (typeof window !== 'undefined' ? window : null);
    if (!doc) throw new Error('DOM document is required for CueTimeline.createTimeline');

    var els = opts.elements || {};
    var scrollEl = els.scroll || doc.getElementById('timeline-scroll');
    var gridEl = els.grid || doc.getElementById('timeline-grid');
    var rulerEl = els.ruler || doc.getElementById('timeline-ruler');
    var handleEl = els.playheadHandle || doc.getElementById('playhead-handle');
    var tracksEl = els.tracks || doc.getElementById('timeline-tracks');
    var lineEl = els.playheadLine || doc.getElementById('playhead-line');
    var summaryEl = els.summary || doc.getElementById('arrangement-summary');
    var zoomReadoutEl = els.zoomReadout || doc.getElementById('zoom-readout');
    var zoomInBtn = els.zoomIn || doc.getElementById('btn-zoom-in');
    var zoomOutBtn = els.zoomOut || doc.getElementById('btn-zoom-out');

    var onSeek = typeof opts.onSeek === 'function' ? opts.onSeek : null;
    var onStatus = typeof opts.onStatus === 'function' ? opts.onStatus : null;
    var onTrackVolumePreview = typeof opts.onTrackVolumePreview === 'function' ? opts.onTrackVolumePreview : null;
    var onClipAudition = typeof opts.onClipAudition === 'function' ? opts.onClipAudition : null;

    var trackRowMap = new Map(), clipElMap = new Map();
    var lastState = null, renderedBars = 0, playheadBeat = 0, lastAriaStep = -1;
    var zoomIndex = Math.max(0, ZOOM_LEVELS.indexOf(1));
    var activeDrag = null, activeScrub = null, activeVolTid = null, deferredState = null, disposed = false;

    function announce(msg, tone) { if (onStatus) onStatus(msg, tone || 'neutral'); }
    function setAttrs(el, map) { var k = Object.keys(map); for (var i = 0; i < k.length; i++) el.setAttribute(k[i], map[k[i]]); return el; }
    function mk(tag, cls, map, txt) {
      var el = doc.createElement(tag);
      if (cls) el.className = cls;
      if (map) setAttrs(el, map);
      if (txt !== undefined) el.textContent = txt;
      return el;
    }
    function bindAll(el, hs, add) {
      if (!el) return;
      var fn = add ? 'addEventListener' : 'removeEventListener', ks = Object.keys(hs);
      for (var i = 0; i < ks.length; i++) el[fn](ks[i], hs[ks[i]]);
    }
    function releaseCap(el, pid) {
      if (el && typeof el.releasePointerCapture === 'function') {
        try { if (el.hasPointerCapture(pid)) el.releasePointerCapture(pid); } catch (_) {}
      }
    }

    function baseBeatW() { return (win && typeof win.innerWidth === 'number' && win.innerWidth <= 767) ? 26 : 34; }

    function getBeatWidthPx() {
      if (rulerEl && lastState) {
        var tot = model.getTotalBeats(lastState.project), w = rulerEl.getBoundingClientRect().width;
        if (w > 0 && tot > 0) return w / tot;
      }
      if (gridEl && win && typeof win.getComputedStyle === 'function') {
        var v = parseFloat(win.getComputedStyle(gridEl).getPropertyValue('--beat-width'));
        if (Number.isFinite(v) && v > 0) return v;
      }
      return baseBeatW() * ZOOM_LEVELS[zoomIndex];
    }

    function getHeaderWidthPx() {
      var c = gridEl ? gridEl.querySelector('.ruler-corner') : null;
      if (c) { var w = c.getBoundingClientRect().width; if (w > 0) return w; }
      if (gridEl && win && typeof win.getComputedStyle === 'function') {
        var v = parseFloat(win.getComputedStyle(gridEl).getPropertyValue('--header-width'));
        if (Number.isFinite(v) && v > 0) return v;
      }
      return baseBeatW() === 26 ? 132 : 212;
    }

    function beatFromX(x, ref) { var t = ref || rulerEl, bw = getBeatWidthPx(); return (t && bw) ? (x - t.getBoundingClientRect().left) / bw : 0; }

    function trackAtY(y) {
      var it = trackRowMap.entries(), st = it.next();
      while (!st.done) {
        var r = st.value[1].row.getBoundingClientRect();
        if (y >= r.top && y <= r.bottom) return st.value[0];
        st = it.next();
      }
      return null;
    }

    function ensureBeatVisible(beat, sOpts) {
      if (!scrollEl || !lastState) return;
      var bw = getBeatWidthPx(), lw = scrollEl.clientWidth - getHeaderWidthPx();
      if (lw <= 24) return;
      var px = clamp(beat, 0, model.getTotalBeats(lastState.project)) * bw, pad = Math.min(48, lw * 0.18);
      if (px < scrollEl.scrollLeft + pad) scrollEl.scrollLeft = Math.max(0, px - pad);
      else if (px > scrollEl.scrollLeft + lw - pad) scrollEl.scrollLeft = Math.max(0, px - ((sOpts && sOpts.followPlayhead) ? lw * 0.25 : lw - pad));
    }

    function applyZoom(nextIdx, keepCenter) {
      var idx = clamp(nextIdx, 0, ZOOM_LEVELS.length - 1), f = ZOOM_LEVELS[idx], ctr = null;
      if (keepCenter && scrollEl && lastState) {
        var oldBw = getBeatWidthPx(), lw = Math.max(0, scrollEl.clientWidth - getHeaderWidthPx());
        if (oldBw > 0) ctr = (scrollEl.scrollLeft + lw * 0.5) / oldBw;
      }
      zoomIndex = idx;
      var newBw = r2(baseBeatW() * f);
      if (gridEl) gridEl.style.setProperty('--beat-width', newBw + 'px');
      if (zoomReadoutEl) zoomReadoutEl.textContent = Math.round(f * 100) + '%';
      if (zoomOutBtn) zoomOutBtn.disabled = zoomIndex <= 0;
      if (zoomInBtn) zoomInBtn.disabled = zoomIndex >= ZOOM_LEVELS.length - 1;
      if (ctr !== null && scrollEl) scrollEl.scrollLeft = Math.max(0, ctr * newBw - Math.max(0, scrollEl.clientWidth - getHeaderWidthPx()) * 0.5);
      return f;
    }

    // --- Seeded DOM Adoption & Keyed Builders ---

    function hydrateDom() {
      if (!tracksEl) return;
      var rows = tracksEl.querySelectorAll('.track-row[data-track-id]');
      for (var i = 0; i < rows.length; i++) {
        var row = rows[i], tid = row.getAttribute('data-track-id');
        if (!tid || trackRowMap.has(tid)) continue;
        var rec = {
          row: row, selectBtn: row.querySelector('.track-select-btn'),
          nameEl: row.querySelector('.track-name'), kindEl: row.querySelector('.track-kind'),
          muteBtn: row.querySelector('.btn-mute'), soloBtn: row.querySelector('.btn-solo'),
          volSlider: row.querySelector('.track-vol-slider'), volReadout: row.querySelector('.track-vol-readout'),
          lane: row.querySelector('.track-lane')
        };
        trackRowMap.set(tid, rec);
        if (rec.lane) {
          var clips = rec.lane.querySelectorAll('.clip-block[data-clip-id]');
          for (var j = 0; j < clips.length; j++) {
            var cEl = clips[j], cid = cEl.getAttribute('data-clip-id');
            if (!cid || clipElMap.has(cid)) continue;
            clipElMap.set(cid, { el: cEl, titleEl: cEl.querySelector('.clip-title'), lenEl: cEl.querySelector('.clip-len'), patternEl: cEl.querySelector('.clip-pattern') });
          }
        }
      }
      if (rulerEl) renderedBars = rulerEl.querySelectorAll('.ruler-bar').length;
    }

    function createTrackRowDom(tr) {
      var row = mk('div', 'track-row', { role: 'listitem', 'data-track-id': tr.id, 'data-track-type': tr.type });
      var hdr = mk('div', 'track-header', { 'data-action': 'select-track', 'data-track-id': tr.id });
      var top = mk('div', 'track-header-top');
      var selBtn = mk('button', 'track-select-btn', { type: 'button', 'data-action': 'select-track', 'data-track-id': tr.id });
      var nameEl = mk('span', 'track-name'), kindEl = mk('span', 'track-kind');
      selBtn.appendChild(nameEl); selBtn.appendChild(kindEl);
      var ms = mk('div', 'track-ms');
      var muteBtn = mk('button', 'btn-ms btn-mute', { type: 'button', 'data-action': 'toggle-mute', 'data-track-id': tr.id }, 'M');
      var soloBtn = mk('button', 'btn-ms btn-solo', { type: 'button', 'data-action': 'toggle-solo', 'data-track-id': tr.id }, 'S');
      ms.appendChild(muteBtn); ms.appendChild(soloBtn);
      top.appendChild(selBtn); top.appendChild(ms);
      var bot = mk('div', 'track-header-bottom');
      var volSlider = mk('input', 'track-vol-slider', { type: 'range', min: '0', max: '100', step: '1', 'data-action': 'track-volume', 'data-track-id': tr.id });
      var volReadout = mk('span', 'track-vol-readout');
      bot.appendChild(volSlider); bot.appendChild(volReadout);
      hdr.appendChild(mk('span', 'track-rail', { 'aria-hidden': 'true' })); hdr.appendChild(top); hdr.appendChild(bot);
      var lane = mk('div', 'track-lane', { 'data-track-id': tr.id, 'data-track-type': tr.type });
      row.appendChild(hdr); row.appendChild(lane);
      return { row: row, selectBtn: selBtn, nameEl: nameEl, kindEl: kindEl, muteBtn: muteBtn, soloBtn: soloBtn, volSlider: volSlider, volReadout: volReadout, lane: lane };
    }

    function createClipDom(cl) {
      var el = mk('div', 'clip-block', { role: 'button', tabindex: '0', 'data-clip-id': cl.id, 'data-track-id': cl.trackId });
      var hdr = mk('div', 'clip-header'), titleEl = mk('span', 'clip-title'), lenEl = mk('span', 'clip-len');
      hdr.appendChild(titleEl); hdr.appendChild(lenEl);
      var patternEl = mk('div', 'clip-pattern', { 'aria-hidden': 'true' });
      el.appendChild(mk('span', 'clip-resize-handle is-left', { 'data-resize': 'left', 'aria-hidden': 'true' }));
      el.appendChild(hdr); el.appendChild(patternEl);
      el.appendChild(mk('span', 'clip-resize-handle is-right', { 'data-resize': 'right', 'aria-hidden': 'true' }));
      return { el: el, titleEl: titleEl, lenEl: lenEl, patternEl: patternEl };
    }

    function renderPatternBars(pEl, cl, tr) {
      if (!pEl) return;
      var sig = patternSig(cl, tr ? tr.type : 'synth');
      if (pEl._cueSig === sig) return;
      pEl._cueSig = sig;
      var bars = buildClipPatternVisuals(cl, tr), frag = doc.createDocumentFragment();
      for (var i = 0; i < bars.length; i++) {
        var b = bars[i], node = doc.createElement('i');
        node.style.cssText = '--x:' + b.xPct + '%;--w:' + b.wPct + '%;--y:' + b.yPct + '%;--v:' + b.v;
        frag.appendChild(node);
      }
      pEl.textContent = ''; pEl.appendChild(frag);
    }

    function reconcileRuler(lenBars, bpb) {
      if (!rulerEl) return;
      rulerEl.setAttribute('aria-valuemax', String(lenBars * bpb));
      if (renderedBars === lenBars) return;
      var cur = rulerEl.querySelectorAll('.ruler-bar');
      for (var i = cur.length - 1; i >= lenBars; i--) cur[i].parentNode.removeChild(cur[i]);
      for (var b = cur.length + 1; b <= lenBars; b++) {
        var barEl = mk('div', 'ruler-bar', { 'data-bar': String(b) });
        barEl.appendChild(mk('span', 'bar-num', null, String(b)));
        var ticks = mk('span', 'beat-ticks', { 'aria-hidden': 'true' });
        for (var t = 0; t < bpb - 1; t++) ticks.appendChild(doc.createElement('i'));
        barEl.appendChild(ticks);
        if (handleEl && handleEl.parentNode === rulerEl) rulerEl.insertBefore(barEl, handleEl);
        else rulerEl.appendChild(barEl);
      }
      renderedBars = lenBars;
    }

    function render(state) {
      if (disposed || !state || !state.project) return;
      lastState = state;
      if (activeDrag) { deferredState = state; return; }

      var p = state.project, sel = state.selection || { trackId: null, clipId: null };
      var totB = model.getTotalBeats(p);
      if (gridEl) gridEl.style.setProperty('--total-beats', String(totB));
      reconcileRuler(p.lengthBars, p.beatsPerBar);
      if (summaryEl) {
        var nc = Object.keys(p.clipsById).length;
        summaryEl.textContent = p.trackOrder.length + ' tracks \u00b7 ' + nc + (nc === 1 ? ' clip' : ' clips') + ' \u00b7 ' + p.lengthBars + ' bars';
      }
      if (!tracksEl) return;

      var seenT = {}, seenC = {}, i, c;
      if (lineEl && lineEl.parentNode === tracksEl && tracksEl.firstChild !== lineEl) tracksEl.insertBefore(lineEl, tracksEl.firstChild);
      var rowAnchor = (lineEl && lineEl.parentNode === tracksEl) ? lineEl : null;

      for (i = 0; i < p.trackOrder.length; i++) {
        var tid = p.trackOrder[i], tr = p.tracksById[tid];
        seenT[tid] = true;
        var rec = trackRowMap.get(tid);
        if (!rec) { rec = createTrackRowDom(tr); trackRowMap.set(tid, rec); }
        var nextRow = rowAnchor ? rowAnchor.nextSibling : tracksEl.firstChild;
        if (rec.row !== nextRow) tracksEl.insertBefore(rec.row, nextRow);
        rowAnchor = rec.row;

        var isSelT = sel.trackId === tid, isDim = !model.isTrackAudible(p, tid);
        setAttrs(rec.row, { 'data-track-type': tr.type, 'data-selected': String(isSelT), 'data-muted': String(tr.mute), 'data-solo': String(tr.solo), 'data-dimmed': String(isDim) });
        if (rec.lane) rec.lane.setAttribute('data-track-type', tr.type);
        if (rec.selectBtn) rec.selectBtn.setAttribute('aria-pressed', String(isSelT));
        if (rec.nameEl && rec.nameEl.textContent !== tr.name) rec.nameEl.textContent = tr.name;
        var kLbl = KIND_LABELS[tr.type] || 'Synth';
        if (rec.kindEl && rec.kindEl.textContent !== kLbl) rec.kindEl.textContent = kLbl;
        if (rec.muteBtn) setAttrs(rec.muteBtn, { 'aria-pressed': String(tr.mute), 'aria-label': (tr.mute ? 'Unmute ' : 'Mute ') + tr.name });
        if (rec.soloBtn) setAttrs(rec.soloBtn, { 'aria-pressed': String(tr.solo), 'aria-label': (tr.solo ? 'Unsolo ' : 'Solo ') + tr.name });

        var pct = Math.round(clamp(tr.volume, 0, 1) * 100);
        if (rec.volSlider && activeVolTid !== tid) {
          if (rec.volSlider.value !== String(pct)) rec.volSlider.value = String(pct);
          rec.volSlider.setAttribute('aria-label', tr.name + ' track volume');
        }
        if (rec.volReadout && activeVolTid !== tid) rec.volReadout.textContent = pct + '%';

        var cids = p.trackClipIds[tid] || [], lane = rec.lane, clipAnchor = null;
        for (c = 0; c < cids.length; c++) {
          var cid = cids[c], cl = p.clipsById[cid];
          if (!cl) continue;
          seenC[cid] = true;
          var cRec = clipElMap.get(cid);
          if (!cRec) { cRec = createClipDom(cl); clipElMap.set(cid, cRec); }
          var nextClip = clipAnchor ? clipAnchor.nextSibling : lane.firstChild;
          if (cRec.el !== nextClip) lane.insertBefore(cRec.el, nextClip);
          clipAnchor = cRec.el;

          setAttrs(cRec.el, {
            'aria-selected': String(sel.clipId === cid), 'data-track-id': cl.trackId,
            'data-start-beat': String(cl.startBeat), 'data-duration-beats': String(cl.durationBeats),
            'aria-label': clipAriaLabel(cl, tr, p.beatsPerBar)
          });
          cRec.el.style.setProperty('--start-beat', String(cl.startBeat));
          cRec.el.style.setProperty('--duration-beats', String(cl.durationBeats));
          if (cRec.titleEl && cRec.titleEl.textContent !== cl.name) cRec.titleEl.textContent = cl.name;
          var lenTxt = formatClipLength(cl.durationBeats, p.beatsPerBar);
          if (cRec.lenEl && cRec.lenEl.textContent !== lenTxt) cRec.lenEl.textContent = lenTxt;
          renderPatternBars(cRec.patternEl, cl, tr);
        }
      }
      clipElMap.forEach(function (r, id) { if (!seenC[id]) { if (r.el.parentNode) r.el.parentNode.removeChild(r.el); clipElMap.delete(id); } });
      trackRowMap.forEach(function (r, id) { if (!seenT[id]) { if (r.row.parentNode) r.row.parentNode.removeChild(r.row); trackRowMap.delete(id); } });
      if (playheadBeat > totB) updatePlayhead(totB, { autoScroll: false });
    }

    // --- Playhead & Ruler Seek / Scrub ---

    function updatePlayhead(beat, uOpts) {
      if (disposed) return;
      var tot = lastState ? model.getTotalBeats(lastState.project) : 32;
      var bpb = lastState ? lastState.project.beatsPerBar : 4;
      var cl = clamp(Number.isFinite(beat) ? beat : 0, 0, tot);
      playheadBeat = cl;
      if (gridEl) gridEl.style.setProperty('--playhead-beat', String(r4(cl)));
      var qStep = Math.round(cl * 4);
      if (rulerEl && qStep !== lastAriaStep) {
        lastAriaStep = qStep;
        setAttrs(rulerEl, { 'aria-valuenow': String(r2(cl)), 'aria-valuetext': formatBeatPosition(cl, bpb) });
      }
      if (uOpts && uOpts.autoScroll && !activeDrag && !activeScrub) ensureBeatVisible(cl, { followPlayhead: Boolean(uOpts.followPlayhead) });
    }

    function seekRuler(e, phase) {
      if (!lastState) return;
      var tot = model.getTotalBeats(lastState.project), raw = beatFromX(e.clientX, rulerEl);
      var next = e.shiftKey ? r4(clamp(raw, 0, tot)) : clamp(model.quantizeBeat(raw), 0, tot);
      updatePlayhead(next, { autoScroll: false });
      if (onSeek) onSeek(next, { source: 'ruler', phase: phase });
    }

    function finishScrub(cancel, silent) {
      if (!activeScrub) return;
      var sc = activeScrub; activeScrub = null;
      releaseCap(rulerEl, sc.pointerId);
      if (silent) return;
      if (cancel) {
        updatePlayhead(sc.origBeat, { autoScroll: false });
        if (onSeek) onSeek(sc.origBeat, { source: 'ruler', phase: 'cancel' });
        announce('Seek cancelled', 'neutral');
      } else if (onSeek) {
        onSeek(playheadBeat, { source: 'ruler', phase: 'end' });
      }
    }

    var rulerHandlers = {
      pointerdown: function (e) {
        if ((e.button !== undefined && e.button !== 0) || !lastState) return;
        e.preventDefault();
        if (rulerEl && typeof rulerEl.focus === 'function') rulerEl.focus({ preventScroll: true });
        activeScrub = { pointerId: e.pointerId, origBeat: playheadBeat };
        if (rulerEl && typeof rulerEl.setPointerCapture === 'function') { try { rulerEl.setPointerCapture(e.pointerId); } catch (_) {} }
        seekRuler(e, 'start');
      },
      pointermove: function (e) { if (activeScrub && e.pointerId === activeScrub.pointerId) seekRuler(e, 'move'); },
      pointerup: function (e) { if (activeScrub && e.pointerId === activeScrub.pointerId) finishScrub(false); },
      pointercancel: function (e) { if (activeScrub && e.pointerId === activeScrub.pointerId) finishScrub(true); },
      keydown: function (e) {
        if (!lastState) return;
        var tot = model.getTotalBeats(lastState.project), bpb = lastState.project.beatsPerBar || 4;
        var step = e.shiftKey ? bpb : (e.altKey ? 0.25 : 1), next = null;
        if (e.key === 'ArrowLeft') next = clamp(model.quantizeBeat(playheadBeat - step), 0, tot);
        else if (e.key === 'ArrowRight') next = clamp(model.quantizeBeat(playheadBeat + step), 0, tot);
        else if (e.key === 'Home') next = 0;
        else if (e.key === 'End') next = tot;
        if (next === null) return;
        e.preventDefault();
        updatePlayhead(next, { autoScroll: true });
        if (onSeek) onSeek(next, { source: 'keyboard', phase: 'end' });
        announce('Playhead at ' + formatBeatPosition(next, bpb), 'neutral');
      }
    };

    // --- Clip Pointer Drag-Move & Resize (Single Undo Commit on Release) ---

    function clearDrop(d) {
      if (d && d.dropLaneEl) { d.dropLaneEl.classList.remove('is-drop-target'); d.dropLaneEl = null; }
    }

    function applyDragVisuals(d) {
      d.clipEl.style.setProperty('--start-beat', String(d.previewStartBeat));
      d.clipEl.style.setProperty('--duration-beats', String(d.previewDurationBeats));
      var cRec = clipElMap.get(d.clipId);
      if (cRec && cRec.lenEl && lastState) cRec.lenEl.textContent = formatClipLength(d.previewDurationBeats, lastState.project.beatsPerBar);
      if (d.mode === 'move') {
        clearDrop(d);
        if (d.previewTrackId !== d.origTrackId) {
          var oR = trackRowMap.get(d.origTrackId), dR = trackRowMap.get(d.previewTrackId);
          if (oR && dR) {
            d.clipEl.style.transform = 'translateY(' + Math.round(dR.row.getBoundingClientRect().top - oR.row.getBoundingClientRect().top) + 'px)';
            if (dR.lane) { dR.lane.classList.add('is-drop-target'); d.dropLaneEl = dR.lane; }
          }
        } else {
          d.clipEl.style.transform = '';
        }
      }
    }

    function endDrag(commit, silent) {
      if (!activeDrag) return;
      var d = activeDrag; activeDrag = null;
      clearDrop(d);
      releaseCap(d.captureEl, d.pointerId);
      d.clipEl.classList.remove('is-dragging', 'is-resizing');
      d.clipEl.style.transform = '';

      if (!commit || !d.moved || silent) {
        d.clipEl.style.setProperty('--start-beat', String(d.origStartBeat));
        d.clipEl.style.setProperty('--duration-beats', String(d.origDurationBeats));
        var cRec = clipElMap.get(d.clipId);
        if (cRec && cRec.lenEl && lastState) cRec.lenEl.textContent = formatClipLength(d.origDurationBeats, lastState.project.beatsPerBar);
        if (!silent && !commit && d.moved) announce(d.mode === 'move' ? 'Move cancelled' : 'Resize cancelled', 'neutral');
        if (!silent && deferredState) { var pend = deferredState; deferredState = null; render(pend); }
        return;
      }

      deferredState = null;
      if (!store) return;
      var chS = Math.abs(d.previewStartBeat - d.origStartBeat) > EPS;
      var chD = Math.abs(d.previewDurationBeats - d.origDurationBeats) > EPS;
      var chT = d.previewTrackId !== d.origTrackId, res = null;
      if (d.mode === 'move' && (chS || chT)) {
        res = store.moveClip(d.clipId, { trackId: d.previewTrackId, startBeat: d.previewStartBeat });
        if (res && res.ok && res.changed) {
          var st = store.getState(), tr = st.project.tracksById[res.clip.trackId];
          announce('Moved ' + res.clip.name + ' to ' + formatBeatPosition(res.clip.startBeat, st.project.beatsPerBar) + (tr ? ' on ' + tr.name : ''), 'ok');
        } else if (res && !res.ok) announce('Could not move clip (' + res.reason + ')', 'error');
      } else if (d.mode === 'resize-left' && chS) {
        res = store.resizeClip(d.clipId, { startBeat: d.previewStartBeat });
        if (res && res.ok && res.changed) announce('Trimmed ' + res.clip.name + ' to ' + formatClipLength(res.clip.durationBeats, store.getState().project.beatsPerBar), 'ok');
      } else if (d.mode === 'resize-right' && chD) {
        res = store.resizeClip(d.clipId, { durationBeats: d.previewDurationBeats });
        if (res && res.ok && res.changed) announce('Resized ' + res.clip.name + ' to ' + formatClipLength(res.clip.durationBeats, store.getState().project.beatsPerBar), 'ok');
      }
      render(store.getState());
    }

    // --- Track & Clip Interactions + Keyboard Alternatives ---

    function focusClip(cid) {
      var rec = clipElMap.get(cid);
      if (rec && rec.el && typeof rec.el.focus === 'function') {
        rec.el.focus({ preventScroll: true });
        if (lastState && lastState.project.clipsById[cid]) ensureBeatVisible(lastState.project.clipsById[cid].startBeat);
      }
    }
    function focusTrack(tid) {
      var rec = trackRowMap.get(tid);
      if (rec && rec.selectBtn && typeof rec.selectBtn.focus === 'function') rec.selectBtn.focus({ preventScroll: true });
    }

    var trackHandlers = {
      pointerdown: function (e) {
        if ((e.button !== undefined && e.button !== 0) || !store || !lastState) return;
        var clipEl = e.target.closest ? e.target.closest('.clip-block[data-clip-id]') : null;
        if (!clipEl) return;
        var cid = clipEl.getAttribute('data-clip-id');
        if (!lastState.project.clipsById[cid]) return;
        var hEl = e.target.closest ? e.target.closest('.clip-resize-handle') : null;
        var mode = hEl ? (hEl.getAttribute('data-resize') === 'left' ? 'resize-left' : 'resize-right') : 'move';
        store.selectClip(cid);
        var cRec = clipElMap.get(cid);
        if (cRec) clipEl = cRec.el;
        if (typeof clipEl.focus === 'function') clipEl.focus({ preventScroll: true });
        var cl = store.getState().project.clipsById[cid];
        if (!cl) return;
        var capEl = hEl || clipEl;
        activeDrag = {
          mode: mode, pointerId: e.pointerId, captureEl: capEl, clipEl: clipEl, clipId: cid,
          origTrackId: cl.trackId, origStartBeat: cl.startBeat, origDurationBeats: cl.durationBeats,
          previewTrackId: cl.trackId, previewStartBeat: cl.startBeat, previewDurationBeats: cl.durationBeats,
          startClientX: e.clientX, startClientY: e.clientY, startScrollLeft: scrollEl ? scrollEl.scrollLeft : 0,
          beatWidth: getBeatWidthPx(), moved: false, dropLaneEl: null
        };
        if (typeof capEl.setPointerCapture === 'function') { try { capEl.setPointerCapture(e.pointerId); } catch (_) {} }
      },
      pointermove: function (e) {
        if (!activeDrag || e.pointerId !== activeDrag.pointerId || !lastState) return;
        var dx = (e.clientX - activeDrag.startClientX) + ((scrollEl ? scrollEl.scrollLeft : 0) - activeDrag.startScrollLeft);
        var dy = e.clientY - activeDrag.startClientY;
        if (!activeDrag.moved) {
          if (Math.hypot(dx, dy) < (activeDrag.mode === 'move' ? 3 : 2)) return;
          activeDrag.moved = true;
          activeDrag.clipEl.classList.add(activeDrag.mode === 'move' ? 'is-dragging' : 'is-resizing');
        }
        var hoverTid = activeDrag.mode === 'move' ? trackAtY(e.clientY) : activeDrag.origTrackId;
        var prev = computeDragPreview(lastState.project, activeDrag, dx / (activeDrag.beatWidth || 34), hoverTid, model);
        activeDrag.previewTrackId = prev.trackId;
        activeDrag.previewStartBeat = prev.startBeat;
        activeDrag.previewDurationBeats = prev.durationBeats;
        applyDragVisuals(activeDrag);
      },
      pointerup: function (e) {
        if (activeVolTid) activeVolTid = null;
        if (activeDrag && e.pointerId === activeDrag.pointerId) endDrag(true);
      },
      pointercancel: function (e) {
        if (activeVolTid) activeVolTid = null;
        if (activeDrag && e.pointerId === activeDrag.pointerId) endDrag(false);
      },
      click: function (e) {
        if (!store || !lastState) return;
        var actEl = e.target.closest ? e.target.closest('[data-action]') : null;
        if (actEl) {
          var act = actEl.getAttribute('data-action'), tid = actEl.getAttribute('data-track-id');
          if (act === 'toggle-mute' && tid) {
            var m = store.toggleTrackMute(tid);
            if (m.ok) announce((m.track.mute ? 'Muted ' : 'Unmuted ') + m.track.name, 'neutral');
            return;
          }
          if (act === 'toggle-solo' && tid) {
            var s = store.toggleTrackSolo(tid);
            if (s.ok) announce((s.track.solo ? 'Soloed ' : 'Unsoloed ') + s.track.name, 'neutral');
            return;
          }
          if (act === 'select-track' && tid) {
            if (e.target.closest && e.target.closest('.track-vol-slider')) return;
            store.selectTrack(tid);
            return;
          }
        }
        if (e.target.classList && e.target.classList.contains('track-lane')) {
          var laneTid = e.target.getAttribute('data-track-id');
          if (laneTid) { store.selectTrack(laneTid); store.selectClip(null); }
        }
      },
      dblclick: function (e) {
        if (!store || !lastState) return;
        var clipEl = e.target.closest ? e.target.closest('.clip-block[data-clip-id]') : null;
        if (clipEl) {
          var cl = lastState.project.clipsById[clipEl.getAttribute('data-clip-id')];
          if (cl && onClipAudition) onClipAudition(cl, lastState.project.tracksById[cl.trackId]);
          return;
        }
        if (!e.target.classList || !e.target.classList.contains('track-lane')) return;
        var tid = e.target.getAttribute('data-track-id');
        if (!tid || !lastState.project.tracksById[tid]) return;
        var raw = beatFromX(e.clientX, e.target), maxS = Math.max(0, model.getTotalBeats(lastState.project) - model.LIMITS.MIN_CLIP_BEATS);
        var whole = clamp(Math.floor(Math.max(0, raw)), 0, maxS), fine = clamp(model.quantizeBeat(Math.max(0, raw)), 0, maxS);
        var res = store.createClip({ trackId: tid, startBeat: whole, durationBeats: model.LIMITS.DEFAULT_CLIP_BEATS });
        if (!res.ok && Math.abs(fine - whole) > EPS) res = store.createClip({ trackId: tid, startBeat: fine, durationBeats: model.LIMITS.DEFAULT_CLIP_BEATS });
        if (res.ok) {
          var st = store.getState(), tr = st.project.tracksById[tid];
          announce('Inserted ' + res.clip.name + ' on ' + (tr ? tr.name : tid) + ' at ' + formatBeatPosition(res.clip.startBeat, st.project.beatsPerBar), 'ok');
          focusClip(res.clip.id);
        } else {
          announce('Cannot insert clip here (' + res.reason + ')', 'error');
        }
      },
      input: function (e) {
        var t = e.target;
        if (!t || !t.classList || !t.classList.contains('track-vol-slider')) return;
        var tid = t.getAttribute('data-track-id');
        if (!tid) return;
        activeVolTid = tid;
        var pct = clamp(Math.round(Number(t.value) || 0), 0, 100), rec = trackRowMap.get(tid);
        if (rec && rec.volReadout) rec.volReadout.textContent = pct + '%';
        if (onTrackVolumePreview) onTrackVolumePreview(tid, pct / 100);
      },
      change: function (e) {
        var t = e.target;
        if (!t || !t.classList || !t.classList.contains('track-vol-slider')) return;
        var tid = t.getAttribute('data-track-id');
        activeVolTid = null;
        if (!tid || !store) return;
        var res = store.setTrackVolume(tid, clamp((Number(t.value) || 0) / 100, 0, 1));
        if (res.ok && res.changed) announce(res.track.name + ' volume ' + Math.round(res.track.volume * 100) + '%', 'neutral');
      },
      keydown: function (e) {
        if (!store || !lastState) return;
        var clipEl = e.target.closest ? e.target.closest('.clip-block[data-clip-id]') : null;
        if (clipEl) {
          var cid = clipEl.getAttribute('data-clip-id'), p = lastState.project, cl = p.clipsById[cid];
          if (!cl) return;
          if (e.key === 'Escape') { e.preventDefault(); if (activeDrag) endDrag(false); else store.selectClip(null); return; }
          if (e.key === 'Enter') { e.preventDefault(); store.selectClip(cid); return; }
          if (e.key === 'Delete' || e.key === 'Backspace') {
            e.preventDefault();
            var ids = p.trackClipIds[cl.trackId] || [], idx = ids.indexOf(cid), nextCid = ids[idx + 1] || ids[idx - 1] || null;
            if (store.deleteClip(cid).ok) {
              announce('Deleted ' + cl.name, 'neutral');
              if (nextCid) { store.selectClip(nextCid); focusClip(nextCid); } else focusTrack(cl.trackId);
            }
            return;
          }
          if ((e.key === 'd' || e.key === 'D') && !e.ctrlKey && !e.metaKey) {
            e.preventDefault();
            var dup = store.duplicateClip(cid);
            if (dup.ok) { announce('Duplicated ' + cl.name, 'ok'); focusClip(dup.clip.id); }
            else announce('Cannot duplicate clip (' + dup.reason + ')', 'error');
            return;
          }
          var step = e.altKey ? 0.25 : 1;
          if (e.key === '[' || e.key === ']') {
            e.preventDefault();
            var lR = store.resizeClip(cid, { startBeat: cl.startBeat + (e.key === ']' ? step : -step) });
            if (lR.ok && lR.changed) announce('Trimmed ' + lR.clip.name + ' to ' + formatClipLength(lR.clip.durationBeats, p.beatsPerBar), 'ok');
            return;
          }
          if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
            e.preventDefault();
            var dir = e.key === 'ArrowRight' ? 1 : -1;
            if (e.shiftKey) {
              var rR = store.resizeClip(cid, { durationBeats: cl.durationBeats + dir * step });
              if (rR.ok && rR.changed) announce('Resized ' + rR.clip.name + ' to ' + formatClipLength(rR.clip.durationBeats, p.beatsPerBar), 'ok');
            } else {
              var mR = store.moveClip(cid, { startBeat: cl.startBeat + dir * step });
              if (mR.ok && mR.changed) { ensureBeatVisible(mR.clip.startBeat); announce('Moved ' + mR.clip.name + ' to ' + formatBeatPosition(mR.clip.startBeat, p.beatsPerBar), 'ok'); }
            }
            return;
          }
          if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
            e.preventDefault();
            var ord = p.trackOrder, dest = ord[ord.indexOf(cl.trackId) + (e.key === 'ArrowDown' ? 1 : -1)];
            if (dest) {
              var cr = store.moveClip(cid, { trackId: dest, startBeat: cl.startBeat });
              if (cr.ok && cr.changed) {
                var tr = store.getState().project.tracksById[dest];
                announce('Moved ' + cr.clip.name + ' to ' + (tr ? tr.name : dest), 'ok');
                focusClip(cid);
              } else if (!cr.ok) announce('No space on target track', 'error');
            }
          }
          return;
        }
        var trBtn = e.target.closest ? e.target.closest('.track-select-btn[data-track-id]') : null;
        if (trBtn) {
          var tid = trBtn.getAttribute('data-track-id'), tOrd = lastState.project.trackOrder, cur = tOrd.indexOf(tid);
          if (e.key === 'ArrowUp' && cur > 0) { e.preventDefault(); store.selectTrack(tOrd[cur - 1]); focusTrack(tOrd[cur - 1]); }
          else if (e.key === 'ArrowDown' && cur !== -1 && cur < tOrd.length - 1) { e.preventDefault(); store.selectTrack(tOrd[cur + 1]); focusTrack(tOrd[cur + 1]); }
          else if ((e.key === 'm' || e.key === 'M') && !e.ctrlKey && !e.metaKey) { e.preventDefault(); store.toggleTrackMute(tid); }
          else if ((e.key === 's' || e.key === 'S') && !e.ctrlKey && !e.metaKey) { e.preventDefault(); store.toggleTrackSolo(tid); }
        }
      }
    };

    var winHandlers = {
      keydown: function (e) {
        if (e.key !== 'Escape') return;
        if (activeDrag) { e.preventDefault(); endDrag(false); }
        else if (activeScrub) { e.preventDefault(); finishScrub(true); }
      },
      blur: function () { activeVolTid = null; if (activeDrag) endDrag(false); if (activeScrub) finishScrub(true); },
      resize: function () { applyZoom(zoomIndex, false); }
    };
    function onZoomIn() { applyZoom(zoomIndex + 1, true); }
    function onZoomOut() { applyZoom(zoomIndex - 1, true); }

    hydrateDom();
    applyZoom(zoomIndex, false);
    bindAll(rulerEl, rulerHandlers, true);
    bindAll(tracksEl, trackHandlers, true);
    bindAll(win, winHandlers, true);
    if (zoomInBtn) zoomInBtn.addEventListener('click', onZoomIn);
    if (zoomOutBtn) zoomOutBtn.addEventListener('click', onZoomOut);

    var unsub = (store && opts.autoSubscribe !== false && typeof store.subscribe === 'function')
      ? store.subscribe(function (nextState) { render(nextState); })
      : null;
    if (store) render(store.getState());

    function dispose() {
      if (disposed) return;
      disposed = true;
      if (activeDrag) endDrag(false, true);
      if (activeScrub) finishScrub(false, true);
      if (unsub) { unsub(); unsub = null; }
      bindAll(rulerEl, rulerHandlers, false);
      bindAll(tracksEl, trackHandlers, false);
      bindAll(win, winHandlers, false);
      if (zoomInBtn) zoomInBtn.removeEventListener('click', onZoomIn);
      if (zoomOutBtn) zoomOutBtn.removeEventListener('click', onZoomOut);
      trackRowMap.clear(); clipElMap.clear();
    }

    return {
      render: render, updatePlayhead: updatePlayhead,
      setZoom: function (f) {
        var best = 0, dist = Infinity;
        for (var i = 0; i < ZOOM_LEVELS.length; i++) { var d = Math.abs(ZOOM_LEVELS[i] - f); if (d < dist) { dist = d; best = i; } }
        return applyZoom(best, true);
      },
      zoomIn: onZoomIn, zoomOut: onZoomOut,
      getZoom: function () { return ZOOM_LEVELS[zoomIndex]; },
      getBeatWidth: getBeatWidthPx,
      ensureBeatVisible: ensureBeatVisible,
      focusClip: focusClip, focusTrack: focusTrack,
      isInteracting: function () { return Boolean(activeDrag || activeScrub); },
      dispose: dispose
    };
  }

  return Object.freeze({
    ZOOM_LEVELS: ZOOM_LEVELS,
    formatBeatPosition: formatBeatPosition,
    formatClipLength: formatClipLength,
    buildClipPatternVisuals: buildClipPatternVisuals,
    computeDragPreview: computeDragPreview,
    createTimeline: createTimeline
  });
});
