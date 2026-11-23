/**
 * Cue Room — Deterministic Web Audio Synthesis & Lookahead Scheduler
 * Dual-environment UMD module: browser global `CueAudio` + Node CommonJS.
 *
 * ARCHITECTURE:
 * 1. Pure time-mapping & scheduling planner (hardware-free, deterministic):
 *    - midiToFreq, beatsToSeconds, secondsToBeats, clampTempo
 *    - computeBeatAtTime, computeTimeForBeat, reanchorForTempo
 *    - planScheduleWindow(project, params)
 *    - createDeterministicNoiseSamples(length, seed)
 * 2. Web Audio Instrument & Transport Engine (`createEngine`):
 *    - Master headroom chain: TrackBus (Gain + StereoPanner) -> Headroom (0.72)
 *      -> DynamicsCompressor -> MasterGain -> Analyser -> Destination
 *    - Real synthesized voices for all 6 CueModel track types:
 *      kick, snare (snare + clap), hat (closed + open), bass, synth, pad
 *    - Anchor-based lookahead scheduling robust to live tempo changes, arrangement
 *      edits, mute/solo/volume changes, seek, pause/resume, loop, and end-of-song.
 *    - Strict voice registry & runId tokens prevent orphaned sources and duplicate
 *      scheduling on rapid repeated play or stop-during-resume.
 */
