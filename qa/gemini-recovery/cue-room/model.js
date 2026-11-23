/**
 * Cue Room — Normalized Project Model, Validator, and Undoable Store
 * Dual-environment UMD module: browser global `CueModel` + Node CommonJS.
 *
 * STABLE PUBLIC PROJECT FORMAT (schemaVersion: 1):
 * {
 *   schemaVersion: 1,            // integer === 1
 *   name: "Midnight Relay",      // trimmed string, 1..80 chars
 *   tempo: 124,                  // finite BPM in [40, 240]
 *   beatsPerBar: 4,              // integer === 4
 *   lengthBars: 8,               // integer in [4, 32] (starter: 8 bars = 32 beats)
 *   tracks: [{                   // 1..12 tracks, unique trimmed ids (1..64 chars)
 *     id, name, type,            // type: 'kick'|'snare'|'hat'|'bass'|'synth'|'pad'
 *     color, volume, pan,        // #RRGGBB, volume [0,1], pan [-1,1]
 *     mute, solo                 // booleans
 *   }],
 *   clips: [{                    // 0..256 total, <= 64/track, unique ids, non-overlapping
 *     id, trackId, name,         // trackId references an existing track.id
 *     startBeat, durationBeats,  // finite 0.25-aligned, dur >= 0.5, end <= totalBeats
 *     patternId, patternSteps,   // patternSteps: integer [4, 32] (16th-note loop)
 *     gain,                      // finite [0, 1]
 *     notes: [{ step, pitch, velocity, durationSteps }]
 *       // 0..128 notes sorted by (step, pitch, durationSteps)
 *       // step: int [0, patternSteps-1], pitch: int MIDI [24, 108],
 *       // velocity: finite (0, 1], durationSteps: finite [0.25, patternSteps]
 *   }]
 * }
 *
 * INTERNAL NORMALIZED STATE:
 *   { schemaVersion, name, tempo, beatsPerBar, lengthBars,
 *     trackOrder, tracksById, clipsById, trackClipIds } (trackClipIds sorted by startBeat)
 *
 * COLLISION, FITTING & HISTORY CONTRACT:
 * - Single-lane non-overlapping tracks within [0, lengthBars * 4].
 * - createClip: fits open gap at startBeat (shrinking duration to fit), auto-places in
 *   earliest open gap if startBeat omitted, or rejects collisions.
 * - moveClip: clamps to timeline bounds; on overlap magnetically clamps flush to closest
 *   legal neighbor edge (fitMode:'clamp'), or rejects if no fit or fitMode:'strict'.
 * - resizeClip: right-edge clamps to [0.5, nextStart - start]; left-edge preserves endBeat
 *   and clamps startBeat to [prevEnd, end - 0.5].
 * - duplicateClip: places equal-duration copy in nearest open gap on same track.
 * - Audibility: if any track is soloed, only soloed tracks play; else unmuted tracks play.
 * - History: bounded past/future stacks (default 50). Selection is non-undoable; new
 *   mutating actions truncate redo. Failed imports leave state and history untouched.
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.CueModel = api;
})(typeof globalThis !== 'undefined' ? globalThis : typeof window !== 'undefined' ? window : this, function () {
  'use strict';

  var SCHEMA_VERSION = 1, BEATS_PER_BAR = 4, STEPS_PER_BEAT = 4, EPS = 1e-6;
  var HEX_RE = /^#[0-9a-fA-F]{6}$/;

  var LIMITS = Object.freeze({
    SCHEMA_VERSION: SCHEMA_VERSION, BEATS_PER_BAR: BEATS_PER_BAR, STEPS_PER_BEAT: STEPS_PER_BEAT,
    MIN_TEMPO: 40, MAX_TEMPO: 240, DEFAULT_TEMPO: 124,
    MIN_BARS: 4, MAX_BARS: 32, DEFAULT_BARS: 8, MIN_TRACKS: 1, MAX_TRACKS: 12,
    MAX_CLIPS: 256, MAX_CLIPS_PER_TRACK: 64, MIN_CLIP_BEATS: 0.5, DEFAULT_CLIP_BEATS: 4,
    GRID_STEP_BEATS: 0.25, MIN_PATTERN_STEPS: 4, MAX_PATTERN_STEPS: 32, DEFAULT_PATTERN_STEPS: 16,
    MAX_NOTES_PER_CLIP: 128, MIN_PITCH: 24, MAX_PITCH: 108,
    MAX_NAME_LENGTH: 80, MAX_ID_LENGTH: 64, MAX_HISTORY: 50, MAX_JSON_CHARS: 512 * 1024
  });

  var TRACK_TYPES = Object.freeze(['kick', 'snare', 'hat', 'bass', 'synth', 'pad']);
  var TRACK_TYPE_META = Object.freeze({
    kick: Object.freeze({ label: 'Kick', color: '#e85d4a', pitched: false, defaultPitch: 36 }),
    snare: Object.freeze({ label: 'Snare & Clap', color: '#e69f38', pitched: false, defaultPitch: 38 }),
    hat: Object.freeze({ label: 'Hi-Hats', color: '#38b8a8', pitched: false, defaultPitch: 42 }),
    bass: Object.freeze({ label: 'Analog Bass', color: '#7c6ef0', pitched: true, defaultPitch: 33 }),
    synth: Object.freeze({ label: 'Poly Synth', color: '#3b9eff', pitched: true, defaultPitch: 60 }),
    pad: Object.freeze({ label: 'Atmosphere Pad', color: '#d966a8', pitched: true, defaultPitch: 57 })
  });

  var PATTERN_DEFS = [
    ['kick-four', 'Four on Floor', 'kick', [[0,36,1,1],[4,36,0.94,1],[8,36,0.98,1],[12,36,0.94,1]]],
    ['kick-drive', 'Drop Drive', 'kick', [[0,36,1,1],[4,36,0.95,1],[8,36,0.98,1],[12,36,0.95,1],[14,36,0.62,0.5]]],
    ['kick-turnaround', 'Sync Turnaround', 'kick', [[0,36,1,1],[4,36,0.92,1],[7,36,0.58,0.5],[10,36,0.88,1],[12,36,0.95,1],[15,36,0.72,0.5]]],
    ['snare-backbeat', 'Backbeat Clap', 'snare', [[4,38,0.9,1],[4,39,0.75,1],[12,38,0.94,1],[12,39,0.78,1],[15,38,0.4,0.5]]],
    ['snare-build', 'Tension Build', 'snare', [[4,39,0.72,1],[8,38,0.62,0.5],[10,38,0.7,0.5],[12,38,0.82,0.5],[12,39,0.74,1],[14,38,0.86,0.5],[15,38,0.95,0.5]]],
    ['snare-ghost', 'Ghost Groove', 'snare', [[4,38,0.92,1],[7,38,0.34,0.5],[9,38,0.42,0.5],[12,38,0.96,1],[12,39,0.68,1]]],
    ['hat-offbeat', 'Offbeat Pulse', 'hat', [[0,42,0.45,0.5],[2,46,0.85,1],[4,42,0.42,0.5],[6,46,0.82,1],[8,42,0.45,0.5],[10,46,0.86,1],[12,42,0.42,0.5],[14,46,0.84,1]]],
    ['hat-shimmer', '16th Shimmer', 'hat', [[0,42,0.52,0.5],[1,42,0.28,0.5],[2,46,0.78,1],[3,42,0.32,0.5],[4,42,0.55,0.5],[5,42,0.3,0.5],[6,42,0.74,0.5],[7,42,0.36,0.5],[8,42,0.52,0.5],[9,42,0.28,0.5],[10,46,0.82,1],[11,42,0.34,0.5],[12,42,0.56,0.5],[13,42,0.34,0.5],[14,46,0.86,1],[15,42,0.46,0.5]]],
    ['hat-drive', 'Open Drive', 'hat', [[0,42,0.62,0.5],[2,46,0.9,1.5],[4,42,0.6,0.5],[6,46,0.88,1.5],[7,42,0.4,0.5],[8,42,0.64,0.5],[10,46,0.92,1.5],[12,42,0.58,0.5],[14,46,0.9,1.5],[15,42,0.48,0.5]]],
    ['bass-groove', 'A-Minor Root', 'bass', [[0,33,0.94,2],[3,33,0.72,1],[6,36,0.86,2],[8,33,0.82,1.5],[10,40,0.88,2],[14,31,0.84,2]]],
    ['bass-pedal', 'Lift Pedal', 'bass', [[0,29,0.9,2],[2,29,0.72,1.5],[4,29,0.88,2],[6,36,0.76,1.5],[8,31,0.92,2],[10,31,0.75,1.5],[12,31,0.9,2],[14,38,0.82,2]]],
    ['bass-octave', 'Octave Roll', 'bass', [[0,33,0.96,1.5],[2,45,0.82,1],[3,33,0.74,1],[6,36,0.88,1.5],[8,33,0.92,1.5],[10,45,0.84,1],[12,38,0.88,1.5],[14,40,0.9,1.5]]],
    ['synth-chords', 'Glass Chords', 'synth', [[0,57,0.74,3],[0,60,0.7,3],[0,64,0.72,3],[6,57,0.66,2],[6,60,0.64,2],[6,67,0.72,2],[10,53,0.72,3],[10,57,0.68,3],[10,64,0.74,3]]],
    ['synth-arp', 'Rising Arp', 'synth', [[0,57,0.72,1],[2,60,0.74,1],[4,64,0.78,1],[6,67,0.82,1],[8,69,0.86,1],[10,67,0.78,1],[12,72,0.88,1.5],[14,76,0.92,1.5]]],
    ['synth-hook', 'Night Hook', 'synth', [[0,69,0.86,2],[3,72,0.8,1.5],[6,76,0.9,2],[8,74,0.78,1.5],[10,72,0.84,2],[12,67,0.8,1.5],[14,64,0.86,2]]],
    ['pad-wash', 'Warm Wash', 'pad', [[0,57,0.62,8],[0,60,0.58,8],[0,64,0.6,8],[8,53,0.62,8],[8,57,0.58,8],[8,64,0.6,8]]]
  ];

  var PATTERN_LIBRARY = (function () {
    var map = {};
    for (var i = 0; i < PATTERN_DEFS.length; i++) {
      var d = PATTERN_DEFS[i];
      map[d[0]] = Object.freeze({
        id: d[0], name: d[1], trackType: d[2], patternSteps: 16,
        notes: Object.freeze(d[3].map(function (t) { return Object.freeze({ step: t[0], pitch: t[1], velocity: t[2], durationSteps: t[3] }); }))
      });
    }
    return Object.freeze(map);
  })();

  function isObj(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }
  function isNum(v) { return typeof v === 'number' && Number.isFinite(v); }
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
  function r4(v) { return Math.round(v * 10000) / 10000; }
  function quantizeBeat(v) { return r4(Math.round(v / LIMITS.GRID_STEP_BEATS) * LIMITS.GRID_STEP_BEATS); }
  function isAligned(v) { return Math.abs(v / LIMITS.GRID_STEP_BEATS - Math.round(v / LIMITS.GRID_STEP_BEATS)) < EPS; }
  function getTotalBeats(p) { return p.lengthBars * p.beatsPerBar; }
  function fail(reason) { return { ok: false, changed: false, reason: reason }; }
  function cmpId(a, b) { return a < b ? -1 : a > b ? 1 : 0; }

  function sortNotes(notes) {
    return notes.slice().sort(function (a, b) { return (a.step - b.step) || (a.pitch - b.pitch) || (a.durationSteps - b.durationSteps); });
  }
  function cloneNote(n) { return { step: n.step, pitch: n.pitch, velocity: n.velocity, durationSteps: n.durationSteps }; }
  function cloneClip(c) {
    return {
      id: c.id, trackId: c.trackId, name: c.name, startBeat: c.startBeat, durationBeats: c.durationBeats,
      patternId: c.patternId, patternSteps: c.patternSteps, gain: c.gain, notes: c.notes.map(cloneNote)
    };
  }
  function cloneTrack(t) {
    return { id: t.id, name: t.name, type: t.type, color: t.color, volume: t.volume, pan: t.pan, mute: t.mute, solo: t.solo };
  }
  function cloneProject(p) {
    var tracksById = {}, clipsById = {}, trackClipIds = {}, i;
    for (i = 0; i < p.trackOrder.length; i++) {
      var tid = p.trackOrder[i];
      tracksById[tid] = cloneTrack(p.tracksById[tid]);
      trackClipIds[tid] = p.trackClipIds[tid].slice();
    }
    var cids = Object.keys(p.clipsById);
    for (i = 0; i < cids.length; i++) clipsById[cids[i]] = cloneClip(p.clipsById[cids[i]]);
    return {
      schemaVersion: p.schemaVersion, name: p.name, tempo: p.tempo, beatsPerBar: p.beatsPerBar,
      lengthBars: p.lengthBars, trackOrder: p.trackOrder.slice(),
      tracksById: tracksById, clipsById: clipsById, trackClipIds: trackClipIds
    };
  }

  function getPatternById(id) {
    var p = PATTERN_LIBRARY[id];
    return p ? { id: p.id, name: p.name, trackType: p.trackType, patternSteps: p.patternSteps, notes: p.notes.map(cloneNote) } : null;
  }
  function getPatternsForTrackType(type) {
    var out = [], keys = Object.keys(PATTERN_LIBRARY);
    for (var i = 0; i < keys.length; i++) if (PATTERN_LIBRARY[keys[i]].trackType === type) out.push(getPatternById(keys[i]));
    return out;
  }
  function defaultPatternFor(type) {
    return getPatternsForTrackType(type)[0] || { id: 'custom', name: 'New Pattern', trackType: type, patternSteps: 16, notes: [] };
  }
  function sortTrackClips(p, tid) {
    p.trackClipIds[tid].sort(function (a, b) { return (p.clipsById[a].startBeat - p.clipsById[b].startBeat) || cmpId(a, b); });
  }
  function nextClipId(p, tid) {
    var base = 'clip-' + tid.replace(/^trk-/, '') + '-', n = 1;
    while (p.clipsById[base + n]) n++;
    return base + n;
  }
  function validNote(nt, steps) {
    return isObj(nt) && Number.isInteger(nt.step) && nt.step >= 0 && nt.step < steps &&
      Number.isInteger(nt.pitch) && nt.pitch >= LIMITS.MIN_PITCH && nt.pitch <= LIMITS.MAX_PITCH &&
      isNum(nt.velocity) && nt.velocity > 0 && nt.velocity <= 1 &&
      isNum(nt.durationSteps) && nt.durationSteps >= 0.25 && nt.durationSteps <= steps;
  }

  function isTrackAudible(project, trackId) {
    var tr = project.tracksById[trackId];
    if (!tr) return false;
    for (var i = 0; i < project.trackOrder.length; i++) {
      if (project.tracksById[project.trackOrder[i]].solo) return tr.solo;
    }
    return !tr.mute;
  }

  function expandProjectEvents(project, options) {
    var opts = options || {}, total = getTotalBeats(project);
    var fromBeat = isNum(opts.fromBeat) ? Math.max(0, opts.fromBeat) : 0;
    var toBeat = isNum(opts.toBeat) ? Math.min(total, opts.toBeat) : total;
    var includeMuted = Boolean(opts.includeMuted), events = [], stepBeat = 1 / STEPS_PER_BEAT;
    if (toBeat <= fromBeat) return events;

    for (var t = 0; t < project.trackOrder.length; t++) {
      var tid = project.trackOrder[t], tr = project.tracksById[tid];
      if (!includeMuted && !isTrackAudible(project, tid)) continue;
      var cids = project.trackClipIds[tid];
      for (var c = 0; c < cids.length; c++) {
        var cl = project.clipsById[cids[c]], clEnd = cl.startBeat + cl.durationBeats;
        if (clEnd <= fromBeat || cl.startBeat >= toBeat) continue;
        var totalSteps = Math.round(cl.durationBeats * STEPS_PER_BEAT), cycles = Math.ceil(totalSteps / cl.patternSteps);
        for (var cy = 0; cy < cycles; cy++) {
          for (var n = 0; n < cl.notes.length; n++) {
            var nt = cl.notes[n], absStep = cy * cl.patternSteps + nt.step;
            if (absStep >= totalSteps) continue;
            var beat = r4(cl.startBeat + absStep * stepBeat);
            if (beat < fromBeat - EPS || beat >= toBeat - EPS) continue;
            var dur = r4(Math.min(nt.durationSteps * stepBeat, clEnd - beat));
            if (dur > 0) {
              events.push({
                trackId: tr.id, trackType: tr.type, clipId: cl.id, beat: beat, durationBeats: dur,
                pitch: nt.pitch, velocity: r4(clamp(nt.velocity * cl.gain, 0, 1)),
                trackVolume: tr.volume, trackPan: tr.pan
              });
            }
          }
        }
      }
    }
    return events.sort(function (a, b) { return (a.beat - b.beat) || cmpId(a.trackId, b.trackId) || (a.pitch - b.pitch); });
  }

  function checkId(v, lbl, errs) {
    if (typeof v !== 'string' || !v.trim() || v.trim() !== v || v.length > LIMITS.MAX_ID_LENGTH) { errs.push(lbl + ' invalid id'); return null; }
    return v;
  }
  function checkName(v, lbl, errs) {
    if (typeof v !== 'string' || !v.trim() || v.trim().length > LIMITS.MAX_NAME_LENGTH) { errs.push(lbl + ' invalid name'); return null; }
    return v.trim();
  }

  function validateProject(raw) {
    var errs = [];
    if (!isObj(raw)) return { ok: false, errors: ['Project must be a plain object'] };
    if (raw.schemaVersion !== SCHEMA_VERSION) errs.push('Unsupported or missing schemaVersion');
    var name = checkName(raw.name, 'name', errs);
    if (!isNum(raw.tempo) || raw.tempo < LIMITS.MIN_TEMPO || raw.tempo > LIMITS.MAX_TEMPO) errs.push('tempo out of bounds');
    if (raw.beatsPerBar !== BEATS_PER_BAR) errs.push('beatsPerBar must equal 4');
    if (!Number.isInteger(raw.lengthBars) || raw.lengthBars < LIMITS.MIN_BARS || raw.lengthBars > LIMITS.MAX_BARS) errs.push('lengthBars out of bounds');
    if (!Array.isArray(raw.tracks) || raw.tracks.length < LIMITS.MIN_TRACKS || raw.tracks.length > LIMITS.MAX_TRACKS) errs.push('tracks count out of bounds');
    if (!Array.isArray(raw.clips) || raw.clips.length > LIMITS.MAX_CLIPS) errs.push('clips count out of bounds');
    if (errs.length) return { ok: false, errors: errs };

    var totalBeats = raw.lengthBars * raw.beatsPerBar;
    var trackOrder = [], tracksById = {}, trackClipIds = {}, clipsById = {}, i, j;

    for (i = 0; i < raw.tracks.length; i++) {
      var tr = raw.tracks[i], tLbl = 'tracks[' + i + ']';
      if (!isObj(tr)) { errs.push(tLbl + ' must be object'); continue; }
      var tid = checkId(tr.id, tLbl + '.id', errs);
      if (tid && tracksById[tid]) errs.push('Duplicate track id: ' + tid);
      var tName = checkName(tr.name, tLbl + '.name', errs);
      if (typeof tr.type !== 'string' || TRACK_TYPES.indexOf(tr.type) === -1) errs.push(tLbl + '.type invalid');
      if (typeof tr.color !== 'string' || !HEX_RE.test(tr.color)) errs.push(tLbl + '.color must be #RRGGBB');
      if (!isNum(tr.volume) || tr.volume < 0 || tr.volume > 1) errs.push(tLbl + '.volume invalid');
      var pan = tr.pan === undefined ? 0 : tr.pan;
      if (!isNum(pan) || pan < -1 || pan > 1) errs.push(tLbl + '.pan invalid');
      if (typeof tr.mute !== 'boolean') errs.push(tLbl + '.mute must be boolean');
      if (typeof tr.solo !== 'boolean') errs.push(tLbl + '.solo must be boolean');
      if (tid && !tracksById[tid]) {
        trackOrder.push(tid);
        tracksById[tid] = {
          id: tid, name: tName || '', type: tr.type, color: tr.color,
          volume: isNum(tr.volume) ? r4(tr.volume) : 0.8, pan: isNum(pan) ? r4(pan) : 0,
          mute: Boolean(tr.mute), solo: Boolean(tr.solo)
        };
        trackClipIds[tid] = [];
      }
    }

    for (i = 0; i < raw.clips.length; i++) {
      var cl = raw.clips[i], cLbl = 'clips[' + i + ']';
      if (!isObj(cl)) { errs.push(cLbl + ' must be object'); continue; }
      var cid = checkId(cl.id, cLbl + '.id', errs);
      if (cid && clipsById[cid]) errs.push('Duplicate clip id: ' + cid);
      var cTid = typeof cl.trackId === 'string' ? cl.trackId : '';
      if (!tracksById[cTid]) errs.push(cLbl + '.trackId references unknown track: ' + String(cl.trackId));
      var cName = checkName(cl.name, cLbl + '.name', errs);
      var okS = isNum(cl.startBeat) && cl.startBeat >= 0 && cl.startBeat < totalBeats && isAligned(cl.startBeat);
      var okD = isNum(cl.durationBeats) && cl.durationBeats >= LIMITS.MIN_CLIP_BEATS && cl.durationBeats <= totalBeats && isAligned(cl.durationBeats);
      if (!okS) errs.push(cLbl + '.startBeat invalid');
      if (!okD) errs.push(cLbl + '.durationBeats invalid');
      if (okS && okD && cl.startBeat + cl.durationBeats > totalBeats + EPS) errs.push(cLbl + ' exceeds project length');
      var patId = checkId(cl.patternId, cLbl + '.patternId', errs);
      var patSteps = cl.patternSteps;
      var okSteps = Number.isInteger(patSteps) && patSteps >= LIMITS.MIN_PATTERN_STEPS && patSteps <= LIMITS.MAX_PATTERN_STEPS;
      if (!okSteps) errs.push(cLbl + '.patternSteps invalid');
      var gain = cl.gain === undefined ? 1 : cl.gain;
      if (!isNum(gain) || gain < 0 || gain > 1) errs.push(cLbl + '.gain invalid');

      var normNotes = [];
      if (!Array.isArray(cl.notes) || cl.notes.length > LIMITS.MAX_NOTES_PER_CLIP) errs.push(cLbl + '.notes invalid');
      else if (okSteps) {
        for (j = 0; j < cl.notes.length; j++) {
          var nt = cl.notes[j];
          if (!validNote(nt, patSteps)) errs.push(cLbl + '.notes[' + j + '] invalid');
          else normNotes.push({ step: nt.step, pitch: nt.pitch, velocity: r4(nt.velocity), durationSteps: r4(nt.durationSteps) });
        }
      }
      if (cid && !clipsById[cid] && tracksById[cTid]) {
        clipsById[cid] = {
          id: cid, trackId: cTid, name: cName || '',
          startBeat: okS ? quantizeBeat(cl.startBeat) : 0, durationBeats: okD ? quantizeBeat(cl.durationBeats) : LIMITS.MIN_CLIP_BEATS,
          patternId: patId || 'custom', patternSteps: okSteps ? patSteps : 16,
          gain: isNum(gain) ? r4(gain) : 1, notes: sortNotes(normNotes)
        };
        if (okS && okD) trackClipIds[cTid].push(cid);
      }
    }

    for (i = 0; i < trackOrder.length; i++) {
      var tk = trackOrder[i], ids = trackClipIds[tk];
      if (ids.length > LIMITS.MAX_CLIPS_PER_TRACK) errs.push('Track ' + tk + ' exceeds max clips');
      ids.sort(function (a, b) { return (clipsById[a].startBeat - clipsById[b].startBeat) || cmpId(a, b); });
      for (j = 1; j < ids.length; j++) {
        var prev = clipsById[ids[j - 1]], curr = clipsById[ids[j]];
        if (prev.startBeat + prev.durationBeats > curr.startBeat + EPS) {
          errs.push('Overlapping clips on track ' + tk + ': ' + prev.id + ' and ' + curr.id);
        }
      }
    }

    if (errs.length) return { ok: false, errors: errs };
    return {
      ok: true,
      project: {
        schemaVersion: SCHEMA_VERSION, name: name, tempo: r4(raw.tempo), beatsPerBar: BEATS_PER_BAR,
        lengthBars: raw.lengthBars, trackOrder: trackOrder, tracksById: tracksById, clipsById: clipsById, trackClipIds: trackClipIds
      }
    };
  }

  function toPublicProject(p) {
    var tracks = [], clips = [];
    for (var i = 0; i < p.trackOrder.length; i++) {
      var tid = p.trackOrder[i], ids = p.trackClipIds[tid];
      tracks.push(cloneTrack(p.tracksById[tid]));
      for (var j = 0; j < ids.length; j++) clips.push(cloneClip(p.clipsById[ids[j]]));
    }
    return { schemaVersion: p.schemaVersion, name: p.name, tempo: p.tempo, beatsPerBar: p.beatsPerBar, lengthBars: p.lengthBars, tracks: tracks, clips: clips };
  }
  function normalizedToPublicCandidate(p) {
    if (!isObj(p) || p.tracks !== undefined || p.clips !== undefined) return p;
    if (!Array.isArray(p.trackOrder) || !isObj(p.tracksById) || !isObj(p.clipsById) || !isObj(p.trackClipIds)) return p;
    var tracks = [], clips = [], seenT = {}, seenC = {}, i, j;
    for (i = 0; i < p.trackOrder.length; i++) {
      var tid = p.trackOrder[i], tr = p.tracksById[tid], cids = p.trackClipIds[tid];
      if (typeof tid !== 'string' || !isObj(tr) || tr.id !== tid || !Array.isArray(cids)) return p;
      seenT[tid] = true;
      tracks.push(cloneTrack(tr));
      for (j = 0; j < cids.length; j++) {
        var cid = cids[j], cl = p.clipsById[cid];
        if (typeof cid !== 'string' || !isObj(cl) || cl.id !== cid || cl.trackId !== tid) return p;
        seenC[cid] = true;
        clips.push({
          id: cl.id, trackId: cl.trackId, name: cl.name, startBeat: cl.startBeat, durationBeats: cl.durationBeats,
          patternId: cl.patternId, patternSteps: cl.patternSteps, gain: cl.gain,
          notes: Array.isArray(cl.notes) ? cl.notes.map(function (n) { return isObj(n) ? cloneNote(n) : n; }) : cl.notes
        });
      }
    }
    var tk = Object.keys(p.tracksById), tck = Object.keys(p.trackClipIds), ck = Object.keys(p.clipsById);
    for (i = 0; i < tk.length; i++) if (!seenT[tk[i]]) return p;
    for (i = 0; i < tck.length; i++) if (!seenT[tck[i]]) return p;
    for (i = 0; i < ck.length; i++) if (!seenC[ck[i]]) return p;
    return { schemaVersion: p.schemaVersion, name: p.name, tempo: p.tempo, beatsPerBar: p.beatsPerBar, lengthBars: p.lengthBars, tracks: tracks, clips: clips };
  }
  function serializeProject(p) { return JSON.stringify(toPublicProject(p), null, 2); }
  function deserializeProject(json) {
    if (typeof json !== 'string' || !json.length || json.length > LIMITS.MAX_JSON_CHARS) return { ok: false, errors: ['JSON payload invalid or out of bounds'] };
    try { return validateProject(JSON.parse(json)); } catch (e) { return { ok: false, errors: ['Malformed JSON: ' + e.message] }; }
  }

  var STARTER_TRACKS = [
    ['trk-kick', 'Kick', 'kick', '#e85d4a', 0.9, 0],
    ['trk-snare', 'Snare & Clap', 'snare', '#e69f38', 0.78, -0.05],
    ['trk-hat', 'Hi-Hats', 'hat', '#38b8a8', 0.68, 0.12],
    ['trk-bass', 'Analog Bass', 'bass', '#7c6ef0', 0.82, 0],
    ['trk-synth', 'Poly Synth', 'synth', '#3b9eff', 0.72, -0.1]
  ];
  var STARTER_CLIPS = [
    ['clip-kick-1', 'trk-kick', 'Four on Floor', 0, 16, 'kick-four', 0.95],
    ['clip-kick-2', 'trk-kick', 'Drop Drive', 20, 8, 'kick-drive', 0.96],
    ['clip-kick-3', 'trk-kick', 'Sync Turnaround', 28, 4, 'kick-turnaround', 0.94],
    ['clip-snare-1', 'trk-snare', 'Backbeat Clap', 4, 12, 'snare-backbeat', 0.88],
    ['clip-snare-2', 'trk-snare', 'Tension Build', 16, 8, 'snare-build', 0.92],
    ['clip-snare-3', 'trk-snare', 'Peak Snap', 24, 8, 'snare-backbeat', 0.94],
    ['clip-hat-1', 'trk-hat', 'Offbeat Pulse', 0, 12, 'hat-offbeat', 0.84],
    ['clip-hat-2', 'trk-hat', '16th Shimmer', 12, 12, 'hat-shimmer', 0.86],
    ['clip-hat-3', 'trk-hat', 'Open Drive', 24, 8, 'hat-drive', 0.92],
    ['clip-bass-1', 'trk-bass', 'A-Minor Root', 0, 16, 'bass-groove', 0.9],
    ['clip-bass-2', 'trk-bass', 'Lift Pedal', 16, 8, 'bass-pedal', 0.88],
    ['clip-bass-3', 'trk-bass', 'Octave Roll', 24, 8, 'bass-octave', 0.95],
    ['clip-synth-1', 'trk-synth', 'Glass Chords', 4, 12, 'synth-chords', 0.84],
    ['clip-synth-2', 'trk-synth', 'Rising Arp', 16, 8, 'synth-arp', 0.88],
    ['clip-synth-3', 'trk-synth', 'Night Hook', 24, 8, 'synth-hook', 0.92]
  ];

  function createStarterProject() {
    var v = validateProject({
      schemaVersion: SCHEMA_VERSION, name: 'Midnight Relay', tempo: LIMITS.DEFAULT_TEMPO,
      beatsPerBar: BEATS_PER_BAR, lengthBars: LIMITS.DEFAULT_BARS,
      tracks: STARTER_TRACKS.map(function (t) { return { id: t[0], name: t[1], type: t[2], color: t[3], volume: t[4], pan: t[5], mute: false, solo: false }; }),
      clips: STARTER_CLIPS.map(function (c) {
        var pat = PATTERN_LIBRARY[c[5]];
        return { id: c[0], trackId: c[1], name: c[2], startBeat: c[3], durationBeats: c[4], patternId: pat.id, patternSteps: pat.patternSteps, gain: c[6], notes: pat.notes.map(cloneNote) };
      })
    });
    if (!v.ok) throw new Error('Starter project invalid: ' + v.errors.join('; '));
    return v.project;
  }

  function getTrackClips(p, tid, excludeId) {
    var ids = p.trackClipIds[tid] || [], out = [];
    for (var i = 0; i < ids.length; i++) if (ids[i] !== excludeId) out.push(p.clipsById[ids[i]]);
    return out;
  }
  function getOpenGaps(p, tid, excludeId) {
    var total = getTotalBeats(p), clips = getTrackClips(p, tid, excludeId), gaps = [], cur = 0;
    for (var i = 0; i < clips.length; i++) {
      if (clips[i].startBeat > cur + EPS) gaps.push({ start: cur, end: clips[i].startBeat });
      cur = Math.max(cur, clips[i].startBeat + clips[i].durationBeats);
    }
    if (total > cur + EPS) gaps.push({ start: cur, end: total });
    return gaps;
  }
  function findNeighbors(p, tid, cid) {
    var ids = p.trackClipIds[tid] || [], idx = ids.indexOf(cid);
    return idx === -1 ? { prev: null, next: null } : {
      prev: idx > 0 ? p.clipsById[ids[idx - 1]] : null,
      next: idx < ids.length - 1 ? p.clipsById[ids[idx + 1]] : null
    };
  }
  function hasOverlap(clips, start, dur) {
    var end = start + dur;
    for (var i = 0; i < clips.length; i++) if (start < clips[i].startBeat + clips[i].durationBeats - EPS && end > clips[i].startBeat + EPS) return true;
    return false;
  }
  function resolveMoveStart(p, tid, excludeId, desired, dur, fitMode) {
    var total = getTotalBeats(p);
    if (dur > total + EPS) return null;
    var target = clamp(quantizeBeat(desired), 0, quantizeBeat(total - dur));
    var others = getTrackClips(p, tid, excludeId);
    if (!hasOverlap(others, target, dur)) return target;
    if (fitMode === 'strict') return null;

    var cands = [], end = target + dur;
    for (var i = 0; i < others.length; i++) {
      var o = others[i], oEnd = o.startBeat + o.durationBeats;
      if (target < oEnd - EPS && end > o.startBeat + EPS) {
        var L = quantizeBeat(o.startBeat - dur), R = quantizeBeat(oEnd);
        if (L >= -EPS && L + dur <= total + EPS && !hasOverlap(others, L, dur)) cands.push(L);
        if (R >= -EPS && R + dur <= total + EPS && !hasOverlap(others, R, dur)) cands.push(R);
      }
    }
    if (!cands.length) return null;
    cands.sort(function (a, b) { var d = Math.abs(a - target) - Math.abs(b - target); return Math.abs(d) > EPS ? d : a - b; });
    return cands[0];
  }

  function normSel(p, sel) {
    var tid = sel && sel.trackId && p.tracksById[sel.trackId] ? sel.trackId : (p.trackOrder[0] || null);
    var cid = sel && sel.clipId && p.clipsById[sel.clipId] ? sel.clipId : null;
    if (cid) tid = p.clipsById[cid].trackId;
    return { trackId: tid, clipId: cid };
  }
  function defSel(p) {
    var tid = p.trackOrder[0] || null;
    return { trackId: tid, clipId: tid && p.trackClipIds[tid].length ? p.trackClipIds[tid][0] : null };
  }

  function createStore(options) {
    var opts = options || {};
    var maxHistory = Number.isInteger(opts.maxHistory) && opts.maxHistory >= 1 ? opts.maxHistory : LIMITS.MAX_HISTORY;
    var initProj = opts.project ? (function () {
      var c = validateProject(normalizedToPublicCandidate(opts.project));
      if (!c.ok) throw new Error('Invalid initial project: ' + c.errors.join('; '));
      return c.project;
    })() : createStarterProject();

    var state = { project: initProj, selection: defSel(initProj), history: { past: [], future: [] } };
    var listeners = [];

    function snap() { return { project: cloneProject(state.project), selection: { trackId: state.selection.trackId, clipId: state.selection.clipId } }; }
    function getState() {
      return {
        project: cloneProject(state.project),
        selection: { trackId: state.selection.trackId, clipId: state.selection.clipId },
        history: {
          pastCount: state.history.past.length, futureCount: state.history.future.length,
          maxHistory: maxHistory, canUndo: state.history.past.length > 0, canRedo: state.history.future.length > 0
        }
      };
    }
    function emit(action) {
      if (!listeners.length) return;
      var cur = getState(), meta = { action: action }, list = listeners.slice();
      for (var i = 0; i < list.length; i++) list[i](cur, meta);
    }
    function pushPast(prev) {
      state.history.past.push(prev);
      if (state.history.past.length > maxHistory) state.history.past.shift();
      state.history.future = [];
    }
    function mutate(action, fn) {
      var prev = snap(), draft = cloneProject(state.project), sel = { trackId: state.selection.trackId, clipId: state.selection.clipId };
      var res = fn(draft, sel);
      if (!res || !res.ok || !res.changed) return res || fail('failed');
      pushPast(prev);
      state.project = draft;
      state.selection = normSel(draft, sel);
      emit(action);
      return res;
    }
    function mutateClip(action, cid, fn) {
      if (typeof cid !== 'string') return fail('invalid_argument');
      return mutate(action, function (d, sel) { var c = d.clipsById[cid]; return c ? fn(c, d, sel) : fail('not_found'); });
    }
    function mutateTrack(action, tid, fn) {
      if (typeof tid !== 'string') return fail('invalid_argument');
      return mutate(action, function (d) { var t = d.tracksById[tid]; return t ? fn(t) : fail('not_found'); });
    }

    function selectTrack(tid) {
      if (typeof tid !== 'string' || !state.project.tracksById[tid]) return fail('not_found');
      var cid = state.selection.clipId;
      if (cid && state.project.clipsById[cid].trackId !== tid) cid = null;
      var changed = state.selection.trackId !== tid || state.selection.clipId !== cid;
      state.selection = { trackId: tid, clipId: cid };
      if (changed) emit('selectTrack');
      return { ok: true, changed: changed, selection: { trackId: tid, clipId: cid } };
    }
    function selectClip(cid) {
      if (cid === null) {
        var ch = state.selection.clipId !== null;
        state.selection = { trackId: state.selection.trackId, clipId: null };
        if (ch) emit('selectClip');
        return { ok: true, changed: ch, selection: { trackId: state.selection.trackId, clipId: null } };
      }
      if (typeof cid !== 'string' || !state.project.clipsById[cid]) return fail('not_found');
      var cl = state.project.clipsById[cid];
      var changed = state.selection.clipId !== cid || state.selection.trackId !== cl.trackId;
      state.selection = { trackId: cl.trackId, clipId: cid };
      if (changed) emit('selectClip');
      return { ok: true, changed: changed, selection: { trackId: cl.trackId, clipId: cid } };
    }

    function createClip(params) {
      if (!isObj(params)) return fail('invalid_argument');
      return mutate('createClip', function (d, sel) {
        var tid = params.trackId !== undefined ? params.trackId : sel.trackId, tr = d.tracksById[tid];
        if (!tr) return fail('track_not_found');
        if (Object.keys(d.clipsById).length >= LIMITS.MAX_CLIPS) return fail('project_clip_limit');
        if (d.trackClipIds[tid].length >= LIMITS.MAX_CLIPS_PER_TRACK) return fail('track_clip_limit');

        var total = getTotalBeats(d), reqDur = params.durationBeats === undefined ? LIMITS.DEFAULT_CLIP_BEATS : params.durationBeats;
        if (!isNum(reqDur) || reqDur < LIMITS.MIN_CLIP_BEATS) return fail('invalid_duration');
        reqDur = quantizeBeat(Math.min(reqDur, total));

        var gaps = getOpenGaps(d, tid, null), startBeat, durationBeats, i, g;
        if (params.startBeat !== undefined) {
          if (!isNum(params.startBeat)) return fail('invalid_start');
          var qS = quantizeBeat(params.startBeat);
          if (qS < 0 || qS > total - LIMITS.MIN_CLIP_BEATS + EPS) return fail('out_of_bounds');
          var hit = null;
          for (i = 0; i < gaps.length; i++) { g = gaps[i]; if (qS >= g.start - EPS && qS <= g.end - LIMITS.MIN_CLIP_BEATS + EPS) { hit = g; break; } }
          if (!hit) return fail('collision');
          startBeat = qS; durationBeats = Math.min(reqDur, quantizeBeat(hit.end - startBeat));
          if (durationBeats < LIMITS.MIN_CLIP_BEATS - EPS) return fail('collision');
        } else {
          var best = null;
          for (i = 0; i < gaps.length; i++) if (gaps[i].end - gaps[i].start >= reqDur - EPS) { best = gaps[i]; break; }
          if (!best) for (i = 0; i < gaps.length; i++) if (gaps[i].end - gaps[i].start >= LIMITS.MIN_CLIP_BEATS - EPS) { best = gaps[i]; break; }
          if (!best) return fail('no_space');
          startBeat = quantizeBeat(best.start); durationBeats = Math.min(reqDur, quantizeBeat(best.end - best.start));
        }

        var pat = (params.patternId && getPatternById(params.patternId)) || defaultPatternFor(tr.type);
        var name = typeof params.name === 'string' && params.name.trim() ? params.name.trim().slice(0, LIMITS.MAX_NAME_LENGTH) : pat.name;
        var gain = params.gain === undefined ? 0.9 : params.gain;
        if (!isNum(gain)) return fail('invalid_gain');
        var steps = pat.patternSteps, notes = pat.notes.map(cloneNote);
        if (Array.isArray(params.notes)) {
          if (params.notes.length > LIMITS.MAX_NOTES_PER_CLIP) return fail('note_limit');
          notes = [];
          for (i = 0; i < params.notes.length; i++) {
            var nt = params.notes[i];
            if (!validNote(nt, steps)) return fail('invalid_note');
            notes.push({ step: nt.step, pitch: nt.pitch, velocity: r4(nt.velocity), durationSteps: r4(nt.durationSteps) });
          }
          notes = sortNotes(notes);
        }

        var newId = nextClipId(d, tid);
        var clip = {
          id: newId, trackId: tid, name: name, startBeat: startBeat, durationBeats: durationBeats,
          patternId: Array.isArray(params.notes) || (params.patternId && !getPatternById(params.patternId)) ? 'custom' : pat.id,
          patternSteps: steps, gain: r4(clamp(gain, 0, 1)), notes: notes
        };
        d.clipsById[newId] = clip; d.trackClipIds[tid].push(newId); sortTrackClips(d, tid);
        sel.trackId = tid; sel.clipId = newId;
        return { ok: true, changed: true, clip: cloneClip(clip) };
      });
    }

    function moveClip(cid, params) {
      if (!isObj(params)) return fail('invalid_argument');
      return mutateClip('moveClip', cid, function (c, d, sel) {
        var tid = params.trackId !== undefined ? params.trackId : c.trackId;
        if (typeof tid !== 'string' || !d.tracksById[tid]) return fail('track_not_found');
        var desired = params.startBeat !== undefined ? params.startBeat : c.startBeat;
        if (!isNum(desired)) return fail('invalid_start');
        if (tid !== c.trackId && d.trackClipIds[tid].length >= LIMITS.MAX_CLIPS_PER_TRACK) return fail('track_clip_limit');
        var start = resolveMoveStart(d, tid, cid, desired, c.durationBeats, params.fitMode === 'strict' ? 'strict' : 'clamp');
        if (start === null) return fail('collision');
        if (tid === c.trackId && Math.abs(start - c.startBeat) < EPS) return { ok: true, changed: false, clip: cloneClip(c) };
        if (tid !== c.trackId) {
          var old = d.trackClipIds[c.trackId], idx = old.indexOf(cid);
          if (idx !== -1) old.splice(idx, 1);
          d.trackClipIds[tid].push(cid); c.trackId = tid;
        }
        c.startBeat = start; sortTrackClips(d, tid);
        sel.trackId = tid; sel.clipId = cid;
        return { ok: true, changed: true, clip: cloneClip(c) };
      });
    }

    function resizeClip(cid, params) {
      if (!isObj(params)) return fail('invalid_argument');
      return mutateClip('resizeClip', cid, function (c, d) {
        var hasS = params.startBeat !== undefined, hasD = params.durationBeats !== undefined;
        if ((!hasS && !hasD) || (hasS && !isNum(params.startBeat)) || (hasD && !isNum(params.durationBeats))) return fail('invalid_argument');
        var nb = findNeighbors(d, c.trackId, cid);
        var minS = nb.prev ? quantizeBeat(nb.prev.startBeat + nb.prev.durationBeats) : 0;
        var maxE = nb.next ? quantizeBeat(nb.next.startBeat) : getTotalBeats(d);
        var oldE = quantizeBeat(c.startBeat + c.durationBeats), nextS = c.startBeat, nextD = c.durationBeats;
        if (hasS && !hasD) { nextS = clamp(quantizeBeat(params.startBeat), minS, quantizeBeat(oldE - LIMITS.MIN_CLIP_BEATS)); nextD = quantizeBeat(oldE - nextS); }
        else if (!hasS && hasD) { nextD = clamp(quantizeBeat(params.durationBeats), LIMITS.MIN_CLIP_BEATS, quantizeBeat(maxE - c.startBeat)); }
        else { nextS = clamp(quantizeBeat(params.startBeat), minS, quantizeBeat(maxE - LIMITS.MIN_CLIP_BEATS)); nextD = clamp(quantizeBeat(params.durationBeats), LIMITS.MIN_CLIP_BEATS, quantizeBeat(maxE - nextS)); }
        if (Math.abs(nextS - c.startBeat) < EPS && Math.abs(nextD - c.durationBeats) < EPS) return { ok: true, changed: false, clip: cloneClip(c) };
        c.startBeat = nextS; c.durationBeats = nextD; sortTrackClips(d, c.trackId);
        return { ok: true, changed: true, clip: cloneClip(c) };
      });
    }

    function duplicateClip(cid) {
      return mutateClip('duplicateClip', cid, function (src, d, sel) {
        if (Object.keys(d.clipsById).length >= LIMITS.MAX_CLIPS) return fail('project_clip_limit');
        if (d.trackClipIds[src.trackId].length >= LIMITS.MAX_CLIPS_PER_TRACK) return fail('track_clip_limit');
        var gaps = getOpenGaps(d, src.trackId, null), after = quantizeBeat(src.startBeat + src.durationBeats), placed = null, i;
        for (i = 0; i < gaps.length; i++) { var cand = Math.max(gaps[i].start, after); if (gaps[i].end - cand >= src.durationBeats - EPS) { placed = quantizeBeat(cand); break; } }
        if (placed === null) for (i = 0; i < gaps.length; i++) if (gaps[i].end - gaps[i].start >= src.durationBeats - EPS) { placed = quantizeBeat(gaps[i].start); break; }
        if (placed === null) return fail('no_space');
        var copy = cloneClip(src);
        copy.id = nextClipId(d, src.trackId);
        copy.name = (src.name.replace(/\s+\(Copy(?:\s+\d+)?\)$/, '') + ' (Copy)').slice(0, LIMITS.MAX_NAME_LENGTH);
        copy.startBeat = placed;
        d.clipsById[copy.id] = copy; d.trackClipIds[src.trackId].push(copy.id); sortTrackClips(d, src.trackId);
        sel.trackId = src.trackId; sel.clipId = copy.id;
        return { ok: true, changed: true, clip: cloneClip(copy) };
      });
    }

    function deleteClip(cid) {
      return mutateClip('deleteClip', cid, function (c, d, sel) {
        var tid = c.trackId, ids = d.trackClipIds[tid], idx = ids.indexOf(cid);
        delete d.clipsById[cid];
        if (idx !== -1) ids.splice(idx, 1);
        if (sel.clipId === cid) { sel.clipId = null; sel.trackId = tid; }
        return { ok: true, changed: true, deletedId: cid };
      });
    }
    function renameClip(cid, name) {
      if (typeof name !== 'string' || !name.trim() || name.trim().length > LIMITS.MAX_NAME_LENGTH) return fail('invalid_name');
      var tr = name.trim();
      return mutateClip('renameClip', cid, function (c) {
        if (c.name === tr) return { ok: true, changed: false, clip: cloneClip(c) };
        c.name = tr; return { ok: true, changed: true, clip: cloneClip(c) };
      });
    }
    function setClipGain(cid, gain) {
      if (!isNum(gain)) return fail('invalid_argument');
      var next = r4(clamp(gain, 0, 1));
      return mutateClip('setClipGain', cid, function (c) {
        if (Math.abs(c.gain - next) < EPS) return { ok: true, changed: false, clip: cloneClip(c) };
        c.gain = next; return { ok: true, changed: true, clip: cloneClip(c) };
      });
    }
    function setClipPattern(cid, patternId) {
      var pat = typeof patternId === 'string' ? getPatternById(patternId) : null;
      if (!pat) return fail('pattern_not_found');
      return mutateClip('setClipPattern', cid, function (c) {
        c.patternId = pat.id; c.patternSteps = pat.patternSteps; c.notes = pat.notes.map(cloneNote);
        return { ok: true, changed: true, clip: cloneClip(c) };
      });
    }
    function toggleClipNote(cid, params) {
      if (!isObj(params)) return fail('invalid_argument');
      return mutateClip('toggleClipNote', cid, function (c, d) {
        var tr = d.tracksById[c.trackId];
        var nt = {
          step: params.step, pitch: params.pitch !== undefined ? params.pitch : TRACK_TYPE_META[tr.type].defaultPitch,
          velocity: params.velocity !== undefined ? params.velocity : 0.85, durationSteps: params.durationSteps !== undefined ? params.durationSteps : 1
        };
        if (!validNote(nt, c.patternSteps)) return fail('invalid_note');
        var idx = -1;
        for (var i = 0; i < c.notes.length; i++) if (c.notes[i].step === nt.step && c.notes[i].pitch === nt.pitch) { idx = i; break; }
        if (idx !== -1) c.notes.splice(idx, 1);
        else {
          if (c.notes.length >= LIMITS.MAX_NOTES_PER_CLIP) return fail('note_limit');
          c.notes.push({ step: nt.step, pitch: nt.pitch, velocity: r4(nt.velocity), durationSteps: r4(nt.durationSteps) });
          c.notes = sortNotes(c.notes);
        }
        c.patternId = 'custom';
        return { ok: true, changed: true, clip: cloneClip(c) };
      });
    }

    function setTrackBool(action, tid, field, val) {
      if (typeof val !== 'boolean') return fail('invalid_argument');
      return mutateTrack(action, tid, function (t) {
        if (t[field] === val) return { ok: true, changed: false, track: cloneTrack(t) };
        t[field] = val; return { ok: true, changed: true, track: cloneTrack(t) };
      });
    }
    function setTrackNum(action, tid, field, val, lo, hi) {
      if (!isNum(val)) return fail('invalid_argument');
      var next = r4(clamp(val, lo, hi));
      return mutateTrack(action, tid, function (t) {
        if (Math.abs(t[field] - next) < EPS) return { ok: true, changed: false, track: cloneTrack(t) };
        t[field] = next; return { ok: true, changed: true, track: cloneTrack(t) };
      });
    }

    function setTrackMute(tid, v) { return setTrackBool('setTrackMute', tid, 'mute', v); }
    function toggleTrackMute(tid) { var t = state.project.tracksById[tid]; return t ? setTrackMute(tid, !t.mute) : fail('not_found'); }
    function setTrackSolo(tid, v) { return setTrackBool('setTrackSolo', tid, 'solo', v); }
    function toggleTrackSolo(tid) { var t = state.project.tracksById[tid]; return t ? setTrackSolo(tid, !t.solo) : fail('not_found'); }
    function setTrackVolume(tid, v) { return setTrackNum('setTrackVolume', tid, 'volume', v, 0, 1); }
    function setTrackPan(tid, v) { return setTrackNum('setTrackPan', tid, 'pan', v, -1, 1); }
    function renameTrack(tid, name) {
      if (typeof name !== 'string' || !name.trim() || name.trim().length > LIMITS.MAX_NAME_LENGTH) return fail('invalid_name');
      var tr = name.trim();
      return mutateTrack('renameTrack', tid, function (t) {
        if (t.name === tr) return { ok: true, changed: false, track: cloneTrack(t) };
        t.name = tr; return { ok: true, changed: true, track: cloneTrack(t) };
      });
    }

    function setProjectName(name) {
      if (typeof name !== 'string' || !name.trim() || name.trim().length > LIMITS.MAX_NAME_LENGTH) return fail('invalid_name');
      var tr = name.trim();
      return mutate('setProjectName', function (d) {
        if (d.name === tr) return { ok: true, changed: false, name: d.name };
        d.name = tr; return { ok: true, changed: true, name: d.name };
      });
    }
    function setTempo(bpm) {
      if (!isNum(bpm)) return fail('invalid_argument');
      var next = r4(clamp(bpm, LIMITS.MIN_TEMPO, LIMITS.MAX_TEMPO));
      return mutate('setTempo', function (d) {
        if (Math.abs(d.tempo - next) < EPS) return { ok: true, changed: false, tempo: d.tempo };
        d.tempo = next; return { ok: true, changed: true, tempo: d.tempo };
      });
    }
    function setLengthBars(bars, optsObj) {
      if (!Number.isInteger(bars) || bars < LIMITS.MIN_BARS || bars > LIMITS.MAX_BARS) return fail('invalid_bars');
      var truncate = Boolean(optsObj && optsObj.truncate);
      return mutate('setLengthBars', function (d, sel) {
        if (d.lengthBars === bars) return { ok: true, changed: false, lengthBars: bars };
        var nextTotal = bars * d.beatsPerBar, keys = Object.keys(d.clipsById), i;
        if (!truncate) {
          for (i = 0; i < keys.length; i++) if (d.clipsById[keys[i]].startBeat + d.clipsById[keys[i]].durationBeats > nextTotal + EPS) return fail('clips_out_of_bounds');
        } else {
          for (i = 0; i < keys.length; i++) {
            var cid = keys[i], clip = d.clipsById[cid];
            if (clip.startBeat >= nextTotal - LIMITS.MIN_CLIP_BEATS + EPS) {
              delete d.clipsById[cid];
              var arr = d.trackClipIds[clip.trackId], idx = arr.indexOf(cid);
              if (idx !== -1) arr.splice(idx, 1);
              if (sel.clipId === cid) sel.clipId = null;
            } else if (clip.startBeat + clip.durationBeats > nextTotal + EPS) {
              clip.durationBeats = quantizeBeat(nextTotal - clip.startBeat);
            }
          }
        }
        d.lengthBars = bars;
        return { ok: true, changed: true, lengthBars: bars };
      });
    }

    function stepHistory(fromStack, toStack, action, emptyReason) {
      if (!fromStack.length) return fail(emptyReason);
      toStack.push(snap());
      if (toStack.length > maxHistory) toStack.shift();
      var frame = fromStack.pop();
      state.project = frame.project;
      state.selection = normSel(frame.project, frame.selection);
      emit(action);
      return { ok: true, changed: true };
    }
    function commitImport(action, res) {
      if (!res.ok) return { ok: false, changed: false, errors: res.errors };
      state.project = res.project; state.selection = defSel(res.project);
      state.history.past = []; state.history.future = [];
      emit(action);
      return { ok: true, changed: true };
    }

    return {
      getState: getState,
      subscribe: function (fn) {
        if (typeof fn !== 'function') throw new TypeError('Listener must be a function');
        listeners.push(fn);
        return function () { var idx = listeners.indexOf(fn); if (idx !== -1) listeners.splice(idx, 1); };
      },
      selectTrack: selectTrack, selectClip: selectClip,
      createClip: createClip, moveClip: moveClip, resizeClip: resizeClip,
      duplicateClip: duplicateClip, deleteClip: deleteClip,
      renameClip: renameClip, setClipGain: setClipGain, setClipPattern: setClipPattern, toggleClipNote: toggleClipNote,
      setTrackMute: setTrackMute, toggleTrackMute: toggleTrackMute, setTrackSolo: setTrackSolo, toggleTrackSolo: toggleTrackSolo,
      setTrackVolume: setTrackVolume, setTrackPan: setTrackPan, renameTrack: renameTrack,
      setProjectName: setProjectName, setTempo: setTempo, setLengthBars: setLengthBars,
      undo: function () { return stepHistory(state.history.past, state.history.future, 'undo', 'nothing_to_undo'); },
      redo: function () { return stepHistory(state.history.future, state.history.past, 'redo', 'nothing_to_redo'); },
      canUndo: function () { return state.history.past.length > 0; },
      canRedo: function () { return state.history.future.length > 0; },
      importProject: function (raw) { return commitImport('importProject', validateProject(raw)); },
      importJSON: function (json) { return commitImport('importJSON', deserializeProject(json)); },
      exportJSON: function () { return serializeProject(state.project); },
      exportPublicProject: function () { return toPublicProject(state.project); },
      resetToStarter: function () {
        var prev = snap(), starter = createStarterProject();
        pushPast(prev); state.project = starter; state.selection = defSel(starter);
        emit('resetToStarter');
        return { ok: true, changed: true };
      }
    };
  }

  return Object.freeze({
    SCHEMA_VERSION: SCHEMA_VERSION, LIMITS: LIMITS, TRACK_TYPES: TRACK_TYPES,
    TRACK_TYPE_META: TRACK_TYPE_META, PATTERN_LIBRARY: PATTERN_LIBRARY,
    createStarterProject: createStarterProject, createStore: createStore,
    validateProject: validateProject, toPublicProject: toPublicProject,
    serializeProject: serializeProject, deserializeProject: deserializeProject,
    getTotalBeats: getTotalBeats, isTrackAudible: isTrackAudible,
    expandProjectEvents: expandProjectEvents, getPatternsForTrackType: getPatternsForTrackType,
    getPatternById: getPatternById, quantizeBeat: quantizeBeat
  });
});
