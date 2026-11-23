/**
 * Cue Room — Application Controller & Module Glue
 * Glues CueModel, CueAudio, and CueTimeline to the DOM workstation UI.
 * Zero build, zero network, resilient localStorage, validated JSON import/export.
 */
(function (root, factory) {
  'use strict';
  var api = factory(root);
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.CueApp = api;
})(typeof globalThis !== 'undefined' ? globalThis : typeof window !== 'undefined' ? window : this, function (rootObj) {
  'use strict';

  var STORAGE_KEY = 'cue-room.project.v1';
  var EPS = 1e-6;
  var NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
  var BASE_BAR_OPTIONS = [4, 8, 12, 16, 32];

  var DRUM_PITCH_PRESETS = {
    kick: [{ pitch: 36, label: 'Kick (36)' }],
    snare: [
      { pitch: 38, label: 'Snare (38)' },
      { pitch: 39, label: 'Clap (39)' }
    ],
    hat: [
      { pitch: 42, label: 'Closed Hat (42)' },
      { pitch: 46, label: 'Open Hat (46)' }
    ]
  };

  var DRUM_SHORT_NAMES = {
    36: 'Kd',
    38: 'Sn',
    39: 'Cp',
    42: 'CH',
    46: 'OH'
  };

  var MELODIC_PITCH_RANGES = {
    bass: [28, 29, 31, 33, 35, 36, 38, 40, 41, 43, 45, 48],
    synth: [53, 55, 57, 59, 60, 62, 64, 65, 67, 69, 71, 72, 74, 76, 79],
    pad: [48, 52, 53, 55, 57, 60, 62, 64, 67, 69, 72]
  };

  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
  function r2(v) { return Math.round(v * 100) / 100; }

  function midiToLabel(midi) {
    var oct = Math.floor(midi / 12) - 1;
    return NOTE_NAMES[((midi % 12) + 12) % 12] + oct + ' (' + midi + ')';
  }

  function midiToShortLabel(midi, trackType) {
    if ((trackType === 'kick' || trackType === 'snare' || trackType === 'hat') && DRUM_SHORT_NAMES[midi]) {
      return DRUM_SHORT_NAMES[midi];
    }
    var oct = Math.floor(midi / 12) - 1;
    return NOTE_NAMES[((midi % 12) + 12) % 12] + oct;
  }

  function pitchLabelForTrack(midi, trackType) {
    var presets = DRUM_PITCH_PRESETS[trackType];
    if (presets) {
      for (var i = 0; i < presets.length; i++) {
        if (presets[i].pitch === midi) return presets[i].label;
      }
    }
    return midiToLabel(midi);
  }

  function formatTransportPos(beat, beatsPerBar) {
    var bpb = beatsPerBar || 4;
    var safe = Math.max(0, Number.isFinite(beat) ? beat : 0);
    var totalSteps = Math.floor(safe * 4 + 1e-5);
    var stepsPerBar = bpb * 4;
    var bar = Math.floor(totalSteps / stepsPerBar) + 1;
    var rem = totalSteps % stepsPerBar;
    var beatNum = Math.floor(rem / 4) + 1;
    var sixteenth = (rem % 4) + 1;
    return bar + '.' + beatNum + '.' + sixteenth;
  }

  function formatPanLabel(pan) {
    var pct = Math.round(clamp(Number.isFinite(pan) ? pan : 0, -1, 1) * 100);
    if (Math.abs(pct) < 2) return 'Center';
    return (pct < 0 ? 'L ' : 'R ') + Math.abs(pct);
  }

  function slugifyFilename(name) {
    var slug = String(name || 'cue-room-project')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 48);
    return (slug || 'cue-room-project') + '.cueroom.json';
  }

  function isTextEditable(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.isContentEditable) return true;
    var tag = el.tagName;
    if (tag === 'TEXTAREA') return true;
    if (tag === 'INPUT') {
      var t = String(el.type || 'text').toLowerCase();
      return t !== 'button' && t !== 'checkbox' && t !== 'radio' && t !== 'range' &&
        t !== 'submit' && t !== 'reset' && t !== 'file';
    }
    var role = el.getAttribute && el.getAttribute('role');
    return role === 'textbox' || role === 'searchbox' || role === 'spinbutton';
  }

  function isNativeFormOrWidgetControl(el) {
    if (!el || el.nodeType !== 1) return false;
    if (isTextEditable(el)) return true;
    var tag = el.tagName;
    if (tag === 'SELECT' || tag === 'INPUT' || tag === 'OPTION') return true;
    var role = el.getAttribute && el.getAttribute('role');
    return role === 'combobox' || role === 'listbox' || role === 'slider' ||
      role === 'checkbox' || role === 'radio' || role === 'switch' ||
      role === 'menuitem' || role === 'option';
  }

  function isNativeActivatable(el) {
    if (!el || el.nodeType !== 1) return false;
    var tag = el.tagName;
    return tag === 'BUTTON' || tag === 'A' || tag === 'SUMMARY';
  }

  function getSafeStorage(win) {
    try {
      if (!win || !win.localStorage) return null;
      return win.localStorage;
    } catch (_) {
      return null;
    }
  }

  function safeStorageRead(win, key) {
    try {
      var ls = getSafeStorage(win);
      if (!ls) return { ok: false, blocked: true, value: null };
      return { ok: true, blocked: false, value: ls.getItem(key) };
    } catch (_) {
      return { ok: false, blocked: true, value: null };
    }
  }

  function safeStorageWrite(win, key, value) {
    try {
      var ls = getSafeStorage(win);
      if (!ls) return { ok: false, reason: 'unavailable' };
      ls.setItem(key, value);
      return { ok: true };
    } catch (err) {
      return { ok: false, reason: err && err.name ? err.name : 'storage_error' };
    }
  }

  function buildPitchOptions(trackType, clip) {
    var base = DRUM_PITCH_PRESETS[trackType]
      ? DRUM_PITCH_PRESETS[trackType].slice()
      : (MELODIC_PITCH_RANGES[trackType] || MELODIC_PITCH_RANGES.synth).map(function (p) {
          return { pitch: p, label: midiToLabel(p) };
        });

    var seen = {};
    for (var i = 0; i < base.length; i++) seen[base[i].pitch] = true;

    var notes = (clip && Array.isArray(clip.notes)) ? clip.notes : [];
    for (var j = 0; j < notes.length; j++) {
      var np = notes[j].pitch;
      if (!seen[np]) {
        seen[np] = true;
        base.push({ pitch: np, label: midiToLabel(np) });
      }
    }
    base.sort(function (a, b) { return a.pitch - b.pitch; });
    return base;
  }

  function createApp(options) {
    var opts = options || {};
    var win = opts.window || (typeof window !== 'undefined' ? window : rootObj);
    var doc = opts.document || (typeof document !== 'undefined' ? document : null);
    var Model = opts.model || (win && win.CueModel) || (rootObj && rootObj.CueModel);
    var Audio = opts.audio || (win && win.CueAudio) || (rootObj && rootObj.CueAudio);
    var Timeline = opts.timeline || (win && win.CueTimeline) || (rootObj && rootObj.CueTimeline);

    if (!doc) throw new Error('DOM document is required for CueApp');
    if (!Model || !Audio || !Timeline) {
      var fallbackStatus = doc.getElementById('status-region');
      if (fallbackStatus) {
        fallbackStatus.textContent = 'Module load error';
        fallbackStatus.setAttribute('data-tone', 'error');
      }
      throw new Error('CueModel, CueAudio, and CueTimeline must load before app.js');
    }

    function $(id) { return doc.getElementById(id); }

    var dom = {
      app: $('app'),
      projectName: $('project-name-input'),
      btnPlayPause: $('btn-play-pause'),
      btnStop: $('btn-stop'),
      btnLoop: $('btn-loop'),
      transportPos: $('transport-position'),
      transportScrub: $('transport-scrub'),
      btnTempoDown: $('btn-tempo-down'),
      tempoInput: $('tempo-input'),
      btnTempoUp: $('btn-tempo-up'),
      masterVol: $('master-volume'),
      masterMeterFill: $('master-meter-fill'),
      btnUndo: $('btn-undo'),
      btnRedo: $('btn-redo'),
      btnSaveLocal: $('btn-save-local'),
      btnResetProject: $('btn-reset-project'),
      btnImport: $('btn-import'),
      btnExport: $('btn-export'),
      importFileInput: $('import-file-input'),
      btnAddClip: $('btn-add-clip'),
      lengthBarsSelect: $('length-bars-select'),
      statusRegion: $('status-region'),
      patternDesk: $('pattern-desk'),
      deskTrackBadge: $('desk-track-badge'),
      deskClipName: $('desk-clip-name'),
      deskClipMeta: $('desk-clip-meta'),
      deskDensityReadout: $('desk-density-readout'),
      patternDeskEmpty: $('pattern-desk-empty'),
      patternDeskBody: $('pattern-desk-body'),
      stepPitchSelect: $('step-pitch-select'),
      btnAuditionNote: $('btn-audition-note'),
      stepGrid: $('step-grid'),
      deskNoteStrip: $('desk-note-strip'),
      clipTrackBadge: $('clip-track-badge'),
      inspectorEmpty: $('inspector-empty'),
      clipInspectorBody: $('clip-inspector-body'),
      clipNameInput: $('clip-name-input'),
      btnClipNudgeLeft: $('btn-clip-nudge-left'),
      clipStartInput: $('clip-start-input'),
      btnClipNudgeRight: $('btn-clip-nudge-right'),
      btnClipShorten: $('btn-clip-shorten'),
      clipDurationInput: $('clip-duration-input'),
      btnClipLengthen: $('btn-clip-lengthen'),
      clipPatternSelect: $('clip-pattern-select'),
      clipGainInput: $('clip-gain-input'),
      clipGainValue: $('clip-gain-value'),
      btnClipDuplicate: $('btn-clip-duplicate'),
      btnClipDelete: $('btn-clip-delete'),
      trackTypeBadge: $('track-type-badge'),
      trackNameInput: $('track-name-input'),
      inspectorTrackMute: $('inspector-track-mute'),
      inspectorTrackSolo: $('inspector-track-solo'),
      btnTrackAddClip: $('btn-track-add-clip'),
      inspectorTrackVol: $('inspector-track-volume'),
      inspectorTrackVolVal: $('inspector-track-volume-value'),
      inspectorTrackPan: $('inspector-track-pan'),
      inspectorTrackPanVal: $('inspector-track-pan-value')
    };

    var storageKey = opts.storageKey || STORAGE_KEY;
    var statusTimer = null;
    var autoSaveTimer = null;
    var rafId = null;
    var disposed = false;
    var isScrubbing = false;
    var isDraggingClipGain = false;
    var isDraggingTrackVol = false;
    var isDraggingTrackPan = false;
    var lastRenderedClipId = null;
    var lastRenderedTrackId = null;
    var lastPatternTrackType = null;
    var lastPitchSig = '';
    var lastPlayheadStepEl = null;
    var clipPitchMemory = {};

    function setStatus(message, tone, optionsObj) {
      if (!dom.statusRegion) return;
      var t = tone || 'neutral';
      dom.statusRegion.textContent = String(message || 'Ready');
      dom.statusRegion.setAttribute('data-tone', t);
      if (optionsObj && optionsObj.title) dom.statusRegion.setAttribute('title', optionsObj.title);
      else dom.statusRegion.setAttribute('title', String(message || ''));

      if (statusTimer !== null && win && typeof win.clearTimeout === 'function') {
        win.clearTimeout(statusTimer);
        statusTimer = null;
      }
      if (t !== 'error' && message && message !== 'Ready' && win && typeof win.setTimeout === 'function') {
        statusTimer = win.setTimeout(function () {
          statusTimer = null;
          if (!disposed && dom.statusRegion && dom.statusRegion.getAttribute('data-tone') !== 'error') {
            dom.statusRegion.textContent = 'Ready';
            dom.statusRegion.setAttribute('data-tone', 'neutral');
            dom.statusRegion.setAttribute('title', 'Ready');
          }
        }, 4200);
      }
    }

    // --- Initial Project Boot (Resilient localStorage -> Starter Fallback) ---

    var bootStatus = { message: 'Ready', tone: 'neutral' };
    var initialProject = null;

    var stored = safeStorageRead(win, storageKey);
    if (stored.ok && typeof stored.value === 'string' && stored.value.trim()) {
      var parsed = Model.deserializeProject(stored.value);
      if (parsed.ok) {
        initialProject = parsed.project;
        bootStatus = { message: 'Restored saved project', tone: 'neutral' };
      } else {
        bootStatus = {
          message: 'Saved data invalid — loaded starter',
          tone: 'error',
          title: 'Local storage validation failed: ' + parsed.errors.join('; ')
        };
      }
    }

    var store = initialProject ? Model.createStore({ project: initialProject }) : Model.createStore();

    var audio = Audio.createEngine({
      store: store,
      model: Model,
      loop: true,
      masterVolume: 0.85
    });

    var timeline = Timeline.createTimeline({
      store: store,
      model: Model,
      document: doc,
      window: win,
      autoSubscribe: false,
      onStatus: function (msg, tone) { setStatus(msg, tone); },
      onSeek: function (beat) {
        audio.seek(beat);
        syncTransportPosition(beat, false);
      },
      onTrackVolumePreview: function (trackId, vol) {
        var st = store.getState();
        if (st.selection.trackId === trackId && !isDraggingTrackVol) {
          var pct = Math.round(clamp(vol, 0, 1) * 100);
          if (dom.inspectorTrackVol) dom.inspectorTrackVol.value = String(pct);
          if (dom.inspectorTrackVolVal) dom.inspectorTrackVolVal.textContent = pct + '%';
        }
      },
      onClipAudition: function (clip, track) {
        if (!clip || !track) return;
        var pitch = clip.notes && clip.notes.length
          ? clip.notes[0].pitch
          : (Model.TRACK_TYPE_META[track.type] ? Model.TRACK_TYPE_META[track.type].defaultPitch : 60);
        audio.auditionNote(track.type, pitch, 0.85, 0.5);
      }
    });

    // --- Storage Save Helpers ---

    function saveProjectToLocal(explicit) {
      var json = store.exportJSON();
      var res = safeStorageWrite(win, storageKey, json);
      if (res.ok) {
        if (explicit) setStatus('Saved locally', 'ok');
        return true;
      }
      if (explicit) {
        setStatus('Browser storage unavailable — use Export', 'error', {
          title: 'Could not write to localStorage (' + (res.reason || 'blocked') + '). Use Export JSON to save a file.'
        });
      }
      return false;
    }

    function scheduleAutoSave() {
      if (autoSaveTimer !== null && win && typeof win.clearTimeout === 'function') {
        win.clearTimeout(autoSaveTimer);
      }
      if (win && typeof win.setTimeout === 'function') {
        autoSaveTimer = win.setTimeout(function () {
          autoSaveTimer = null;
          if (!disposed) saveProjectToLocal(false);
        }, 350);
      }
    }

    // --- Transport, Playhead Step Chase & Animation Loop ---

    function clearDeskPlayheadStep() {
      if (lastPlayheadStepEl) {
        lastPlayheadStepEl.removeAttribute('data-playhead');
        lastPlayheadStepEl = null;
      }
    }

    function updateDeskPlayheadStep(beat, isPlaying) {
      if (!dom.stepGrid) return;
      var st = store.getState();
      var cid = st.selection && st.selection.clipId;
      var cl = cid ? st.project.clipsById[cid] : null;
      if (!cl || !isPlaying || beat < cl.startBeat - EPS || beat >= cl.startBeat + cl.durationBeats - EPS) {
        clearDeskPlayheadStep();
        return;
      }
      var stepsCount = clamp(cl.patternSteps || 16, Model.LIMITS.MIN_PATTERN_STEPS, Model.LIMITS.MAX_PATTERN_STEPS);
      var localStep = Math.floor((beat - cl.startBeat) * 4 + 1e-5) % stepsCount;
      if (localStep < 0) localStep += stepsCount;
      var nextEl = dom.stepGrid.querySelector('.step-cell[data-step="' + localStep + '"]');
      if (nextEl !== lastPlayheadStepEl) {
        clearDeskPlayheadStep();
        if (nextEl) {
          nextEl.setAttribute('data-playhead', 'true');
          lastPlayheadStepEl = nextEl;
        }
      }
    }

    function syncTransportPosition(beat, autoScroll) {
      var st = store.getState();
      var bpb = st.project.beatsPerBar || 4;
      var total = Model.getTotalBeats(st.project);
      var clamped = clamp(Number.isFinite(beat) ? beat : 0, 0, total);
      var playing = audio.getTransportState().isPlaying;

      if (dom.transportPos) dom.transportPos.textContent = formatTransportPos(clamped, bpb);
      if (dom.transportScrub) {
        dom.transportScrub.max = String(total);
        if (!isScrubbing) dom.transportScrub.value = String(r2(clamped));
        dom.transportScrub.setAttribute('aria-valuetext', Timeline.formatBeatPosition(clamped, bpb));
      }
      timeline.updatePlayhead(clamped, {
        autoScroll: Boolean(autoScroll),
        followPlayhead: Boolean(autoScroll && playing)
      });
      updateDeskPlayheadStep(clamped, playing);
    }

    function setMeterLevel(level) {
      if (dom.masterMeterFill) {
        dom.masterMeterFill.style.setProperty('--meter-level', String(r2(clamp(level || 0, 0, 1))));
      }
    }

    function stopRafLoop() {
      if (rafId !== null && win && typeof win.cancelAnimationFrame === 'function') {
        win.cancelAnimationFrame(rafId);
      }
      rafId = null;
    }

    function rafTick() {
      rafId = null;
      if (disposed) return;
      var ts = audio.getTransportState();
      if (!ts.isPlaying) {
        setMeterLevel(0);
        clearDeskPlayheadStep();
        return;
      }
      syncTransportPosition(audio.getPlayheadBeat(), true);
      setMeterLevel(audio.getMasterLevel());
      if (win && typeof win.requestAnimationFrame === 'function') {
        rafId = win.requestAnimationFrame(rafTick);
      }
    }

    function ensureRafLoop() {
      if (rafId !== null || disposed) return;
      if (win && typeof win.requestAnimationFrame === 'function') {
        rafId = win.requestAnimationFrame(rafTick);
      }
    }

    function renderTransport(ts) {
      var t = ts || audio.getTransportState();
      if (dom.app) {
        dom.app.setAttribute('data-status', t.status);
        dom.app.setAttribute('data-loop', String(Boolean(t.loop)));
      }
      if (dom.btnPlayPause) {
        dom.btnPlayPause.setAttribute('aria-pressed', String(t.isPlaying));
        dom.btnPlayPause.setAttribute('aria-label', t.isPlaying ? 'Pause' : 'Play');
        var lbl = dom.btnPlayPause.querySelector('.btn-label');
        if (lbl) lbl.textContent = t.isPlaying ? 'Pause' : 'Play';
      }
      if (dom.btnLoop) {
        dom.btnLoop.setAttribute('aria-pressed', String(Boolean(t.loop)));
      }
      if (dom.masterVol && doc.activeElement !== dom.masterVol) {
        dom.masterVol.value = String(Math.round(clamp(t.masterVolume, 0, 1) * 100));
      }
      syncTransportPosition(t.currentBeat, false);
      if (t.isPlaying) {
        ensureRafLoop();
      } else {
        stopRafLoop();
        setMeterLevel(0);
      }
    }

    // --- Inspector & Bottom Pattern Desk Rendering Helpers ---

    function syncLengthBarsSelect(lengthBars) {
      if (!dom.lengthBarsSelect) return;
      var valStr = String(lengthBars);
      var optsList = dom.lengthBarsSelect.options;
      var found = false;
      for (var i = 0; i < optsList.length; i++) {
        if (optsList[i].value === valStr) { found = true; break; }
      }
      if (!found) {
        var values = BASE_BAR_OPTIONS.slice();
        if (values.indexOf(lengthBars) === -1) values.push(lengthBars);
        values.sort(function (a, b) { return a - b; });
        dom.lengthBarsSelect.textContent = '';
        for (var j = 0; j < values.length; j++) {
          var o = doc.createElement('option');
          o.value = String(values[j]);
          o.textContent = values[j] + ' bars';
          dom.lengthBarsSelect.appendChild(o);
        }
      }
      if (dom.lengthBarsSelect.value !== valStr) dom.lengthBarsSelect.value = valStr;
    }

    function syncPatternSelect(trackType, patternId) {
      if (!dom.clipPatternSelect) return;
      if (lastPatternTrackType !== trackType) {
        lastPatternTrackType = trackType;
        var pats = Model.getPatternsForTrackType(trackType);
        dom.clipPatternSelect.textContent = '';
        for (var i = 0; i < pats.length; i++) {
          var opt = doc.createElement('option');
          opt.value = pats[i].id;
          opt.textContent = pats[i].name;
          dom.clipPatternSelect.appendChild(opt);
        }
        var customOpt = doc.createElement('option');
        customOpt.value = 'custom';
        customOpt.textContent = 'Custom Pattern';
        dom.clipPatternSelect.appendChild(customOpt);
      }
      var hasVal = false;
      for (var j = 0; j < dom.clipPatternSelect.options.length; j++) {
        if (dom.clipPatternSelect.options[j].value === patternId) { hasVal = true; break; }
      }
      dom.clipPatternSelect.value = hasVal ? patternId : 'custom';
    }

    function getActiveStepPitch(clip, track) {
      var pitchOpts = buildPitchOptions(track.type, clip);
      var remembered = clipPitchMemory[clip.id];
      if (Number.isInteger(remembered)) {
        for (var i = 0; i < pitchOpts.length; i++) {
          if (pitchOpts[i].pitch === remembered) return remembered;
        }
      }
      var fallback = (clip.notes && clip.notes.length)
        ? clip.notes[0].pitch
        : (Model.TRACK_TYPE_META[track.type] ? Model.TRACK_TYPE_META[track.type].defaultPitch : 60);
      clipPitchMemory[clip.id] = fallback;
      return fallback;
    }

    function buildStepCellButton(stepIdx) {
      var btn = doc.createElement('button');
      btn.className = 'step-cell';
      btn.type = 'button';
      btn.setAttribute('data-step', String(stepIdx));
      if (stepIdx % 4 === 0) btn.setAttribute('data-downbeat', 'true');
      btn.setAttribute('aria-pressed', 'false');

      var top = doc.createElement('span');
      top.className = 'step-cell-top';
      var num = doc.createElement('span');
      num.className = 'step-num';
      num.textContent = String(stepIdx + 1);
      var dot = doc.createElement('span');
      dot.className = 'step-other-dot';
      dot.setAttribute('aria-hidden', 'true');
      top.appendChild(num);
      top.appendChild(dot);

      var well = doc.createElement('span');
      well.className = 'step-vel-well';
      well.setAttribute('aria-hidden', 'true');
      var bar = doc.createElement('span');
      bar.className = 'step-vel-bar';
      bar.style.setProperty('--vel', '0');
      well.appendChild(bar);

      var foot = doc.createElement('span');
      foot.className = 'step-cell-foot';
      foot.textContent = '\u00b7';

      btn.appendChild(top);
      btn.appendChild(well);
      btn.appendChild(foot);
      return btn;
    }

    function ensureStepGridStructure(stepsCount) {
      if (!dom.stepGrid) return [];
      var existing = dom.stepGrid.querySelectorAll('.step-cell');
      if (existing.length === stepsCount && existing.length > 0 && existing[0].querySelector('.step-vel-bar')) {
        return existing;
      }
      clearDeskPlayheadStep();
      dom.stepGrid.textContent = '';
      var frag = doc.createDocumentFragment();
      var groupCount = Math.ceil(stepsCount / 4);
      for (var g = 0; g < groupCount; g++) {
        var firstStep = g * 4;
        var lastStep = Math.min(stepsCount - 1, firstStep + 3);
        var groupEl = doc.createElement('div');
        groupEl.className = 'step-beat-group';
        groupEl.setAttribute('data-beat-group', String(g + 1));
        groupEl.setAttribute('role', 'group');
        groupEl.setAttribute('aria-label', 'Beat ' + (g + 1) + ', steps ' + (firstStep + 1) + ' to ' + (lastStep + 1));

        var hdr = doc.createElement('div');
        hdr.className = 'step-beat-header';
        var bLbl = doc.createElement('span');
        bLbl.className = 'step-beat-label';
        bLbl.textContent = 'Beat ' + (g + 1);
        var bSub = doc.createElement('span');
        bSub.className = 'step-beat-sub';
        bSub.textContent = (firstStep + 1) + '\u2013' + (lastStep + 1);
        hdr.appendChild(bLbl);
        hdr.appendChild(bSub);

        var cellsWrap = doc.createElement('div');
        cellsWrap.className = 'step-beat-cells';
        for (var s = firstStep; s <= lastStep; s++) {
          cellsWrap.appendChild(buildStepCellButton(s));
        }
        groupEl.appendChild(hdr);
        groupEl.appendChild(cellsWrap);
        frag.appendChild(groupEl);
      }
      dom.stepGrid.appendChild(frag);
      return dom.stepGrid.querySelectorAll('.step-cell');
    }

    function renderDeskNoteStrip(clip, track, activePitch) {
      if (!dom.deskNoteStrip) return;
      var notes = clip.notes || [];
      var counts = {};
      var pitches = [];
      var minVel = Infinity;
      var maxVel = -Infinity;

      for (var i = 0; i < notes.length; i++) {
        var n = notes[i];
        if (!counts[n.pitch]) {
          counts[n.pitch] = 0;
          pitches.push(n.pitch);
        }
        counts[n.pitch]++;
        if (n.velocity < minVel) minVel = n.velocity;
        if (n.velocity > maxVel) maxVel = n.velocity;
      }
      pitches.sort(function (a, b) { return a - b; });

      dom.deskNoteStrip.textContent = '';
      var voiceList = doc.createElement('div');
      voiceList.className = 'desk-voice-list';

      if (pitches.length === 0) {
        var emptyChip = doc.createElement('span');
        emptyChip.className = 'desk-vel-summary';
        emptyChip.textContent = 'No active notes in clip';
        voiceList.appendChild(emptyChip);
      } else {
        for (var p = 0; p < pitches.length; p++) {
          var pitch = pitches[p];
          var chip = doc.createElement('button');
          chip.className = 'voice-chip';
          chip.type = 'button';
          chip.setAttribute('data-pitch', String(pitch));
          chip.setAttribute('aria-pressed', String(pitch === activePitch));
          chip.textContent = pitchLabelForTrack(pitch, track.type) + ' ';
          var cnt = doc.createElement('span');
          cnt.className = 'voice-chip-count';
          cnt.textContent = '\u00d7' + counts[pitch];
          chip.appendChild(cnt);
          voiceList.appendChild(chip);
        }
      }

      var summary = doc.createElement('span');
      summary.className = 'desk-vel-summary';
      if (notes.length > 0) {
        var lo = Math.round(clamp(minVel, 0, 1) * 100);
        var hi = Math.round(clamp(maxVel, 0, 1) * 100);
        summary.textContent = 'Vel ' + (lo === hi ? hi + '%' : lo + '\u2013' + hi + '%') +
          ' \u00b7 ' + notes.length + (notes.length === 1 ? ' note' : ' notes') + ' \u00b7 1/16 grid';
      } else {
        summary.textContent = '1/16 grid';
      }

      dom.deskNoteStrip.appendChild(voiceList);
      dom.deskNoteStrip.appendChild(summary);
    }

    function renderStepEditor(clip, track, beatsPerBar) {
      if (dom.patternDesk && track) {
        dom.patternDesk.setAttribute('data-track-type', track.type);
      }
      if (dom.deskTrackBadge && track) {
        dom.deskTrackBadge.textContent = track.name;
        dom.deskTrackBadge.setAttribute('data-track-type', track.type);
      }

      if (!clip || !track) {
        clearDeskPlayheadStep();
        if (dom.patternDeskEmpty) dom.patternDeskEmpty.hidden = false;
        if (dom.patternDeskBody) dom.patternDeskBody.hidden = true;
        if (dom.deskClipName) dom.deskClipName.textContent = 'No clip selected';
        if (dom.deskClipMeta) dom.deskClipMeta.textContent = track ? track.name : '';
        if (dom.deskDensityReadout) dom.deskDensityReadout.textContent = '0 / 16 active';
        if (dom.stepPitchSelect) dom.stepPitchSelect.disabled = true;
        return;
      }

      if (dom.patternDeskEmpty) dom.patternDeskEmpty.hidden = true;
      if (dom.patternDeskBody) dom.patternDeskBody.hidden = false;
      if (dom.stepPitchSelect) dom.stepPitchSelect.disabled = false;

      if (dom.deskClipName) dom.deskClipName.textContent = clip.name;
      if (dom.deskClipMeta) {
        dom.deskClipMeta.textContent = Timeline.formatBeatPosition(clip.startBeat, beatsPerBar) +
          ' \u00b7 ' + Timeline.formatClipLength(clip.durationBeats, beatsPerBar);
      }

      if (!dom.stepPitchSelect || !dom.stepGrid) return;
      var pitchOpts = buildPitchOptions(track.type, clip);
      var activePitch = getActiveStepPitch(clip, track);
      var activePitchLabel = pitchLabelForTrack(activePitch, track.type);

      var sig = track.type + ':' + pitchOpts.map(function (p) { return p.pitch; }).join(',');
      if (sig !== lastPitchSig) {
        lastPitchSig = sig;
        dom.stepPitchSelect.textContent = '';
        for (var i = 0; i < pitchOpts.length; i++) {
          var o = doc.createElement('option');
          o.value = String(pitchOpts[i].pitch);
          o.textContent = pitchOpts[i].label;
          dom.stepPitchSelect.appendChild(o);
        }
      }
      if (dom.stepPitchSelect.value !== String(activePitch)) {
        dom.stepPitchSelect.value = String(activePitch);
      }

      var stepsCount = clamp(clip.patternSteps || 16, Model.LIMITS.MIN_PATTERN_STEPS, Model.LIMITS.MAX_PATTERN_STEPS);
      var activeByStep = {};
      var othersByStep = {};
      var notes = clip.notes || [];
      var activeVoiceHits = 0;
      for (var n = 0; n < notes.length; n++) {
        var nt = notes[n];
        if (nt.pitch === activePitch) {
          if (!activeByStep[nt.step]) activeVoiceHits++;
          activeByStep[nt.step] = nt;
        } else {
          if (!othersByStep[nt.step]) othersByStep[nt.step] = [];
          othersByStep[nt.step].push(nt);
        }
      }

      if (dom.deskDensityReadout) {
        dom.deskDensityReadout.textContent = activeVoiceHits + ' / ' + stepsCount + ' active';
      }

      var cells = ensureStepGridStructure(stepsCount);
      for (var c = 0; c < cells.length; c++) {
        var cell = cells[c];
        var stepIdx = Number(cell.getAttribute('data-step'));
        var beatGroup = Math.floor(stepIdx / 4) + 1;
        var activeNote = activeByStep[stepIdx] || null;
        var otherNotes = othersByStep[stepIdx] || null;
        var pressed = Boolean(activeNote);
        var hasOther = Boolean(otherNotes && otherNotes.length);

        cell.setAttribute('aria-pressed', String(pressed));
        cell.setAttribute('data-has-other', String(hasOther));

        var velBar = cell.querySelector('.step-vel-bar');
        var foot = cell.querySelector('.step-cell-foot');

        if (pressed) {
          var vel = clamp(activeNote.velocity, 0.05, 1);
          var velPct = Math.round(vel * 100);
          if (velBar) velBar.style.setProperty('--vel', String(r2(vel)));
          if (foot) foot.textContent = String(velPct);
          cell.setAttribute(
            'aria-label',
            'Step ' + (stepIdx + 1) + ', Beat ' + beatGroup + ', ' + activePitchLabel +
              ' active, velocity ' + velPct + '%' +
              (hasOther ? ', plus ' + otherNotes.length + ' other ' + (otherNotes.length === 1 ? 'voice' : 'voices') : '')
          );
        } else if (hasOther) {
          var maxOtherVel = 0;
          for (var k = 0; k < otherNotes.length; k++) {
            if (otherNotes[k].velocity > maxOtherVel) maxOtherVel = otherNotes[k].velocity;
          }
          if (velBar) velBar.style.setProperty('--vel', String(r2(clamp(maxOtherVel, 0.12, 1))));
          if (foot) {
            foot.textContent = otherNotes.length === 1
              ? midiToShortLabel(otherNotes[0].pitch, track.type)
              : '+' + otherNotes.length;
          }
          var otherTag = otherNotes.length === 1
            ? pitchLabelForTrack(otherNotes[0].pitch, track.type)
            : otherNotes.length + ' other voices';
          cell.setAttribute(
            'aria-label',
            'Step ' + (stepIdx + 1) + ', Beat ' + beatGroup + ', inactive; contains ' + otherTag
          );
        } else {
          if (velBar) velBar.style.setProperty('--vel', '0');
          if (foot) foot.textContent = '\u00b7';
          cell.setAttribute('aria-label', 'Step ' + (stepIdx + 1) + ', Beat ' + beatGroup + ', inactive');
        }
      }

      renderDeskNoteStrip(clip, track, activePitch);
    }

    function setInputIfSafe(el, value, force) {
      if (!el) return;
      if (!force && doc.activeElement === el) return;
      if (el.value !== value) el.value = value;
    }

    function renderAll(state, meta) {
      if (disposed || !state || !state.project) return;
      var p = state.project;
      var sel = state.selection || { trackId: null, clipId: null };
      var action = meta && meta.action;
      var forceInputs = action === 'undo' || action === 'redo' || action === 'importJSON' ||
        action === 'importProject' || action === 'resetToStarter';

      var totalBeats = Model.getTotalBeats(p);

      // Header & History Controls
      setInputIfSafe(dom.projectName, p.name, forceInputs);
      setInputIfSafe(dom.tempoInput, String(Math.round(p.tempo)), forceInputs);
      syncLengthBarsSelect(p.lengthBars);

      if (dom.btnUndo) {
        dom.btnUndo.disabled = !state.history.canUndo;
        dom.btnUndo.setAttribute('aria-disabled', String(!state.history.canUndo));
      }
      if (dom.btnRedo) {
        dom.btnRedo.disabled = !state.history.canRedo;
        dom.btnRedo.setAttribute('aria-disabled', String(!state.history.canRedo));
      }

      // Timeline View
      timeline.render(state);

      // Selected Track & Clip Inspector + Bottom Pattern Desk
      var track = sel.trackId && p.tracksById[sel.trackId]
        ? p.tracksById[sel.trackId]
        : (p.trackOrder.length ? p.tracksById[p.trackOrder[0]] : null);
      var clip = sel.clipId && p.clipsById[sel.clipId] ? p.clipsById[sel.clipId] : null;
      var clipTrack = clip && p.tracksById[clip.trackId] ? p.tracksById[clip.trackId] : track;

      var clipChanged = (clip ? clip.id : null) !== lastRenderedClipId;
      var trackChanged = (track ? track.id : null) !== lastRenderedTrackId;
      lastRenderedClipId = clip ? clip.id : null;
      lastRenderedTrackId = track ? track.id : null;

      // Clip Inspector Section
      if (dom.clipTrackBadge && clipTrack) {
        dom.clipTrackBadge.textContent = clipTrack.name;
        dom.clipTrackBadge.setAttribute('data-track-type', clipTrack.type);
      }
      if (dom.inspectorEmpty) dom.inspectorEmpty.hidden = Boolean(clip);
      if (dom.clipInspectorBody) dom.clipInspectorBody.hidden = !clip;

      if (clip && clipTrack) {
        setInputIfSafe(dom.clipNameInput, clip.name, forceInputs || clipChanged);
        if (dom.clipStartInput) {
          dom.clipStartInput.max = String(Math.max(0, totalBeats - Model.LIMITS.MIN_CLIP_BEATS));
          setInputIfSafe(dom.clipStartInput, String(clip.startBeat), forceInputs || clipChanged);
        }
        if (dom.clipDurationInput) {
          dom.clipDurationInput.max = String(totalBeats);
          setInputIfSafe(dom.clipDurationInput, String(clip.durationBeats), forceInputs || clipChanged);
        }
        syncPatternSelect(clipTrack.type, clip.patternId);
        var gainPct = Math.round(clamp(clip.gain, 0, 1) * 100);
        if (!isDraggingClipGain || forceInputs || clipChanged) {
          if (dom.clipGainInput) dom.clipGainInput.value = String(gainPct);
          if (dom.clipGainValue) dom.clipGainValue.textContent = gainPct + '%';
        }
      }

      // Bottom 16-Step Pattern Sequencer Desk
      renderStepEditor(clip, clipTrack, p.beatsPerBar);

      // Track Channel Inspector Section
      if (track) {
        var metaInfo = Model.TRACK_TYPE_META[track.type];
        if (dom.trackTypeBadge) {
          dom.trackTypeBadge.textContent = metaInfo ? metaInfo.label : track.type;
          dom.trackTypeBadge.setAttribute('data-track-type', track.type);
        }
        setInputIfSafe(dom.trackNameInput, track.name, forceInputs || trackChanged);
        if (dom.inspectorTrackMute) dom.inspectorTrackMute.setAttribute('aria-pressed', String(track.mute));
        if (dom.inspectorTrackSolo) dom.inspectorTrackSolo.setAttribute('aria-pressed', String(track.solo));

        var volPct = Math.round(clamp(track.volume, 0, 1) * 100);
        if (!isDraggingTrackVol || forceInputs || trackChanged) {
          if (dom.inspectorTrackVol) dom.inspectorTrackVol.value = String(volPct);
          if (dom.inspectorTrackVolVal) dom.inspectorTrackVolVal.textContent = volPct + '%';
        }
        var panPct = Math.round(clamp(track.pan || 0, -1, 1) * 100);
        if (!isDraggingTrackPan || forceInputs || trackChanged) {
          if (dom.inspectorTrackPan) dom.inspectorTrackPan.value = String(panPct);
          if (dom.inspectorTrackPanVal) dom.inspectorTrackPanVal.textContent = formatPanLabel(track.pan || 0);
        }
      }

      // Sync transport range bounds & playhead
      renderTransport(audio.getTransportState());
    }

    // --- Actions ---

    function togglePlayPause() {
      var ts = audio.getTransportState();
      if (ts.isPlaying) {
        audio.pause();
        setStatus('Paused at ' + formatTransportPos(audio.getPlayheadBeat(), store.getState().project.beatsPerBar), 'neutral');
      } else {
        var p = audio.play();
        if (p && typeof p.then === 'function') {
          p.then(function (res) {
            if (!disposed && res && !res.ok && !res.cancelled) {
              setStatus('Audio unavailable (' + (res.reason || 'blocked') + ')', 'error');
            }
          });
        }
      }
    }

    function stopTransport() {
      audio.stop();
      syncTransportPosition(0, true);
      setStatus('Stopped', 'neutral');
    }

    function toggleLoop() {
      var next = !audio.getTransportState().loop;
      audio.setLoop(next);
      setStatus(next ? 'Loop enabled' : 'Loop disabled', 'neutral');
    }

    function insertClipOnSelectedTrack() {
      var st = store.getState();
      var tid = st.selection.trackId || st.project.trackOrder[0];
      if (!tid) return;
      var beat = Model.quantizeBeat(audio.getPlayheadBeat());
      var res = store.createClip({ trackId: tid, startBeat: beat, durationBeats: Model.LIMITS.DEFAULT_CLIP_BEATS });
      if (!res.ok) {
        res = store.createClip({ trackId: tid, durationBeats: Model.LIMITS.DEFAULT_CLIP_BEATS });
      }
      if (res.ok) {
        var nextSt = store.getState();
        var tr = nextSt.project.tracksById[tid];
        setStatus('Added ' + res.clip.name + ' on ' + (tr ? tr.name : tid), 'ok');
        timeline.focusClip(res.clip.id);
      } else {
        setStatus('No open space on track (' + res.reason + ')', 'error');
      }
    }

    function performUndo() {
      var res = store.undo();
      if (res.ok) setStatus('Undid last change', 'neutral');
    }

    function performRedo() {
      var res = store.redo();
      if (res.ok) setStatus('Redid change', 'neutral');
    }

    function exportProjectFile() {
      var st = store.getState();
      var json = store.exportJSON();
      var filename = slugifyFilename(st.project.name);
      try {
        var blob = new Blob([json], { type: 'application/json' });
        var url = win.URL.createObjectURL(blob);
        var link = doc.createElement('a');
        link.href = url;
        link.download = filename;
        link.style.display = 'none';
        doc.body.appendChild(link);
        link.click();
        doc.body.removeChild(link);
        if (win && typeof win.setTimeout === 'function') {
          win.setTimeout(function () { try { win.URL.revokeObjectURL(url); } catch (_) {} }, 1000);
        }
        setStatus('Exported ' + filename, 'ok');
      } catch (err) {
        setStatus('Export failed: ' + (err && err.message ? err.message : 'unavailable'), 'error');
      }
    }

    function importProjectText(text, sourceLabel) {
      var res = store.importJSON(text);
      if (!res.ok) {
        var errs = Array.isArray(res.errors) && res.errors.length ? res.errors : ['Invalid project file'];
        setStatus('Import failed: ' + errs[0], 'error', {
          title: (sourceLabel ? sourceLabel + ': ' : '') + errs.join('; ')
        });
        return false;
      }
      audio.stop();
      clipPitchMemory = {};
      saveProjectToLocal(false);
      var st = store.getState();
      renderAll(st, { action: 'importJSON' });
      setStatus('Imported "' + st.project.name + '"', 'ok');
      return true;
    }

    function handleImportFile(file) {
      if (!file) return;
      if (typeof file.size === 'number' && file.size > Model.LIMITS.MAX_JSON_CHARS) {
        setStatus('Import failed: file exceeds 512 KiB limit', 'error');
        return;
      }
      if (typeof FileReader !== 'undefined') {
        var reader = new FileReader();
        reader.onload = function () {
          importProjectText(typeof reader.result === 'string' ? reader.result : '', file.name);
        };
        reader.onerror = function () {
          setStatus('Import failed: could not read file', 'error');
        };
        reader.readAsText(file);
      } else if (typeof file.text === 'function') {
        file.text().then(
          function (txt) { importProjectText(txt, file.name); },
          function () { setStatus('Import failed: could not read file', 'error'); }
        );
      } else {
        setStatus('Import unavailable in this browser', 'error');
      }
    }

    // --- Event Binding Helper ---

    var cleanups = [];
    function listen(el, event, handler, capture) {
      if (!el) return;
      el.addEventListener(event, handler, Boolean(capture));
      cleanups.push(function () { el.removeEventListener(event, handler, Boolean(capture)); });
    }

    function bindCommitInput(el, onCommit, getCurrentVal) {
      if (!el) return;
      listen(el, 'change', function () { onCommit(el.value); });
      listen(el, 'keydown', function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          onCommit(el.value);
          el.blur();
        } else if (e.key === 'Escape') {
          e.preventDefault();
          el.value = String(getCurrentVal());
          el.blur();
        }
      });
    }

    // --- Header & Transport Bindings ---

    bindCommitInput(dom.projectName, function (raw) {
      var res = store.setProjectName(raw);
      if (res.ok) {
        if (res.changed) setStatus('Project renamed to "' + res.name + '"', 'neutral');
      } else {
        dom.projectName.value = store.getState().project.name;
        setStatus('Project name cannot be empty', 'error');
      }
    }, function () { return store.getState().project.name; });

    listen(dom.btnPlayPause, 'click', togglePlayPause);
    listen(dom.btnStop, 'click', stopTransport);
    listen(dom.btnLoop, 'click', toggleLoop);

    // Transport Scrub Slider
    listen(dom.transportScrub, 'pointerdown', function () { isScrubbing = true; });
    listen(dom.transportScrub, 'input', function () {
      isScrubbing = true;
      var beat = Number(dom.transportScrub.value);
      if (Number.isFinite(beat)) {
        audio.seek(beat);
        syncTransportPosition(beat, false);
      }
    });
    function endScrub() {
      if (!isScrubbing) return;
      isScrubbing = false;
      var beat = Number(dom.transportScrub.value);
      if (Number.isFinite(beat)) {
        audio.seek(beat);
        syncTransportPosition(beat, false);
      }
    }
    listen(dom.transportScrub, 'change', endScrub);
    listen(dom.transportScrub, 'pointerup', endScrub);
    listen(dom.transportScrub, 'pointercancel', endScrub);
    listen(dom.transportScrub, 'blur', function () { isScrubbing = false; });

    // Tempo Controls
    function stepTempo(delta) {
      var cur = store.getState().project.tempo;
      var res = store.setTempo(Math.round(cur) + delta);
      if (res.ok && res.changed) setStatus('Tempo ' + Math.round(res.tempo) + ' BPM', 'neutral');
    }
    listen(dom.btnTempoDown, 'click', function (e) { stepTempo(e.shiftKey ? -5 : -1); });
    listen(dom.btnTempoUp, 'click', function (e) { stepTempo(e.shiftKey ? 5 : 1); });
    bindCommitInput(dom.tempoInput, function (raw) {
      var num = Number(raw);
      if (!Number.isFinite(num)) {
        dom.tempoInput.value = String(Math.round(store.getState().project.tempo));
        setStatus('Enter a valid BPM (40–240)', 'error');
        return;
      }
      var res = store.setTempo(Math.round(num));
      if (res.ok) {
        dom.tempoInput.value = String(Math.round(res.tempo));
        if (res.changed) setStatus('Tempo ' + Math.round(res.tempo) + ' BPM', 'neutral');
      }
    }, function () { return Math.round(store.getState().project.tempo); });

    // Master Volume
    listen(dom.masterVol, 'input', function () {
      var pct = clamp(Number(dom.masterVol.value) || 0, 0, 100);
      audio.setMasterVolume(pct / 100);
    });
    listen(dom.masterVol, 'change', function () {
      var pct = clamp(Number(dom.masterVol.value) || 0, 0, 100);
      audio.setMasterVolume(pct / 100);
      setStatus('Master output ' + Math.round(pct) + '%', 'neutral');
    });

    // Project Actions
    listen(dom.btnUndo, 'click', performUndo);
    listen(dom.btnRedo, 'click', performRedo);
    listen(dom.btnSaveLocal, 'click', function () { saveProjectToLocal(true); });
    listen(dom.btnResetProject, 'click', function () {
      audio.stop();
      clipPitchMemory = {};
      store.resetToStarter();
      saveProjectToLocal(false);
      setStatus('Reset to Midnight Relay (Undo to restore)', 'ok');
    });
    listen(dom.btnExport, 'click', exportProjectFile);
    listen(dom.btnImport, 'click', function () {
      if (dom.importFileInput) {
        dom.importFileInput.value = '';
        dom.importFileInput.click();
      }
    });
    listen(dom.importFileInput, 'change', function () {
      var file = dom.importFileInput.files && dom.importFileInput.files[0];
      if (file) handleImportFile(file);
      dom.importFileInput.value = '';
    });

    // Arrangement Toolbar
    listen(dom.btnAddClip, 'click', insertClipOnSelectedTrack);
    listen(dom.btnTrackAddClip, 'click', insertClipOnSelectedTrack);
    listen(dom.lengthBarsSelect, 'change', function () {
      var bars = parseInt(dom.lengthBarsSelect.value, 10);
      if (!Number.isInteger(bars)) return;
      var prevClipCount = Object.keys(store.getState().project.clipsById).length;
      var res = store.setLengthBars(bars, { truncate: true });
      if (res.ok && res.changed) {
        var nextClipCount = Object.keys(store.getState().project.clipsById).length;
        var trimmed = nextClipCount < prevClipCount;
        setStatus(
          'Length ' + bars + ' bars' + (trimmed ? ' (clips trimmed — Undo to restore)' : ''),
          'ok'
        );
      } else if (!res.ok) {
        syncLengthBarsSelect(store.getState().project.lengthBars);
        setStatus('Could not change length (' + res.reason + ')', 'error');
      }
    });

    // --- Clip Inspector & Bottom Pattern Desk Bindings ---

    function getSelectedClip() {
      var st = store.getState();
      return st.selection.clipId ? st.project.clipsById[st.selection.clipId] || null : null;
    }

    bindCommitInput(dom.clipNameInput, function (raw) {
      var cl = getSelectedClip();
      if (!cl) return;
      var res = store.renameClip(cl.id, raw);
      if (res.ok) {
        if (res.changed) setStatus('Renamed clip to "' + res.clip.name + '"', 'neutral');
      } else {
        dom.clipNameInput.value = cl.name;
        setStatus('Clip name cannot be empty', 'error');
      }
    }, function () { var c = getSelectedClip(); return c ? c.name : ''; });

    function nudgeClipStart(delta) {
      var cl = getSelectedClip();
      if (!cl) return;
      var res = store.moveClip(cl.id, { startBeat: cl.startBeat + delta });
      if (res.ok && res.changed) {
        timeline.ensureBeatVisible(res.clip.startBeat);
        setStatus('Moved ' + res.clip.name + ' to ' + Timeline.formatBeatPosition(res.clip.startBeat, store.getState().project.beatsPerBar), 'ok');
      } else if (!res.ok) {
        setStatus('Cannot move clip (' + res.reason + ')', 'error');
      }
    }
    listen(dom.btnClipNudgeLeft, 'click', function (e) { nudgeClipStart(e.shiftKey ? -0.25 : -1); });
    listen(dom.btnClipNudgeRight, 'click', function (e) { nudgeClipStart(e.shiftKey ? 0.25 : 1); });
    bindCommitInput(dom.clipStartInput, function (raw) {
      var cl = getSelectedClip();
      if (!cl) return;
      var num = Number(raw);
      if (!Number.isFinite(num)) { dom.clipStartInput.value = String(cl.startBeat); return; }
      var res = store.moveClip(cl.id, { startBeat: num });
      if (res.ok) {
        dom.clipStartInput.value = String(res.clip.startBeat);
        if (res.changed) timeline.ensureBeatVisible(res.clip.startBeat);
      } else {
        dom.clipStartInput.value = String(cl.startBeat);
        setStatus('Cannot move clip (' + res.reason + ')', 'error');
      }
    }, function () { var c = getSelectedClip(); return c ? c.startBeat : 0; });

    function stepClipDuration(delta) {
      var cl = getSelectedClip();
      if (!cl) return;
      var res = store.resizeClip(cl.id, { durationBeats: cl.durationBeats + delta });
      if (res.ok && res.changed) {
        setStatus('Resized ' + res.clip.name + ' to ' + Timeline.formatClipLength(res.clip.durationBeats, store.getState().project.beatsPerBar), 'ok');
      }
    }
    listen(dom.btnClipShorten, 'click', function (e) { stepClipDuration(e.shiftKey ? -0.25 : -1); });
    listen(dom.btnClipLengthen, 'click', function (e) { stepClipDuration(e.shiftKey ? 0.25 : 1); });
    bindCommitInput(dom.clipDurationInput, function (raw) {
      var cl = getSelectedClip();
      if (!cl) return;
      var num = Number(raw);
      if (!Number.isFinite(num)) { dom.clipDurationInput.value = String(cl.durationBeats); return; }
      var res = store.resizeClip(cl.id, { durationBeats: num });
      if (res.ok) dom.clipDurationInput.value = String(res.clip.durationBeats);
      else dom.clipDurationInput.value = String(cl.durationBeats);
    }, function () { var c = getSelectedClip(); return c ? c.durationBeats : 4; });

    listen(dom.clipPatternSelect, 'change', function () {
      var cl = getSelectedClip();
      if (!cl) return;
      var patId = dom.clipPatternSelect.value;
      if (!patId || patId === 'custom') return;
      var prevPitch = clipPitchMemory[cl.id];
      delete clipPitchMemory[cl.id];
      var res = store.setClipPattern(cl.id, patId);
      if (res.ok) {
        setStatus('Applied pattern "' + (Model.getPatternById(patId) ? Model.getPatternById(patId).name : patId) + '"', 'ok');
      } else if (prevPitch !== undefined) {
        clipPitchMemory[cl.id] = prevPitch;
      }
    });

    listen(dom.clipGainInput, 'input', function () {
      isDraggingClipGain = true;
      var pct = clamp(Math.round(Number(dom.clipGainInput.value) || 0), 0, 100);
      if (dom.clipGainValue) dom.clipGainValue.textContent = pct + '%';
    });
    listen(dom.clipGainInput, 'change', function () {
      isDraggingClipGain = false;
      var cl = getSelectedClip();
      if (!cl) return;
      var pct = clamp(Number(dom.clipGainInput.value) || 0, 0, 100);
      var res = store.setClipGain(cl.id, pct / 100);
      if (res.ok && res.changed) setStatus(res.clip.name + ' gain ' + Math.round(res.clip.gain * 100) + '%', 'neutral');
    });
    listen(dom.clipGainInput, 'pointercancel', function () { isDraggingClipGain = false; });
    listen(dom.clipGainInput, 'blur', function () { isDraggingClipGain = false; });

    function selectStepPitch(pitch, shouldAudition) {
      var cl = getSelectedClip();
      if (!cl || !Number.isInteger(pitch)) return;
      clipPitchMemory[cl.id] = pitch;
      var st = store.getState();
      var tr = st.project.tracksById[cl.trackId];
      if (tr) {
        renderStepEditor(cl, tr, st.project.beatsPerBar);
        if (shouldAudition) audio.auditionNote(tr.type, pitch, 0.82, 0.45);
      }
    }

    listen(dom.stepPitchSelect, 'change', function () {
      var pitch = parseInt(dom.stepPitchSelect.value, 10);
      if (Number.isInteger(pitch)) selectStepPitch(pitch, false);
    });

    listen(dom.deskNoteStrip, 'click', function (e) {
      var chip = e.target.closest ? e.target.closest('.voice-chip[data-pitch]') : null;
      if (!chip) return;
      var pitch = parseInt(chip.getAttribute('data-pitch'), 10);
      if (Number.isInteger(pitch)) selectStepPitch(pitch, true);
    });

    listen(dom.btnAuditionNote, 'click', function () {
      var cl = getSelectedClip();
      var st = store.getState();
      var tr = cl ? st.project.tracksById[cl.trackId] : (st.selection.trackId ? st.project.tracksById[st.selection.trackId] : null);
      if (!tr) return;
      var pitch = parseInt(dom.stepPitchSelect && dom.stepPitchSelect.value, 10);
      if (!Number.isInteger(pitch)) pitch = Model.TRACK_TYPE_META[tr.type] ? Model.TRACK_TYPE_META[tr.type].defaultPitch : 60;
      audio.auditionNote(tr.type, pitch, 0.85, 0.5);
    });

    listen(dom.stepGrid, 'click', function (e) {
      var btn = e.target.closest ? e.target.closest('.step-cell[data-step]') : null;
      if (!btn) return;
      var cl = getSelectedClip();
      if (!cl) return;
      var tr = store.getState().project.tracksById[cl.trackId];
      if (!tr) return;
      var step = parseInt(btn.getAttribute('data-step'), 10);
      var pitch = parseInt(dom.stepPitchSelect && dom.stepPitchSelect.value, 10);
      if (!Number.isInteger(pitch)) pitch = getActiveStepPitch(cl, tr);

      var hadBefore = cl.notes.some(function (n) { return n.step === step && n.pitch === pitch; });
      var durSteps = tr.type === 'pad' ? 4 : (tr.type === 'bass' ? 1.5 : (tr.type === 'hat' && pitch === 42 ? 0.5 : 1));
      var res = store.toggleClipNote(cl.id, { step: step, pitch: pitch, velocity: 0.85, durationSteps: durSteps });
      if (res.ok && !hadBefore) {
        audio.auditionNote(tr.type, pitch, 0.78, Math.max(0.25, durSteps * 0.25));
      }
    });

    // Keyboard navigation between 16-step sequencer pads (Space/Enter stay native button activation)
    listen(dom.stepGrid, 'keydown', function (e) {
      var btn = e.target.closest ? e.target.closest('.step-cell[data-step]') : null;
      if (!btn || e.metaKey || e.ctrlKey || e.altKey) return;
      var cells = dom.stepGrid.querySelectorAll('.step-cell[data-step]');
      if (!cells.length) return;
      var idx = -1;
      for (var i = 0; i < cells.length; i++) {
        if (cells[i] === btn) { idx = i; break; }
      }
      if (idx === -1) return;
      var nextIdx = -1;
      if (e.key === 'ArrowRight') nextIdx = Math.min(cells.length - 1, idx + 1);
      else if (e.key === 'ArrowLeft') nextIdx = Math.max(0, idx - 1);
      else if (e.key === 'Home') nextIdx = 0;
      else if (e.key === 'End') nextIdx = cells.length - 1;
      if (nextIdx !== -1) {
        e.preventDefault();
        cells[nextIdx].focus();
      }
    });

    listen(dom.btnClipDuplicate, 'click', function () {
      var cl = getSelectedClip();
      if (!cl) return;
      var res = store.duplicateClip(cl.id);
      if (res.ok) {
        setStatus('Duplicated ' + cl.name, 'ok');
        timeline.focusClip(res.clip.id);
      } else {
        setStatus('Cannot duplicate clip (' + res.reason + ')', 'error');
      }
    });

    listen(dom.btnClipDelete, 'click', function () {
      var cl = getSelectedClip();
      if (!cl) return;
      var res = store.deleteClip(cl.id);
      if (res.ok) setStatus('Deleted ' + cl.name, 'neutral');
    });

    // --- Track Channel Inspector Bindings ---

    function getSelectedTrack() {
      var st = store.getState();
      return st.selection.trackId ? st.project.tracksById[st.selection.trackId] || null : null;
    }

    bindCommitInput(dom.trackNameInput, function (raw) {
      var tr = getSelectedTrack();
      if (!tr) return;
      var res = store.renameTrack(tr.id, raw);
      if (res.ok) {
        if (res.changed) setStatus('Track renamed to "' + res.track.name + '"', 'neutral');
      } else {
        dom.trackNameInput.value = tr.name;
        setStatus('Track name cannot be empty', 'error');
      }
    }, function () { var t = getSelectedTrack(); return t ? t.name : ''; });

    listen(dom.inspectorTrackMute, 'click', function () {
      var tr = getSelectedTrack();
      if (!tr) return;
      var res = store.toggleTrackMute(tr.id);
      if (res.ok) setStatus((res.track.mute ? 'Muted ' : 'Unmuted ') + res.track.name, 'neutral');
    });

    listen(dom.inspectorTrackSolo, 'click', function () {
      var tr = getSelectedTrack();
      if (!tr) return;
      var res = store.toggleTrackSolo(tr.id);
      if (res.ok) setStatus((res.track.solo ? 'Soloed ' : 'Unsoloed ') + res.track.name, 'neutral');
    });

    listen(dom.inspectorTrackVol, 'input', function () {
      isDraggingTrackVol = true;
      var pct = clamp(Math.round(Number(dom.inspectorTrackVol.value) || 0), 0, 100);
      if (dom.inspectorTrackVolVal) dom.inspectorTrackVolVal.textContent = pct + '%';
    });
    listen(dom.inspectorTrackVol, 'change', function () {
      isDraggingTrackVol = false;
      var tr = getSelectedTrack();
      if (!tr) return;
      var pct = clamp(Number(dom.inspectorTrackVol.value) || 0, 0, 100);
      var res = store.setTrackVolume(tr.id, pct / 100);
      if (res.ok && res.changed) setStatus(res.track.name + ' volume ' + Math.round(res.track.volume * 100) + '%', 'neutral');
    });
    listen(dom.inspectorTrackVol, 'pointercancel', function () { isDraggingTrackVol = false; });
    listen(dom.inspectorTrackVol, 'blur', function () { isDraggingTrackVol = false; });

    listen(dom.inspectorTrackPan, 'input', function () {
      isDraggingTrackPan = true;
      var pan = clamp((Number(dom.inspectorTrackPan.value) || 0) / 100, -1, 1);
      if (dom.inspectorTrackPanVal) dom.inspectorTrackPanVal.textContent = formatPanLabel(pan);
    });
    listen(dom.inspectorTrackPan, 'change', function () {
      isDraggingTrackPan = false;
      var tr = getSelectedTrack();
      if (!tr) return;
      var pan = clamp((Number(dom.inspectorTrackPan.value) || 0) / 100, -1, 1);
      var res = store.setTrackPan(tr.id, pan);
      if (res.ok && res.changed) setStatus(res.track.name + ' pan ' + formatPanLabel(res.track.pan), 'neutral');
    });
    listen(dom.inspectorTrackPan, 'pointercancel', function () { isDraggingTrackPan = false; });
    listen(dom.inspectorTrackPan, 'blur', function () { isDraggingTrackPan = false; });

    // --- Global Workspace Keyboard Shortcuts (Bubble Phase, Native-Control Safe) ---

    listen(win, 'keydown', function (e) {
      if (disposed || e.defaultPrevented) return;
      var target = e.target;
      var textEditable = isTextEditable(target);
      var mod = e.metaKey || e.ctrlKey;
      var key = e.key;

      // Cmd/Ctrl+S: Save locally
      if (mod && !e.altKey && (key === 's' || key === 'S')) {
        e.preventDefault();
        saveProjectToLocal(true);
        return;
      }

      // Cmd/Ctrl+Z (Undo) & Cmd/Ctrl+Shift+Z / Ctrl+Y (Redo) outside text inputs & active drags
      if (mod && !e.altKey && !textEditable && !timeline.isInteracting()) {
        if ((key === 'z' || key === 'Z') && !e.shiftKey) {
          e.preventDefault();
          performUndo();
          return;
        }
        if (((key === 'z' || key === 'Z') && e.shiftKey) || (!e.metaKey && (key === 'y' || key === 'Y'))) {
          e.preventDefault();
          performRedo();
          return;
        }
      }

      if (mod || e.altKey) return;

      // Never intercept native range, select, editable, or ARIA form/widget controls
      var isTimelineRuler = Boolean(target && target.id === 'timeline-ruler');
      if (isNativeFormOrWidgetControl(target) && !isTimelineRuler) return;

      // Space: Play/Pause (Shift+Space: Stop). Focused native buttons/links keep native Space activation & keyup.
      if (key === ' ' || e.code === 'Space') {
        if (isNativeActivatable(target)) return;
        e.preventDefault();
        if (e.shiftKey) stopTransport();
        else togglePlayPause();
        return;
      }

      // L: Toggle arrangement loop
      if (key === 'l' || key === 'L') {
        e.preventDefault();
        toggleLoop();
        return;
      }

      // Home: Seek to bar 1 (ruler and step-grid handle their own Home via e.defaultPrevented)
      if (key === 'Home') {
        e.preventDefault();
        audio.seek(0);
        syncTransportPosition(0, true);
        setStatus('Playhead at Bar 1, Beat 1', 'neutral');
      }
    }, false);

    // --- Store & Audio Subscriptions ---

    var unsubStore = store.subscribe(function (nextState, meta) {
      renderAll(nextState, meta);
      var act = meta && meta.action;
      if (act && act !== 'selectTrack' && act !== 'selectClip') {
        scheduleAutoSave();
      }
    });

    var unsubAudio = audio.subscribe(function (ts, meta) {
      renderTransport(ts);
      if (meta && meta.event === 'ended') {
        setStatus('Playback reached end of arrangement', 'neutral');
      }
    });

    listen(win, 'beforeunload', function () {
      if (autoSaveTimer !== null) {
        if (typeof win.clearTimeout === 'function') win.clearTimeout(autoSaveTimer);
        autoSaveTimer = null;
        saveProjectToLocal(false);
      }
    });

    // Initial render
    renderAll(store.getState(), { action: 'init' });
    setStatus(bootStatus.message, bootStatus.tone, bootStatus.title ? { title: bootStatus.title } : null);

    function dispose() {
      if (disposed) return;
      disposed = true;
      stopRafLoop();
      clearDeskPlayheadStep();
      if (statusTimer !== null && win && typeof win.clearTimeout === 'function') win.clearTimeout(statusTimer);
      if (autoSaveTimer !== null && win && typeof win.clearTimeout === 'function') win.clearTimeout(autoSaveTimer);
      if (unsubStore) unsubStore();
      if (unsubAudio) unsubAudio();
      for (var i = 0; i < cleanups.length; i++) cleanups[i]();
      cleanups = [];
      timeline.dispose();
      audio.dispose();
    }

    return {
      store: store,
      audio: audio,
      timeline: timeline,
      setStatus: setStatus,
      saveLocal: function () { return saveProjectToLocal(true); },
      importJSON: function (text) { return importProjectText(text, 'JSON'); },
      exportJSON: function () { return store.exportJSON(); },
      dispose: dispose
    };
  }

  var activeInstance = null;

  function boot(options) {
    if (activeInstance) return activeInstance;
    activeInstance = createApp(options);
    return activeInstance;
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function () { boot(); }, { once: true });
    } else {
      boot();
    }
  }

  return Object.freeze({
    STORAGE_KEY: STORAGE_KEY,
    formatTransportPos: formatTransportPos,
    formatPanLabel: formatPanLabel,
    createApp: createApp,
    boot: boot,
    getInstance: function () { return activeInstance; }
  });
});