(function (root, factory) {
  'use strict';
  var modelDep = null;
  if (typeof module !== 'undefined' && module.exports) {
    try { modelDep = require('./model.js'); } catch (_) { modelDep = null; }
  } else if (root && root.CueModel) {
    modelDep = root.CueModel;
  }
  var api = factory(modelDep, root);
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.CueAudio = api;
})(typeof globalThis !== 'undefined' ? globalThis : typeof window !== 'undefined' ? window : this, function (defaultModel, rootObj) {
  'use strict';

  var EPS = 1e-6;
  var DEFAULTS = Object.freeze({
    LOOKAHEAD_SEC: 0.12,
    TICK_INTERVAL_MS: 25,
    START_OFFSET_SEC: 0.015,
    MASTER_VOLUME: 0.85,
    HEADROOM_GAIN: 0.72,
    MIN_TEMPO: 40,
    MAX_TEMPO: 240
  });

  // --------------------------------------------------------------------------
  // Pure Time Mapping, Tempo Anchoring & Lookahead Planner
  // --------------------------------------------------------------------------

  function isFiniteNum(v) { return typeof v === 'number' && Number.isFinite(v); }
  function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

  function resolveProject(raw, model) {
    if (raw && Array.isArray(raw.tracks) && !raw.tracksById && model && typeof model.validateProject === 'function') {
      var res = model.validateProject(raw);
      if (!res.ok) throw new Error('Invalid project: ' + res.errors.join('; '));
      return res.project;
    }
    return raw;
  }

  function clampTempo(tempo) {
    if (!isFiniteNum(tempo)) return 124;
    return clamp(tempo, DEFAULTS.MIN_TEMPO, DEFAULTS.MAX_TEMPO);
  }

  function midiToFreq(midi) { return 440 * Math.pow(2, (midi - 69) / 12); }
  function beatsToSeconds(beats, tempo) { return (beats * 60) / clampTempo(tempo); }
  function secondsToBeats(seconds, tempo) { return (seconds * clampTempo(tempo)) / 60; }

  function wrapBeat(beat, totalBeats) {
    if (totalBeats <= 0) return 0;
    var m = beat % totalBeats;
    return m < 0 ? m + totalBeats : m;
  }

  function computeBeatAtTime(anchor, nowTime, totalBeats, loop) {
    var elapsedSec = Math.max(0, nowTime - anchor.anchorTime);
    var continuousBeat = anchor.anchorBeat + secondsToBeats(elapsedSec, anchor.tempo);
    if (loop) {
      return { continuousBeat: continuousBeat, displayBeat: wrapBeat(continuousBeat, totalBeats), reachedEnd: false };
    }
    var reachedEnd = continuousBeat >= totalBeats - EPS;
    var clamped = Math.min(totalBeats, Math.max(0, continuousBeat));
    return { continuousBeat: clamped, displayBeat: clamped, reachedEnd: reachedEnd };
  }

  function computeTimeForBeat(anchor, targetContinuousBeat) {
    return anchor.anchorTime + beatsToSeconds(targetContinuousBeat - anchor.anchorBeat, anchor.tempo);
  }

  function reanchorForTempo(anchor, nowTime, newTempo, totalBeats, loop) {
    var safeTempo = clampTempo(newTempo);
    if (nowTime <= anchor.anchorTime) {
      return { anchorTime: anchor.anchorTime, anchorBeat: anchor.anchorBeat, tempo: safeTempo };
    }
    var pos = computeBeatAtTime(anchor, nowTime, totalBeats, loop);
    return { anchorTime: nowTime, anchorBeat: pos.continuousBeat, tempo: safeTempo };
  }

  function planScheduleWindow(project, params, modelOverride) {
    var model = modelOverride || defaultModel || (rootObj && rootObj.CueModel);
    if (!model) throw new Error('CueModel is required for CueAudio scheduling');
    project = resolveProject(project, model);

    var totalBeats = model.getTotalBeats(project);
    var loop = Boolean(params.loop);
    var includeMuted = Boolean(params.includeMuted);
    var anchor = params.anchor;
    var tempo = clampTempo(anchor ? anchor.tempo : project.tempo);
    var winStart = isFiniteNum(params.windowStartBeat) ? params.windowStartBeat : 0;
    var winEnd = isFiniteNum(params.windowEndBeat) ? params.windowEndBeat : winStart;

    if (totalBeats <= 0 || winEnd <= winStart + EPS) {
      return { events: [], nextCursorBeat: winStart, reachedEnd: !loop && winStart >= totalBeats - EPS };
    }

    var scheduled = [];

    if (!loop) {
      if (winStart >= totalBeats - EPS) {
        return { events: [], nextCursorBeat: totalBeats, reachedEnd: true };
      }
      var clampedEnd = Math.min(winEnd, totalBeats);
      var raw = model.expandProjectEvents(project, {
        fromBeat: Math.max(0, winStart),
        toBeat: clampedEnd,
        includeMuted: includeMuted
      });
      for (var i = 0; i < raw.length; i++) {
        var ev = raw[i];
        var startTime = computeTimeForBeat(anchor, ev.beat);
        var durationSec = beatsToSeconds(ev.durationBeats, tempo);
        scheduled.push({
          trackId: ev.trackId, trackType: ev.trackType, clipId: ev.clipId,
          localBeat: ev.beat, continuousBeat: ev.beat, durationBeats: ev.durationBeats,
          pitch: ev.pitch, velocity: ev.velocity, trackVolume: ev.trackVolume, trackPan: ev.trackPan,
          startTime: startTime, durationSec: durationSec, endTime: startTime + durationSec
        });
      }
      return { events: scheduled, nextCursorBeat: clampedEnd, reachedEnd: winEnd >= totalBeats - EPS };
    }

    var segStart = Math.max(0, winStart);
    var guard = 0;
    while (segStart < winEnd - EPS && guard < 16) {
      guard++;
      var cycleIndex = Math.floor((segStart + EPS) / totalBeats);
      var cycleBase = cycleIndex * totalBeats;
      var segEnd = Math.min(winEnd, cycleBase + totalBeats);
      var localFrom = Math.max(0, segStart - cycleBase);
      var localTo = Math.min(totalBeats, segEnd - cycleBase);

      if (localTo > localFrom + EPS) {
        var slice = model.expandProjectEvents(project, {
          fromBeat: localFrom, toBeat: localTo, includeMuted: includeMuted
        });
        for (var j = 0; j < slice.length; j++) {
          var item = slice[j];
          var contBeat = cycleBase + item.beat;
          var st = computeTimeForBeat(anchor, contBeat);
          var durSec = beatsToSeconds(item.durationBeats, tempo);
          scheduled.push({
            trackId: item.trackId, trackType: item.trackType, clipId: item.clipId,
            localBeat: item.beat, continuousBeat: contBeat, durationBeats: item.durationBeats,
            pitch: item.pitch, velocity: item.velocity, trackVolume: item.trackVolume, trackPan: item.trackPan,
            startTime: st, durationSec: durSec, endTime: st + durSec
          });
        }
      }
      segStart = segEnd;
    }

    return { events: scheduled, nextCursorBeat: winEnd, reachedEnd: false };
  }

  function createDeterministicNoiseSamples(length, seed) {
    var out = new Float32Array(length);
    var state = (seed === undefined ? 0x51a7c3d1 : seed) >>> 0;
    for (var i = 0; i < length; i++) {
      state = (Math.imul(1664525, state) + 1013904223) >>> 0;
      out[i] = (state / 2147483648) - 1;
    }
    return out;
  }

  // --------------------------------------------------------------------------
  // Web Audio Synthesis Primitives
  // --------------------------------------------------------------------------

  function safeStopNode(node, when) {
    if (!node) return;
    try { node.stop(Math.max(0, when)); } catch (_) {}
  }

  function safeDisconnect(node) {
    if (!node) return;
    try { node.disconnect(); } catch (_) {}
  }

  function makeVoiceHandle(meta, envGain, sources, nodes) {
    var stopped = false;
    var cleaned = false;
    function cleanup() {
      stopped = true;
      if (cleaned) return;
      cleaned = true;
      for (var i = 0; i < nodes.length; i++) safeDisconnect(nodes[i]);
    }
    if (sources && sources[0]) {
      try { sources[0].onended = cleanup; } catch (_) {}
    }
    return {
      id: meta.id,
      trackId: meta.trackId,
      continuousBeat: meta.continuousBeat,
      startTime: meta.startTime,
      endTime: meta.endTime,
      isStopped: function () { return stopped; },
      stop: function (nowTime) {
        if (stopped) return;
        stopped = true;
        var t = Math.max(0, isFiniteNum(nowTime) ? nowTime : 0);
        if (envGain && envGain.gain) {
          try {
            if (typeof envGain.gain.cancelScheduledValues === 'function') {
              envGain.gain.cancelScheduledValues(t);
            }
            if (t < meta.startTime) {
              envGain.gain.setValueAtTime(0, t);
            } else {
              envGain.gain.setValueAtTime(Math.max(0.0001, envGain.gain.value || 0.0001), t);
              envGain.gain.linearRampToValueAtTime(0, t + 0.008);
            }
          } catch (_) {}
        }
        var stopAt = t < meta.startTime ? meta.startTime : t + 0.01;
        for (var i = 0; i < sources.length; i++) safeStopNode(sources[i], stopAt);
        if (t < meta.startTime) cleanup();
      },
      cleanup: cleanup
    };
  }

  function triggerKickVoice(ctx, dest, ev, meta) {
    var t = ev.startTime;
    var vel = clamp(ev.velocity, 0.05, 1);
    var decay = 0.28;
    var stopAt = t + decay + 0.02;

    var outGain = ctx.createGain();
    outGain.gain.setValueAtTime(1, t);
    outGain.connect(dest);

    var env = ctx.createGain();
    env.gain.setValueAtTime(0.0001, t);
    env.gain.linearRampToValueAtTime(0.95 * vel, t + 0.003);
    env.gain.exponentialRampToValueAtTime(0.0008, t + decay);
    env.connect(outGain);

    var body = ctx.createOscillator();
    body.type = 'sine';
    body.frequency.setValueAtTime(148, t);
    body.frequency.exponentialRampToValueAtTime(48, t + 0.055);
    body.frequency.exponentialRampToValueAtTime(41, t + decay);
    body.connect(env);

    var clickGain = ctx.createGain();
    clickGain.gain.setValueAtTime(0.32 * vel, t);
    clickGain.gain.exponentialRampToValueAtTime(0.0008, t + 0.018);
    clickGain.connect(outGain);

    var click = ctx.createOscillator();
    click.type = 'triangle';
    click.frequency.setValueAtTime(220, t);
    click.frequency.exponentialRampToValueAtTime(65, t + 0.016);
    click.connect(clickGain);

    body.start(t);
    body.stop(stopAt);
    click.start(t);
    click.stop(t + 0.022);

    meta.endTime = stopAt;
    return makeVoiceHandle(meta, outGain, [body, click], [body, click, clickGain, env, outGain]);
  }

  function triggerSnareVoice(ctx, dest, ev, meta, noiseBuffer) {
    var t = ev.startTime;
    var vel = clamp(ev.velocity, 0.05, 1);
    var isClap = ev.pitch === 39;
    var tail = isClap ? 0.19 : 0.17;
    var stopAt = t + tail + 0.02;

    var outGain = ctx.createGain();
    outGain.gain.setValueAtTime(1, t);
    outGain.connect(dest);

    var noise = ctx.createBufferSource();
    noise.buffer = noiseBuffer;

    var filter = ctx.createBiquadFilter();
    filter.type = isClap ? 'bandpass' : 'highpass';
    filter.frequency.setValueAtTime(isClap ? 1350 : 1650, t);
    if (filter.Q) filter.Q.setValueAtTime(isClap ? 1.15 : 0.7, t);

    var noiseEnv = ctx.createGain();
    if (isClap) {
      noiseEnv.gain.setValueAtTime(0.0001, t);
      noiseEnv.gain.linearRampToValueAtTime(0.7 * vel, t + 0.002);
      noiseEnv.gain.exponentialRampToValueAtTime(0.18 * vel, t + 0.011);
      noiseEnv.gain.linearRampToValueAtTime(0.75 * vel, t + 0.013);
      noiseEnv.gain.exponentialRampToValueAtTime(0.2 * vel, t + 0.023);
      noiseEnv.gain.linearRampToValueAtTime(0.8 * vel, t + 0.026);
      noiseEnv.gain.exponentialRampToValueAtTime(0.0008, t + tail);
    } else {
      noiseEnv.gain.setValueAtTime(0.0001, t);
      noiseEnv.gain.linearRampToValueAtTime(0.68 * vel, t + 0.002);
      noiseEnv.gain.exponentialRampToValueAtTime(0.0008, t + tail);
    }

    noise.connect(filter);
    filter.connect(noiseEnv);
    noiseEnv.connect(outGain);
    noise.start(t);
    noise.stop(stopAt);

    var sources = [noise];
    var nodes = [noise, filter, noiseEnv, outGain];

    if (!isClap) {
      var body = ctx.createOscillator();
      body.type = 'triangle';
      body.frequency.setValueAtTime(215, t);
      body.frequency.exponentialRampToValueAtTime(128, t + 0.055);

      var bodyEnv = ctx.createGain();
      bodyEnv.gain.setValueAtTime(0.0001, t);
      bodyEnv.gain.linearRampToValueAtTime(0.55 * vel, t + 0.002);
      bodyEnv.gain.exponentialRampToValueAtTime(0.0008, t + 0.11);

      body.connect(bodyEnv);
      bodyEnv.connect(outGain);
      body.start(t);
      body.stop(t + 0.125);
      sources.push(body);
      nodes.push(body, bodyEnv);
    }

    meta.endTime = stopAt;
    return makeVoiceHandle(meta, outGain, sources, nodes);
  }

  function triggerHatVoice(ctx, dest, ev, meta, noiseBuffer) {
    var t = ev.startTime;
    var vel = clamp(ev.velocity, 0.05, 1);
    var isOpen = ev.pitch === 46 || ev.durationBeats >= 0.9;
    var decay = isOpen ? clamp(ev.durationSec * 0.9, 0.16, 0.38) : 0.05;
    var stopAt = t + decay + 0.015;

    var noise = ctx.createBufferSource();
    noise.buffer = noiseBuffer;

    var hp = ctx.createBiquadFilter();
    hp.type = 'highpass';
    hp.frequency.setValueAtTime(isOpen ? 6800 : 7800, t);

    var bp = ctx.createBiquadFilter();
    bp.type = 'bandpass';
    bp.frequency.setValueAtTime(9800, t);
    if (bp.Q) bp.Q.setValueAtTime(0.85, t);

    var env = ctx.createGain();
    env.gain.setValueAtTime(0.0001, t);
    env.gain.linearRampToValueAtTime((isOpen ? 0.42 : 0.36) * vel, t + 0.002);
    env.gain.exponentialRampToValueAtTime(0.0008, t + decay);

    noise.connect(hp);
    hp.connect(bp);
    bp.connect(env);
    env.connect(dest);

    noise.start(t);
    noise.stop(stopAt);

    meta.endTime = stopAt;
    return makeVoiceHandle(meta, env, [noise], [noise, hp, bp, env]);
  }

  function triggerBassVoice(ctx, dest, ev, meta) {
    var t = ev.startTime;
    var vel = clamp(ev.velocity, 0.05, 1);
    var freq = midiToFreq(ev.pitch);
    var gate = Math.max(0.04, ev.durationSec);
    var release = 0.065;
    var stopAt = t + gate + release + 0.015;

    var filter = ctx.createBiquadFilter();
    filter.type = 'lowpass';
    var peakCut = clamp(freq * 9 + vel * 900, 420, 2600);
    var sustainCut = clamp(freq * 3.2, 160, 720);
    filter.frequency.setValueAtTime(peakCut, t);
    filter.frequency.exponentialRampToValueAtTime(sustainCut, t + Math.min(0.13, gate));
    if (filter.Q) filter.Q.setValueAtTime(3.0, t);

    var env = ctx.createGain();
    var peakGain = 0.56 * vel;
    env.gain.setValueAtTime(0.0001, t);
    env.gain.linearRampToValueAtTime(peakGain, t + 0.005);
    env.gain.exponentialRampToValueAtTime(Math.max(0.001, peakGain * 0.68), t + Math.min(0.09, gate));
    env.gain.setValueAtTime(Math.max(0.001, peakGain * 0.68), t + gate);
    env.gain.exponentialRampToValueAtTime(0.0008, t + gate + release);

    var saw = ctx.createOscillator();
    saw.type = 'sawtooth';
    saw.frequency.setValueAtTime(freq, t);

    var sub = ctx.createOscillator();
    sub.type = 'triangle';
    sub.frequency.setValueAtTime(freq, t);
    if (sub.detune) sub.detune.setValueAtTime(4, t);

    var subGain = ctx.createGain();
    subGain.gain.setValueAtTime(0.55, t);

    saw.connect(filter);
    sub.connect(subGain);
    subGain.connect(filter);
    filter.connect(env);
    env.connect(dest);

    saw.start(t);
    sub.start(t);
    saw.stop(stopAt);
    sub.stop(stopAt);

    meta.endTime = stopAt;
    return makeVoiceHandle(meta, env, [saw, sub], [saw, sub, subGain, filter, env]);
  }

  function triggerSynthVoice(ctx, dest, ev, meta, isPad) {
    var t = ev.startTime;
    var vel = clamp(ev.velocity, 0.05, 1);
    var freq = midiToFreq(ev.pitch);
    var gate = Math.max(0.05, ev.durationSec);
    var attack = isPad ? Math.min(0.09, gate * 0.35) : 0.008;
    var release = isPad ? 0.28 : 0.14;
    var stopAt = t + gate + release + 0.02;

    var filter = ctx.createBiquadFilter();
    filter.type = 'lowpass';
    var startCut = isPad ? 1600 : clamp(1800 + vel * 2000, 1200, 4400);
    var endCut = isPad ? 950 : clamp(900 + vel * 800, 700, 2200);
    filter.frequency.setValueAtTime(startCut, t);
    filter.frequency.exponentialRampToValueAtTime(endCut, t + Math.min(0.2, gate + release));
    if (filter.Q) filter.Q.setValueAtTime(isPad ? 0.8 : 1.4, t);

    var env = ctx.createGain();
    var peak = (isPad ? 0.24 : 0.28) * vel;
    env.gain.setValueAtTime(0.0001, t);
    env.gain.linearRampToValueAtTime(peak, t + attack);
    env.gain.exponentialRampToValueAtTime(Math.max(0.001, peak * (isPad ? 0.8 : 0.62)), t + Math.min(attack + 0.12, gate));
    env.gain.setValueAtTime(Math.max(0.001, peak * (isPad ? 0.8 : 0.62)), t + gate);
    env.gain.exponentialRampToValueAtTime(0.0008, t + gate + release);

    var oscA = ctx.createOscillator();
    oscA.type = isPad ? 'triangle' : 'sawtooth';
    oscA.frequency.setValueAtTime(freq, t);
    if (oscA.detune) oscA.detune.setValueAtTime(isPad ? -9 : -7, t);

    var oscB = ctx.createOscillator();
    oscB.type = isPad ? 'sawtooth' : 'triangle';
    oscB.frequency.setValueAtTime(freq, t);
    if (oscB.detune) oscB.detune.setValueAtTime(isPad ? 9 : 7, t);

    var mixB = ctx.createGain();
    mixB.gain.setValueAtTime(isPad ? 0.45 : 0.7, t);

    oscA.connect(filter);
    oscB.connect(mixB);
    mixB.connect(filter);
    filter.connect(env);
    env.connect(dest);

    oscA.start(t);
    oscB.start(t);
    oscA.stop(stopAt);
    oscB.stop(stopAt);

    meta.endTime = stopAt;
    return makeVoiceHandle(meta, env, [oscA, oscB], [oscA, oscB, mixB, filter, env]);
  }

  function triggerEventVoice(ctx, dest, ev, voiceId, noiseBuffer) {
    var meta = {
      id: voiceId, trackId: ev.trackId, continuousBeat: ev.continuousBeat,
      startTime: ev.startTime, endTime: ev.endTime
    };
    switch (ev.trackType) {
      case 'kick': return triggerKickVoice(ctx, dest, ev, meta);
      case 'snare': return triggerSnareVoice(ctx, dest, ev, meta, noiseBuffer);
      case 'hat': return triggerHatVoice(ctx, dest, ev, meta, noiseBuffer);
      case 'bass': return triggerBassVoice(ctx, dest, ev, meta);
      case 'pad': return triggerSynthVoice(ctx, dest, ev, meta, true);
      case 'synth':
      default: return triggerSynthVoice(ctx, dest, ev, meta, false);
    }
  }

  // --------------------------------------------------------------------------
  // Audio Engine Factory
  // --------------------------------------------------------------------------

  function createEngine(options) {
    var opts = options || {};
    var model = opts.model || defaultModel || (rootObj && rootObj.CueModel);
    if (!model) throw new Error('CueModel is required to create CueAudio engine');

    var store = opts.store || null;
    var project = store ? store.getState().project : (opts.project ? resolveProject(opts.project, model) : model.createStarterProject());
    var lookaheadSec = isFiniteNum(opts.lookaheadSec) && opts.lookaheadSec > 0 ? opts.lookaheadSec : DEFAULTS.LOOKAHEAD_SEC;
    var tickIntervalMs = isFiniteNum(opts.tickIntervalMs) && opts.tickIntervalMs > 0 ? opts.tickIntervalMs : DEFAULTS.TICK_INTERVAL_MS;
    var startOffsetSec = isFiniteNum(opts.startOffsetSec) && opts.startOffsetSec >= 0 ? opts.startOffsetSec : DEFAULTS.START_OFFSET_SEC;

    var setIntervalFn = opts.setInterval || (typeof setInterval === 'function' ? setInterval : null);
    var clearIntervalFn = opts.clearInterval || (typeof clearInterval === 'function' ? clearInterval : null);

    var ctx = opts.audioContext || null;
    var ownsContext = !opts.audioContext;
    var graphReady = false;
    var masterPreGain = null, masterCompressor = null, masterGain = null, masterAnalyser = null, analyserBuf = null;
    var noiseBuffer = null;
    var trackBuses = {};

    var status = 'stopped';
    var loop = opts.loop !== undefined ? Boolean(opts.loop) : true;
    var masterVolume = isFiniteNum(opts.masterVolume) ? clamp(opts.masterVolume, 0, 1) : DEFAULTS.MASTER_VOLUME;
    var positionBeat = 0;
    var anchor = null;
    var scheduledUntilBeat = 0;
    var timerId = null;
    var runId = 0;
    var nextVoiceId = 1;
    var activeVoices = [];
    var listeners = [];
    var disposed = false;

    function createHardwareContext() {
      if (typeof opts.createAudioContext === 'function') return opts.createAudioContext();
      var Ctor = (rootObj && (rootObj.AudioContext || rootObj.webkitAudioContext)) ||
        (typeof AudioContext !== 'undefined' ? AudioContext : null);
      return Ctor ? new Ctor() : null;
    }

    function ensureAudioGraph() {
      if (!ctx) ctx = createHardwareContext();
      if (!ctx) return null;
      if (graphReady) return ctx;

      masterPreGain = ctx.createGain();
      masterPreGain.gain.setValueAtTime(DEFAULTS.HEADROOM_GAIN, ctx.currentTime);

      masterGain = ctx.createGain();
      masterGain.gain.setValueAtTime(masterVolume, ctx.currentTime);

      if (typeof ctx.createDynamicsCompressor === 'function') {
        masterCompressor = ctx.createDynamicsCompressor();
        try {
          masterCompressor.threshold.setValueAtTime(-10, ctx.currentTime);
          masterCompressor.knee.setValueAtTime(12, ctx.currentTime);
          masterCompressor.ratio.setValueAtTime(4, ctx.currentTime);
          masterCompressor.attack.setValueAtTime(0.003, ctx.currentTime);
          masterCompressor.release.setValueAtTime(0.15, ctx.currentTime);
        } catch (_) {}
      }

      if (typeof ctx.createAnalyser === 'function') {
        masterAnalyser = ctx.createAnalyser();
        masterAnalyser.fftSize = 256;
        analyserBuf = new Uint8Array(masterAnalyser.frequencyBinCount || 128);
      }

      var chain = masterPreGain;
      if (masterCompressor) { chain.connect(masterCompressor); chain = masterCompressor; }
      chain.connect(masterGain);
      if (masterAnalyser) {
        masterGain.connect(masterAnalyser);
        masterAnalyser.connect(ctx.destination);
      } else {
        masterGain.connect(ctx.destination);
      }

      var sr = ctx.sampleRate || 44100;
      noiseBuffer = ctx.createBuffer(1, sr, sr);
      var samples = createDeterministicNoiseSamples(sr, 0x51a7c3d1);
      var ch = noiseBuffer.getChannelData(0);
      for (var i = 0; i < sr; i++) ch[i] = samples[i];

      graphReady = true;
      syncTrackBuses();
      return ctx;
    }

    function getOrCreateTrackBus(track) {
      if (!graphReady) return null;
      var bus = trackBuses[track.id];
      if (!bus) {
        var gain = ctx.createGain();
        var panner = typeof ctx.createStereoPanner === 'function' ? ctx.createStereoPanner() : null;
        if (panner) { gain.connect(panner); panner.connect(masterPreGain); }
        else { gain.connect(masterPreGain); }
        bus = { gain: gain, panner: panner };
        trackBuses[track.id] = bus;
      }
      return bus;
    }

    function syncTrackBuses() {
      if (!graphReady || !ctx) return;
      var now = ctx.currentTime;
      var present = {};
      for (var i = 0; i < project.trackOrder.length; i++) {
        var tid = project.trackOrder[i];
        present[tid] = true;
        var track = project.tracksById[tid];
        var bus = getOrCreateTrackBus(track);
        var targetVol = model.isTrackAudible(project, tid) ? clamp(track.volume, 0, 1) : 0;
        try {
          if (typeof bus.gain.gain.setTargetAtTime === 'function') {
            bus.gain.gain.setTargetAtTime(targetVol, now, 0.008);
          } else {
            bus.gain.gain.setValueAtTime(targetVol, now);
          }
          if (bus.panner && bus.panner.pan) {
            bus.panner.pan.setValueAtTime(clamp(track.pan || 0, -1, 1), now);
          }
        } catch (_) {}
      }
      var busIds = Object.keys(trackBuses);
      for (var j = 0; j < busIds.length; j++) {
        if (!present[busIds[j]]) {
          safeDisconnect(trackBuses[busIds[j]].gain);
          safeDisconnect(trackBuses[busIds[j]].panner);
          delete trackBuses[busIds[j]];
        }
      }
    }

    function pruneFinishedVoices(now) {
      var kept = [];
      for (var i = 0; i < activeVoices.length; i++) {
        var v = activeVoices[i];
        if (v.isStopped() || v.endTime <= now - 0.05) v.cleanup();
        else kept.push(v);
      }
      activeVoices = kept;
    }

    function stopAllVoices(now) {
      var t = isFiniteNum(now) ? now : (ctx ? ctx.currentTime : 0);
      for (var i = 0; i < activeVoices.length; i++) {
        activeVoices[i].stop(t);
        if (activeVoices[i].startTime > t) activeVoices[i].cleanup();
      }
      activeVoices = [];
    }

    function cancelFutureVoices(cutoffTime) {
      var kept = [];
      for (var i = 0; i < activeVoices.length; i++) {
        var v = activeVoices[i];
        if (v.startTime > cutoffTime) { v.stop(cutoffTime); v.cleanup(); }
        else kept.push(v);
      }
      activeVoices = kept;
    }

    function hasKeptVoiceNear(beat, now) {
      for (var i = 0; i < activeVoices.length; i++) {
        var v = activeVoices[i];
        if (Math.abs(v.continuousBeat - beat) < 1e-4 || Math.abs(v.startTime - now) < 1e-4) return true;
      }
      return false;
    }

    function clearSchedulerTimer() {
      if (timerId !== null && clearIntervalFn) {
        clearIntervalFn(timerId);
        timerId = null;
      }
    }

    function getPlayheadBeat(nowOverride) {
      var total = model.getTotalBeats(project);
      if (status === 'playing' && anchor && ctx) {
        var now = isFiniteNum(nowOverride) ? nowOverride : ctx.currentTime;
        return computeBeatAtTime(anchor, now, total, loop).displayBeat;
      }
      return clamp(positionBeat, 0, total);
    }

    function getTransportState() {
      return {
        status: status, isPlaying: status === 'playing', isPaused: status === 'paused',
        isStopped: status === 'stopped', currentBeat: getPlayheadBeat(),
        totalBeats: model.getTotalBeats(project), tempo: project.tempo,
        loop: loop, masterVolume: masterVolume, activeVoiceCount: activeVoices.length,
        contextState: ctx ? ctx.state : 'uninitialized'
      };
    }

    function emit(eventName) {
      if (!listeners.length) return;
      var snap = getTransportState(), meta = { event: eventName }, copy = listeners.slice();
      for (var i = 0; i < copy.length; i++) copy[i](snap, meta);
    }

    function scheduleTick(nowOverride) {
      if (status !== 'playing' || !anchor || !ctx) return { scheduledCount: 0, ended: false };
      var now = isFiniteNum(nowOverride) ? nowOverride : ctx.currentTime;
      var totalBeats = model.getTotalBeats(project);
      pruneFinishedVoices(now);

      var pos = computeBeatAtTime(anchor, now, totalBeats, loop);
      if (!loop && pos.reachedEnd) {
        var endTime = computeTimeForBeat(anchor, totalBeats);
        if (now >= endTime - EPS) {
          clearSchedulerTimer();
          stopAllVoices(now);
          status = 'stopped';
          anchor = null;
          positionBeat = 0;
          scheduledUntilBeat = 0;
          emit('ended');
          return { scheduledCount: 0, ended: true };
        }
      }

      var horizonTime = Math.max(now, anchor.anchorTime) + lookaheadSec;
      var horizonBeat = anchor.anchorBeat + secondsToBeats(horizonTime - anchor.anchorTime, anchor.tempo);
      if (horizonBeat <= scheduledUntilBeat + EPS) return { scheduledCount: 0, ended: false };

      var plan = planScheduleWindow(project, {
        windowStartBeat: scheduledUntilBeat, windowEndBeat: horizonBeat, anchor: anchor, loop: loop
      }, model);

      var count = 0;
      for (var i = 0; i < plan.events.length; i++) {
        var ev = plan.events[i];
        if (ev.startTime < now - 0.03) continue;
        if (ev.startTime < now) ev = Object.assign({}, ev, { startTime: now, endTime: now + ev.durationSec });
        var track = project.tracksById[ev.trackId];
        if (!track) continue;
        var bus = getOrCreateTrackBus(track);
        if (!bus) continue;
        activeVoices.push(triggerEventVoice(ctx, bus.gain, ev, nextVoiceId++, noiseBuffer));
        count++;
      }

      scheduledUntilBeat = plan.nextCursorBeat;
      return { scheduledCount: count, ended: false };
    }

    function beginRunningPlayback(myRunId) {
      if (disposed || myRunId !== runId || status !== 'playing' || !ctx) return;
      var total = model.getTotalBeats(project);
      var startBeat = positionBeat >= total - EPS ? 0 : Math.max(0, positionBeat);
      anchor = { anchorTime: ctx.currentTime + startOffsetSec, anchorBeat: startBeat, tempo: clampTempo(project.tempo) };
      scheduledUntilBeat = startBeat;
      syncTrackBuses();
      scheduleTick(ctx.currentTime);
      clearSchedulerTimer();
      if (setIntervalFn && status === 'playing') {
        timerId = setIntervalFn(function () {
          if (myRunId === runId && status === 'playing') scheduleTick();
        }, tickIntervalMs);
      }
      emit('play');
    }

    function play() {
      if (disposed) return Promise.resolve({ ok: false, reason: 'disposed' });
      if (status === 'playing') return Promise.resolve({ ok: true, alreadyPlaying: true, state: getTransportState() });
      var audioCtx = ensureAudioGraph();
      if (!audioCtx) return Promise.resolve({ ok: false, reason: 'no_audio_context' });

      status = 'playing';
      var myRunId = ++runId;

      if (audioCtx.state === 'suspended' && typeof audioCtx.resume === 'function') {
        var p = audioCtx.resume();
        if (p && typeof p.then === 'function') {
          return p.then(function () {
            if (myRunId !== runId || status !== 'playing') {
              return { ok: false, cancelled: true, state: getTransportState() };
            }
            beginRunningPlayback(myRunId);
            return { ok: true, state: getTransportState() };
          }, function (err) {
            if (myRunId === runId && status === 'playing') {
              status = 'stopped'; anchor = null; emit('error');
            }
            return { ok: false, reason: err ? err.message : 'resume_failed' };
          });
        }
      }

      beginRunningPlayback(myRunId);
      return Promise.resolve({ ok: true, state: getTransportState() });
    }

    function pause() {
      if (status !== 'playing') return { ok: true, changed: false, state: getTransportState() };
      runId++;
      clearSchedulerTimer();
      var now = ctx ? ctx.currentTime : 0;
      positionBeat = getPlayheadBeat(now);
      stopAllVoices(now);
      anchor = null;
      status = 'paused';
      emit('pause');
      return { ok: true, changed: true, state: getTransportState() };
    }

    function stop() {
      var wasActive = status !== 'stopped' || positionBeat !== 0 || activeVoices.length > 0;
      runId++;
      clearSchedulerTimer();
      var now = ctx ? ctx.currentTime : 0;
      stopAllVoices(now);
      anchor = null;
      status = 'stopped';
      positionBeat = 0;
      scheduledUntilBeat = 0;
      if (wasActive) emit('stop');
      return { ok: true, changed: wasActive, state: getTransportState() };
    }

    function seek(targetBeat) {
      if (!isFiniteNum(targetBeat)) return { ok: false, reason: 'invalid_beat' };
      var total = model.getTotalBeats(project);
      var clamped = clamp(targetBeat, 0, Math.max(0, total - 0.0001));
      if (targetBeat >= total) clamped = loop ? wrapBeat(targetBeat, total) : total;

      if (status === 'playing' && ctx) {
        var now = ctx.currentTime;
        stopAllVoices(now);
        positionBeat = clamped;
        if (anchor) {
          anchor = { anchorTime: now + startOffsetSec, anchorBeat: clamped, tempo: clampTempo(project.tempo) };
          scheduledUntilBeat = clamped;
          scheduleTick(now);
        }
      } else {
        positionBeat = clamped;
      }
      emit('seek');
      return { ok: true, beat: clamped, state: getTransportState() };
    }

    function setLoop(enabled) {
      var next = Boolean(enabled);
      if (loop === next) return { ok: true, changed: false, loop: loop };
      var prevLoop = loop;
      loop = next;
      if (status === 'playing' && anchor && ctx) {
        var now = ctx.currentTime;
        var pos = computeBeatAtTime(anchor, now, model.getTotalBeats(project), prevLoop);
        cancelFutureVoices(now);
        anchor = { anchorTime: Math.max(now, anchor.anchorTime), anchorBeat: pos.displayBeat, tempo: anchor.tempo };
        var resumeBeat = pos.displayBeat;
        if (now > anchor.anchorTime - EPS && hasKeptVoiceNear(resumeBeat, now)) resumeBeat += 1e-4;
        scheduledUntilBeat = resumeBeat;
        scheduleTick(now);
      }
      emit('loop');
      return { ok: true, changed: true, loop: loop };
    }

    function setMasterVolume(vol) {
      if (!isFiniteNum(vol)) return { ok: false, reason: 'invalid_volume' };
      masterVolume = clamp(vol, 0, 1);
      if (graphReady && masterGain && ctx) {
        var now = ctx.currentTime;
        if (typeof masterGain.gain.setTargetAtTime === 'function') masterGain.gain.setTargetAtTime(masterVolume, now, 0.01);
        else masterGain.gain.setValueAtTime(masterVolume, now);
      }
      emit('masterVolume');
      return { ok: true, masterVolume: masterVolume };
    }

    function rescheduleFromNow(prevTotalBeats) {
      if (status !== 'playing' || !anchor || !ctx) return;
      var now = ctx.currentTime;
      var newTotal = model.getTotalBeats(project);
      var newTempo = clampTempo(project.tempo);
      var oldTotal = prevTotalBeats || newTotal;

      cancelFutureVoices(now);

      if (newTotal !== oldTotal) {
        var oldPos = computeBeatAtTime(anchor, now, oldTotal, loop);
        var mapped = loop ? wrapBeat(oldPos.displayBeat, newTotal) : Math.min(oldPos.displayBeat, newTotal);
        anchor = { anchorTime: Math.max(now, anchor.anchorTime), anchorBeat: mapped, tempo: newTempo };
      } else {
        anchor = reanchorForTempo(anchor, now, newTempo, newTotal, loop);
      }

      var resumeBeat = anchor.anchorBeat;
      if (now > anchor.anchorTime - EPS && hasKeptVoiceNear(resumeBeat, now)) resumeBeat += 1e-4;
      scheduledUntilBeat = resumeBeat;
      scheduleTick(now);
    }

    function applyProjectUpdate(nextProject, actionName) {
      nextProject = resolveProject(nextProject, model);
      var prevTotal = model.getTotalBeats(project);
      var prevTempo = project.tempo;
      project = nextProject;
      syncTrackBuses();

      var newTotal = model.getTotalBeats(project);
      if (status !== 'playing' && positionBeat > newTotal) positionBeat = 0;

      if (status === 'playing' && anchor && ctx) {
        var busOnly = actionName === 'setTrackVolume' || actionName === 'setTrackPan' ||
          actionName === 'renameTrack' || actionName === 'renameClip' ||
          actionName === 'setProjectName' || actionName === 'selectTrack' || actionName === 'selectClip';
        if (!busOnly || Math.abs(project.tempo - prevTempo) > EPS || newTotal !== prevTotal) {
          rescheduleFromNow(prevTotal);
        }
      }
      emit('projectUpdate');
    }

    function auditionNote(trackType, pitch, velocity, durationBeats) {
      if (disposed) return false;
      var audioCtx = ensureAudioGraph();
      if (!audioCtx) return false;
      if (audioCtx.state === 'suspended' && typeof audioCtx.resume === 'function') {
        try { audioCtx.resume(); } catch (_) {}
      }
      var now = audioCtx.currentTime;
      var durBeats = isFiniteNum(durationBeats) && durationBeats > 0 ? durationBeats : 0.5;
      var durSec = beatsToSeconds(durBeats, project.tempo);
      var ev = {
        trackId: 'audition', trackType: trackType || 'synth', clipId: 'audition',
        localBeat: 0, continuousBeat: -1, durationBeats: durBeats,
        pitch: Number.isInteger(pitch) ? pitch : 60,
        velocity: isFiniteNum(velocity) ? clamp(velocity, 0.05, 1) : 0.85,
        startTime: now + 0.005, durationSec: durSec, endTime: now + 0.005 + durSec
      };
      activeVoices.push(triggerEventVoice(audioCtx, masterPreGain, ev, nextVoiceId++, noiseBuffer));
      pruneFinishedVoices(now);
      return true;
    }

    function getMasterLevel() {
      if (!masterAnalyser || !analyserBuf || status !== 'playing') return 0;
      if (typeof masterAnalyser.getByteTimeDomainData !== 'function') return 0;
      masterAnalyser.getByteTimeDomainData(analyserBuf);
      var sum = 0;
      for (var i = 0; i < analyserBuf.length; i++) {
        var norm = (analyserBuf[i] - 128) / 128;
        sum += norm * norm;
      }
      return clamp(Math.sqrt(sum / analyserBuf.length) * 2.2, 0, 1);
    }

    var unsubStore = null;
    if (store && typeof store.subscribe === 'function') {
      unsubStore = store.subscribe(function (storeState, meta) {
        applyProjectUpdate(storeState.project, meta && meta.action);
      });
    }

    function dispose() {
      if (disposed) return;
      disposed = true;
      stop();
      if (unsubStore) { unsubStore(); unsubStore = null; }
      var busIds = Object.keys(trackBuses);
      for (var i = 0; i < busIds.length; i++) {
        safeDisconnect(trackBuses[busIds[i]].gain);
        safeDisconnect(trackBuses[busIds[i]].panner);
      }
      trackBuses = {};
      safeDisconnect(masterPreGain);
      safeDisconnect(masterCompressor);
      safeDisconnect(masterGain);
      safeDisconnect(masterAnalyser);
      listeners = [];
      if (ownsContext && ctx && typeof ctx.close === 'function') {
        try { ctx.close(); } catch (_) {}
      }
      ctx = null;
      graphReady = false;
    }

    return {
      play: play, pause: pause, stop: stop, seek: seek,
      setLoop: setLoop, setMasterVolume: setMasterVolume,
      setProject: function (nextProject) { applyProjectUpdate(nextProject, 'setProject'); },
      auditionNote: auditionNote, tick: scheduleTick,
      getPlayheadBeat: getPlayheadBeat, getTransportState: getTransportState, getMasterLevel: getMasterLevel,
      subscribe: function (fn) {
        if (typeof fn !== 'function') throw new TypeError('Listener must be a function');
        listeners.push(fn);
        return function () { var idx = listeners.indexOf(fn); if (idx !== -1) listeners.splice(idx, 1); };
      },
      dispose: dispose
    };
  }

  return Object.freeze({
    DEFAULTS: DEFAULTS, midiToFreq: midiToFreq, beatsToSeconds: beatsToSeconds, secondsToBeats: secondsToBeats,
    clampTempo: clampTempo, computeBeatAtTime: computeBeatAtTime, computeTimeForBeat: computeTimeForBeat,
    reanchorForTempo: reanchorForTempo, planScheduleWindow: planScheduleWindow,
    createDeterministicNoiseSamples: createDeterministicNoiseSamples, createEngine: createEngine
  });
});
