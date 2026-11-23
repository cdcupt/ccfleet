'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const CueModel = require('./model.js');
const CueAudio = require('./audio.js');

// ---------------------------------------------------------------------------
// Hardware-Free Deterministic Web Audio & Timer Test Harness
// ---------------------------------------------------------------------------

function createMockAudioParam(name, initialValue) {
  const param = {
    name,
    value: initialValue,
    events: [],
    setValueAtTime(v, t) {
      assert.ok(Number.isFinite(v), `${name}.setValueAtTime value must be finite`);
      assert.ok(Number.isFinite(t) && t >= 0, `${name}.setValueAtTime time must be >= 0`);
      param.value = v;
      param.events.push({ op: 'setValue', value: v, time: t });
      return param;
    },
    linearRampToValueAtTime(v, t) {
      assert.ok(Number.isFinite(v), `${name}.linearRamp value must be finite`);
      assert.ok(Number.isFinite(t) && t >= 0, `${name}.linearRamp time must be >= 0`);
      param.events.push({ op: 'linearRamp', value: v, time: t });
      return param;
    },
    exponentialRampToValueAtTime(v, t) {
      assert.ok(Number.isFinite(v) && v > 0, `${name}.exponentialRamp value must be > 0, got ${v}`);
      assert.ok(Number.isFinite(t) && t >= 0, `${name}.exponentialRamp time must be >= 0`);
      param.events.push({ op: 'expRamp', value: v, time: t });
      return param;
    },
    setTargetAtTime(target, startTime, timeConstant) {
      assert.ok(Number.isFinite(target), `${name}.setTarget target must be finite`);
      assert.ok(Number.isFinite(startTime) && startTime >= 0, `${name}.setTarget startTime must be >= 0`);
      assert.ok(Number.isFinite(timeConstant) && timeConstant > 0, `${name}.setTarget tc must be > 0`);
      param.value = target;
      param.events.push({ op: 'setTarget', value: target, time: startTime, timeConstant });
      return param;
    },
    cancelScheduledValues(t) {
      assert.ok(Number.isFinite(t) && t >= 0, `${name}.cancelScheduledValues time must be >= 0`);
      param.events.push({ op: 'cancel', time: t });
      return param;
    }
  };
  return param;
}

function createMockAudioContext(options) {
  const opts = options || {};
  let pendingResolve = null;
  let pendingReject = null;

  const ctx = {
    currentTime: 0,
    sampleRate: opts.sampleRate || 44100,
    state: opts.initialState || 'suspended',
    resumeCalls: 0,
    closeCalls: 0,
    closed: false,
    analyserWaveform: null,
    destination: { kind: 'destination', inputs: [] },
    oscillators: [],
    bufferSources: [],
    gains: [],
    filters: [],
    panners: [],
    compressors: [],
    analysers: [],
    buffers: [],

    advance(dt) {
      assert.ok(Number.isFinite(dt) && dt >= 0, 'advance dt must be >= 0');
      ctx.currentTime = Math.round((ctx.currentTime + dt) * 1e9) / 1e9;
    },

    resolveResume() {
      ctx.state = 'running';
      if (pendingResolve) {
        const fn = pendingResolve;
        pendingResolve = null;
        pendingReject = null;
        fn();
      }
    },

    rejectResume(err) {
      if (pendingReject) {
        const fn = pendingReject;
        pendingResolve = null;
        pendingReject = null;
        fn(err || new Error('resume rejected'));
      }
    },

    resume() {
      ctx.resumeCalls++;
      if (opts.deferredResume) {
        return new Promise((resolve, reject) => {
          pendingResolve = resolve;
          pendingReject = reject;
        });
      }
      ctx.state = 'running';
      return Promise.resolve();
    },

    close() {
      ctx.closeCalls++;
      ctx.closed = true;
      ctx.state = 'closed';
      return Promise.resolve();
    },

    createGain() {
      const node = makeBaseNode('gain');
      node.gain = createMockAudioParam('gain', 1);
      ctx.gains.push(node);
      return node;
    },

    createStereoPanner() {
      const node = makeBaseNode('panner');
      node.pan = createMockAudioParam('pan', 0);
      ctx.panners.push(node);
      return node;
    },

    createBiquadFilter() {
      const node = makeBaseNode('filter');
      node.type = 'lowpass';
      node.frequency = createMockAudioParam('frequency', 350);
      node.Q = createMockAudioParam('Q', 1);
      ctx.filters.push(node);
      return node;
    },

    createDynamicsCompressor() {
      const node = makeBaseNode('compressor');
      node.threshold = createMockAudioParam('threshold', -24);
      node.knee = createMockAudioParam('knee', 30);
      node.ratio = createMockAudioParam('ratio', 12);
      node.attack = createMockAudioParam('attack', 0.003);
      node.release = createMockAudioParam('release', 0.25);
      ctx.compressors.push(node);
      return node;
    },

    createAnalyser() {
      const node = makeBaseNode('analyser');
      node.fftSize = 2048;
      Object.defineProperty(node, 'frequencyBinCount', {
        get() { return node.fftSize / 2; }
      });
      node.getByteTimeDomainData = function (buf) {
        if (ctx.analyserWaveform) {
          for (let i = 0; i < buf.length; i++) buf[i] = ctx.analyserWaveform(i, buf.length);
        } else {
          buf.fill(128);
        }
      };
      ctx.analysers.push(node);
      return node;
    },

    createBuffer(channels, length, sampleRate) {
      const data = [];
      for (let i = 0; i < channels; i++) data.push(new Float32Array(length));
      const buf = {
        numberOfChannels: channels,
        length,
        sampleRate,
        getChannelData(ch) { return data[ch]; }
      };
      ctx.buffers.push(buf);
      return buf;
    },

    createOscillator() {
      const node = makeBaseNode('oscillator');
      node.type = 'sine';
      node.frequency = createMockAudioParam('frequency', 440);
      node.detune = createMockAudioParam('detune', 0);
      attachSourceLifecycle(node);
      ctx.oscillators.push(node);
      return node;
    },

    createBufferSource() {
      const node = makeBaseNode('bufferSource');
      node.buffer = null;
      attachSourceLifecycle(node);
      ctx.bufferSources.push(node);
      return node;
    },

    getAllSources() {
      return ctx.oscillators.concat(ctx.bufferSources);
    }
  };

  function makeBaseNode(kind) {
    const node = {
      kind,
      connections: [],
      disconnected: false,
      connect(dest) {
        assert.ok(dest, `${kind}.connect destination must exist`);
        node.connections.push(dest);
        if (dest && Array.isArray(dest.inputs)) dest.inputs.push(node);
        return dest;
      },
      disconnect() {
        node.disconnected = true;
      }
    };
    return node;
  }

  function attachSourceLifecycle(node) {
    node.started = false;
    node.startTime = null;
    node.stopped = false;
    node.stopTime = null;
    node.stopCalls = [];
    node.onended = null;
    node.start = function (t) {
      const when = t === undefined ? ctx.currentTime : t;
      assert.equal(node.started, false, `${node.kind}.start() called twice`);
      assert.ok(Number.isFinite(when) && when >= 0, `${node.kind}.start time must be >= 0`);
      node.started = true;
      node.startTime = when;
    };
    node.stop = function (t) {
      const when = t === undefined ? ctx.currentTime : t;
      assert.ok(Number.isFinite(when) && when >= 0, `${node.kind}.stop time must be >= 0`);
      if (node.startTime !== null) {
        assert.ok(when >= node.startTime - 1e-9, `${node.kind}.stop(${when}) precedes start(${node.startTime})`);
      }
      node.stopCalls.push(when);
      node.stopped = true;
      node.stopTime = node.stopTime === null ? when : Math.min(node.stopTime, when);
    };
  }

  return ctx;
}

function createMockTimers() {
  let nextId = 1;
  const active = new Map();
  return {
    active,
    totalCreated: 0,
    totalCleared: 0,
    setInterval(fn, ms) {
      const id = nextId++;
      this.totalCreated++;
      active.set(id, { fn, ms });
      return id;
    },
    clearInterval(id) {
      if (active.delete(id)) this.totalCleared++;
    },
    runAll() {
      for (const entry of Array.from(active.values())) entry.fn();
    }
  };
}

function makeSimpleKickPublicProject(tempo, lengthBars) {
  const kickPat = CueModel.getPatternById('kick-four');
  const bars = lengthBars || 4;
  return {
    schemaVersion: 1,
    name: 'Kick Pulse',
    tempo: tempo || 120,
    beatsPerBar: 4,
    lengthBars: bars,
    tracks: [
      { id: 'trk-kick', name: 'Kick', type: 'kick', color: '#e85d4a', volume: 0.9, pan: 0, mute: false, solo: false }
    ],
    clips: [
      {
        id: 'clip-kick-1', trackId: 'trk-kick', name: 'Four on Floor',
        startBeat: 0, durationBeats: bars * 4,
        patternId: kickPat.id, patternSteps: kickPat.patternSteps, gain: 1, notes: kickPat.notes
      }
    ]
  };
}

function makeSimpleKickProject(tempo, lengthBars) {
  const res = CueModel.validateProject(makeSimpleKickPublicProject(tempo, lengthBars));
  assert.equal(res.ok, true);
  return res.project;
}

// ---------------------------------------------------------------------------
// 1. Pure Time Mapping, Tempo Re-anchoring & Deterministic Noise
// ---------------------------------------------------------------------------

test('pure time mapping, tempo clamping, re-anchoring, and deterministic noise', () => {
  assert.ok(Math.abs(CueAudio.midiToFreq(69) - 440) < 1e-9);
  assert.ok(Math.abs(CueAudio.midiToFreq(57) - 220) < 1e-9);
  assert.ok(Math.abs(CueAudio.midiToFreq(81) - 880) < 1e-9);
  assert.ok(Math.abs(CueAudio.midiToFreq(60) - 261.6255653) < 1e-4);

  assert.equal(CueAudio.beatsToSeconds(4, 120), 2);
  assert.equal(CueAudio.secondsToBeats(2, 120), 4);
  for (const bpm of [60, 124, 180]) {
    const sec = CueAudio.beatsToSeconds(7.5, bpm);
    assert.ok(Math.abs(CueAudio.secondsToBeats(sec, bpm) - 7.5) < 1e-9);
  }

  assert.equal(CueAudio.clampTempo(10), 40);
  assert.equal(CueAudio.clampTempo(400), 240);
  assert.equal(CueAudio.clampTempo(Number.NaN), 124);

  const anchor = { anchorTime: 1.0, anchorBeat: 4, tempo: 120 };
  const tBeat8 = CueAudio.computeTimeForBeat(anchor, 8);
  assert.ok(Math.abs(tBeat8 - 3.0) < 1e-9);

  const posLoop = CueAudio.computeBeatAtTime(anchor, 10.0, 16, true);
  assert.ok(Math.abs(posLoop.continuousBeat - 22) < 1e-9);
  assert.ok(Math.abs(posLoop.displayBeat - 6) < 1e-9);
  assert.equal(posLoop.reachedEnd, false);

  const posNoLoop = CueAudio.computeBeatAtTime(anchor, 10.0, 16, false);
  assert.equal(posNoLoop.continuousBeat, 16);
  assert.equal(posNoLoop.displayBeat, 16);
  assert.equal(posNoLoop.reachedEnd, true);

  // Re-anchoring preserves exact beat at the tempo-change instant
  const reanchored = CueAudio.reanchorForTempo(anchor, 2.5, 180, 16, true);
  assert.equal(reanchored.anchorTime, 2.5);
  assert.ok(Math.abs(reanchored.anchorBeat - 7) < 1e-9);
  assert.equal(reanchored.tempo, 180);
  // Next 3 beats at 180 BPM take 1.0 second -> t = 3.5
  assert.ok(Math.abs(CueAudio.computeTimeForBeat(reanchored, 10) - 3.5) < 1e-9);

  // Re-anchoring before anchorTime keeps anchorTime/anchorBeat intact
  const preReanchor = CueAudio.reanchorForTempo(anchor, 0.5, 90, 16, true);
  assert.equal(preReanchor.anchorTime, 1.0);
  assert.equal(preReanchor.anchorBeat, 4);
  assert.equal(preReanchor.tempo, 90);

  // Deterministic noise buffer samples
  const a = CueAudio.createDeterministicNoiseSamples(512, 0x12345678);
  const b = CueAudio.createDeterministicNoiseSamples(512, 0x12345678);
  const c = CueAudio.createDeterministicNoiseSamples(512, 0x87654321);
  assert.deepEqual(a, b);
  assert.notDeepEqual(a, c);
  for (let i = 0; i < a.length; i++) {
    assert.ok(a[i] >= -1 && a[i] <= 1);
  }
});

// ---------------------------------------------------------------------------
// 2. Pure Lookahead Planner: Partitioning, Mute/Solo, End & Loop Wraparound
// ---------------------------------------------------------------------------

test('planScheduleWindow partitions contiguous windows without drops/dupes and handles loop & solo', () => {
  const project = CueModel.createStarterProject(); // 8 bars = 32 beats, 124 BPM
  const totalBeats = CueModel.getTotalBeats(project);
  const fullExpected = CueModel.expandProjectEvents(project);
  const anchor = { anchorTime: 0.5, anchorBeat: 0, tempo: project.tempo };

  // Partition [0, 32) using non-grid irregular chunk sizes (0.37 beats)
  const gathered = [];
  let cursor = 0;
  let reachedEnd = false;
  while (cursor < totalBeats) {
    const plan = CueAudio.planScheduleWindow(project, {
      windowStartBeat: cursor,
      windowEndBeat: cursor + 0.37,
      anchor,
      loop: false
    });
    for (const ev of plan.events) gathered.push(ev);
    assert.ok(plan.nextCursorBeat > cursor);
    cursor = plan.nextCursorBeat;
    reachedEnd = plan.reachedEnd;
  }

  assert.equal(reachedEnd, true);
  assert.equal(cursor, totalBeats);
  assert.equal(gathered.length, fullExpected.length);

  for (let i = 0; i < fullExpected.length; i++) {
    assert.equal(gathered[i].trackId, fullExpected[i].trackId);
    assert.equal(gathered[i].clipId, fullExpected[i].clipId);
    assert.ok(Math.abs(gathered[i].localBeat - fullExpected[i].beat) < 1e-9);
    assert.equal(gathered[i].pitch, fullExpected[i].pitch);
    const expectedStart = 0.5 + CueAudio.beatsToSeconds(fullExpected[i].beat, project.tempo);
    const expectedDur = CueAudio.beatsToSeconds(fullExpected[i].durationBeats, project.tempo);
    assert.ok(Math.abs(gathered[i].startTime - expectedStart) < 1e-9);
    assert.ok(Math.abs(gathered[i].endTime - (expectedStart + expectedDur)) < 1e-9);
  }

  // Public vs normalized project contract across createStore and planScheduleWindow
  const publicStarter = CueModel.toPublicProject(project);
  const storeFromPublic = CueModel.createStore({ project: publicStarter });
  const storeFromNorm = CueModel.createStore({ project });
  assert.deepEqual(storeFromNorm.exportPublicProject(), storeFromPublic.exportPublicProject());

  // Malformed normalized project must still be rejected by createStore
  assert.throws(() => {
    CueModel.createStore({
      project: {
        schemaVersion: 1, name: 'Empty', tempo: 120, beatsPerBar: 4, lengthBars: 4,
        trackOrder: [], tracksById: {}, clipsById: {}, trackClipIds: {}
      }
    });
  }, /Invalid initial project/);

  // Public project also accepted by planScheduleWindow identically to normalized state
  const pubWindow = CueAudio.planScheduleWindow(publicStarter, {
    windowStartBeat: 0, windowEndBeat: 8, anchor, loop: false
  });
  const normWindow = CueAudio.planScheduleWindow(project, {
    windowStartBeat: 0, windowEndBeat: 8, anchor, loop: false
  });
  assert.deepEqual(pubWindow, normWindow);

  // Solo filtering
  storeFromPublic.setTrackSolo('trk-kick', true);
  const soloPlan = CueAudio.planScheduleWindow(storeFromPublic.getState().project, {
    windowStartBeat: 0, windowEndBeat: 8, anchor, loop: false
  });
  assert.ok(soloPlan.events.length > 0);
  assert.ok(soloPlan.events.every((e) => e.trackId === 'trk-kick'));

  // Looping wraparound across bar 8 -> bar 1 boundary [31.5, 32.5)
  const wrapPlan = CueAudio.planScheduleWindow(project, {
    windowStartBeat: 31.5, windowEndBeat: 32.5, anchor, loop: true
  });
  assert.equal(wrapPlan.reachedEnd, false);
  assert.equal(wrapPlan.nextCursorBeat, 32.5);
  const cycle1 = wrapPlan.events.filter((e) => e.continuousBeat < 32);
  const cycle2 = wrapPlan.events.filter((e) => e.continuousBeat >= 32);
  assert.ok(cycle1.length > 0, 'expected tail events from cycle 1');
  assert.ok(cycle2.length > 0, 'expected beat-0 events wrapped into cycle 2');
  assert.ok(cycle2.some((e) => e.localBeat === 0 && e.continuousBeat === 32));
  for (let i = 1; i < wrapPlan.events.length; i++) {
    assert.ok(wrapPlan.events[i].startTime >= wrapPlan.events[i - 1].startTime - 1e-9);
  }
});

// ---------------------------------------------------------------------------
// 3. Lazy AudioContext, Master Headroom Chain & All 6 Synthesized Voices
// ---------------------------------------------------------------------------

test('lazy AudioContext on gesture, master headroom chain, and synthesis across all 6 track types', async () => {
  let createdCount = 0;
  let mockCtx = null;

  const res = CueModel.validateProject({
    schemaVersion: 1,
    name: 'All Instruments',
    tempo: 120,
    beatsPerBar: 4,
    lengthBars: 4,
    tracks: [
      { id: 't-kick', name: 'Kick', type: 'kick', color: '#e85d4a', volume: 0.9, pan: 0, mute: false, solo: false },
      { id: 't-snare', name: 'Snare', type: 'snare', color: '#e69f38', volume: 0.8, pan: -0.1, mute: false, solo: false },
      { id: 't-hat', name: 'Hat', type: 'hat', color: '#38b8a8', volume: 0.7, pan: 0.1, mute: false, solo: false },
      { id: 't-bass', name: 'Bass', type: 'bass', color: '#7c6ef0', volume: 0.85, pan: 0, mute: false, solo: false },
      { id: 't-synth', name: 'Synth', type: 'synth', color: '#3b9eff', volume: 0.75, pan: -0.2, mute: false, solo: false },
      { id: 't-pad', name: 'Pad', type: 'pad', color: '#d966a8', volume: 0.65, pan: 0.2, mute: false, solo: false }
    ],
    clips: [
      { id: 'c-kick', trackId: 't-kick', name: 'K', startBeat: 0, durationBeats: 4, patternId: 'custom', patternSteps: 16, gain: 1, notes: [{ step: 0, pitch: 36, velocity: 0.9, durationSteps: 1 }] },
      { id: 'c-snare', trackId: 't-snare', name: 'S', startBeat: 0, durationBeats: 4, patternId: 'custom', patternSteps: 16, gain: 1, notes: [{ step: 0, pitch: 38, velocity: 0.9, durationSteps: 1 }, { step: 2, pitch: 39, velocity: 0.85, durationSteps: 1 }] },
      { id: 'c-hat', trackId: 't-hat', name: 'H', startBeat: 0, durationBeats: 4, patternId: 'custom', patternSteps: 16, gain: 1, notes: [{ step: 0, pitch: 42, velocity: 0.8, durationSteps: 0.5 }, { step: 2, pitch: 46, velocity: 0.85, durationSteps: 2 }] },
      { id: 'c-bass', trackId: 't-bass', name: 'B', startBeat: 0, durationBeats: 4, patternId: 'custom', patternSteps: 16, gain: 1, notes: [{ step: 0, pitch: 33, velocity: 0.9, durationSteps: 2 }] },
      { id: 'c-synth', trackId: 't-synth', name: 'Sy', startBeat: 0, durationBeats: 4, patternId: 'custom', patternSteps: 16, gain: 1, notes: [{ step: 0, pitch: 69, velocity: 0.8, durationSteps: 2 }] },
      { id: 'c-pad', trackId: 't-pad', name: 'P', startBeat: 0, durationBeats: 4, patternId: 'custom', patternSteps: 16, gain: 1, notes: [{ step: 0, pitch: 57, velocity: 0.7, durationSteps: 4 }] }
    ]
  });
  assert.equal(res.ok, true);

  const timers = createMockTimers();
  const engine = CueAudio.createEngine({
    project: res.project,
    createAudioContext: () => {
      createdCount++;
      mockCtx = createMockAudioContext({ initialState: 'suspended' });
      return mockCtx;
    },
    setInterval: timers.setInterval.bind(timers),
    clearInterval: timers.clearInterval.bind(timers),
    startOffsetSec: 0,
    lookaheadSec: 0.4 // 0.8 beats at 120 BPM -> covers beat 0 and beat 0.5
  });

  // AudioContext must NOT exist prior to user gesture
  assert.equal(createdCount, 0);
  assert.equal(engine.getTransportState().contextState, 'uninitialized');

  const playRes = await engine.play();
  assert.equal(playRes.ok, true);
  assert.equal(createdCount, 1);
  assert.equal(mockCtx.resumeCalls, 1);
  assert.equal(mockCtx.state, 'running');

  // Verify master headroom chain topology: PreGain (0.72) -> Compressor -> MasterGain (0.85) -> Analyser -> Destination
  const preGain = mockCtx.gains[0];
  const masterGain = mockCtx.gains[1];
  const compressor = mockCtx.compressors[0];
  const analyser = mockCtx.analysers[0];
  assert.equal(preGain.gain.value, CueAudio.DEFAULTS.HEADROOM_GAIN);
  assert.equal(masterGain.gain.value, CueAudio.DEFAULTS.MASTER_VOLUME);
  assert.ok(preGain.connections.includes(compressor));
  assert.ok(compressor.connections.includes(masterGain));
  assert.ok(masterGain.connections.includes(analyser));
  assert.ok(analyser.connections.includes(mockCtx.destination));

  // Verify master volume control updates masterGain
  const volRes = engine.setMasterVolume(0.5);
  assert.equal(volRes.ok, true);
  assert.equal(masterGain.gain.value, 0.5);

  // Verify noise buffer was deterministically generated
  assert.equal(mockCtx.buffers.length, 1);
  assert.equal(mockCtx.buffers[0].length, 44100);

  // Verify pitched oscillators hit exact MIDI frequencies: bass A1 (33 -> 55 Hz), pad A3 (57 -> 220 Hz), synth A4 (69 -> 440 Hz)
  const oscFreqs = mockCtx.oscillators.map((o) => o.frequency.value);
  assert.ok(oscFreqs.some((f) => Math.abs(f - 55) < 1e-6), 'expected bass 55 Hz oscillator');
  assert.ok(oscFreqs.some((f) => Math.abs(f - 220) < 1e-6), 'expected pad 220 Hz oscillator');
  assert.ok(oscFreqs.some((f) => Math.abs(f - 440) < 1e-6), 'expected synth 440 Hz oscillator');

  // Verify snare(38), clap(39), closed hat(42), open hat(46) all created noise BufferSources (4 total)
  assert.equal(mockCtx.bufferSources.length, 4);
  const closedHatSrc = mockCtx.bufferSources.find(
    (s) => s.connections[0] && s.connections[0].type === 'highpass' && s.connections[0].frequency.value === 7800
  );
  const openHatSrc = mockCtx.bufferSources.find(
    (s) => s.connections[0] && s.connections[0].type === 'highpass' && s.connections[0].frequency.value === 6800
  );
  assert.ok(closedHatSrc, 'expected closed hat noise source with 7800 Hz highpass');
  assert.ok(openHatSrc, 'expected open hat noise source with 6800 Hz highpass');
  assert.ok(
    (openHatSrc.stopTime - openHatSrc.startTime) > (closedHatSrc.stopTime - closedHatSrc.startTime),
    'open hat decay should exceed closed hat decay'
  );

  // Verify clap uses bandpass filter while snare uses highpass and pitched synths use lowpass
  const filterTypes = mockCtx.filters.map((f) => f.type);
  assert.ok(filterTypes.includes('highpass'));
  assert.ok(filterTypes.includes('bandpass'));
  assert.ok(filterTypes.includes('lowpass'));

  // Every source must be started and have a valid future stop time
  for (const src of mockCtx.getAllSources()) {
    assert.equal(src.started, true);
    assert.equal(src.stopped, true);
    assert.ok(src.stopTime > src.startTime);
  }

  engine.dispose();
});

// ---------------------------------------------------------------------------
// 4. Rapid Repeated play() & stop() During Pending resume()
// ---------------------------------------------------------------------------

test('rapid repeated play() produces no duplicate timers/voices and stop() during resume cancels cleanly', async () => {
  const project = makeSimpleKickProject(120, 4);
  const ctx1 = createMockAudioContext({ initialState: 'suspended', deferredResume: true });
  const timers1 = createMockTimers();

  const engine1 = CueAudio.createEngine({
    project,
    audioContext: ctx1,
    setInterval: timers1.setInterval.bind(timers1),
    clearInterval: timers1.clearInterval.bind(timers1),
    startOffsetSec: 0,
    lookaheadSec: 0.12
  });

  // Fire 3 rapid play() calls before resume() settles
  const p1 = engine1.play();
  const p2 = engine1.play();
  const p3 = engine1.play();

  assert.equal(ctx1.resumeCalls, 1, 'resume() should only be invoked once');
  assert.equal(timers1.active.size, 0, 'no scheduler timer until resume resolves');
  assert.equal(ctx1.getAllSources().length, 0, 'no sources scheduled before resume resolves');

  const r2 = await p2;
  const r3 = await p3;
  assert.equal(r2.alreadyPlaying, true);
  assert.equal(r3.alreadyPlaying, true);

  ctx1.resolveResume();
  const r1 = await p1;
  assert.equal(r1.ok, true);
  assert.equal(timers1.active.size, 1, 'exactly one scheduler timer active');
  assert.equal(ctx1.oscillators.length, 2, 'exactly one kick voice (body + click) for beat 0');

  // Calling play() again while already running does not duplicate anything
  const r4 = await engine1.play();
  assert.equal(r4.alreadyPlaying, true);
  assert.equal(timers1.active.size, 1);
  assert.equal(ctx1.oscillators.length, 2);
  engine1.dispose();

  // Case B: stop() called while resume() promise is still pending
  const ctx2 = createMockAudioContext({ initialState: 'suspended', deferredResume: true });
  const timers2 = createMockTimers();
  const engine2 = CueAudio.createEngine({
    project,
    audioContext: ctx2,
    setInterval: timers2.setInterval.bind(timers2),
    clearInterval: timers2.clearInterval.bind(timers2),
    startOffsetSec: 0,
    lookaheadSec: 0.12
  });

  const pPending = engine2.play();
  engine2.stop();
  ctx2.resolveResume();
  const resCancel = await pPending;

  assert.equal(resCancel.ok, false);
  assert.equal(resCancel.cancelled, true);
  assert.equal(engine2.getTransportState().status, 'stopped');
  assert.equal(timers2.active.size, 0);
  assert.equal(ctx2.getAllSources().length, 0);
  engine2.dispose();
});

// ---------------------------------------------------------------------------
// 5. pause() / resume() Continuity & stop() Zero-Orphan Cleanup
// ---------------------------------------------------------------------------

test('pause preserves playhead and resumes without re-triggering; stop leaves zero orphaned sources', async () => {
  const project = makeSimpleKickProject(120, 4);
  const ctx = createMockAudioContext({ initialState: 'running' });
  const timers = createMockTimers();
  const engine = CueAudio.createEngine({
    project,
    audioContext: ctx,
    setInterval: timers.setInterval.bind(timers),
    clearInterval: timers.clearInterval.bind(timers),
    startOffsetSec: 0,
    lookaheadSec: 0.12,
    loop: true
  });

  await engine.play();
  assert.equal(ctx.oscillators.length, 2); // beat 0

  // Step 50 x 25ms = 1.25s -> beat 2.5 (beats 0, 1, 2 scheduled)
  for (let i = 0; i < 50; i++) {
    ctx.advance(0.025);
    timers.runAll();
  }
  assert.equal(ctx.oscillators.length, 6); // 3 kicks x 2 oscs

  const pauseRes = engine.pause();
  assert.equal(pauseRes.ok, true);
  assert.equal(pauseRes.state.status, 'paused');
  assert.ok(Math.abs(pauseRes.state.currentBeat - 2.5) < 1e-6);
  assert.equal(pauseRes.state.activeVoiceCount, 0);
  assert.equal(timers.active.size, 0);
  assert.ok(ctx.getAllSources().every((s) => s.stopped));

  // Time advancing while paused must not drift playhead or schedule voices
  ctx.advance(5.0);
  assert.ok(Math.abs(engine.getPlayheadBeat() - 2.5) < 1e-6);
  assert.equal(ctx.oscillators.length, 6);

  // Resume from beat 2.5: beats 0, 1, 2 must NOT re-trigger
  await engine.play();
  assert.equal(ctx.oscillators.length, 6);

  // Advance 0.25s -> beat 3.0 triggers cleanly
  for (let i = 0; i < 10; i++) {
    ctx.advance(0.025);
    timers.runAll();
  }
  assert.equal(ctx.oscillators.length, 8);
  assert.ok(Math.abs(ctx.oscillators[6].startTime - 6.5) < 1e-6);

  // Advance 0.40s to t=6.90 (horizon 7.02 -> beat 4.04 schedules future beat 4 at t=7.00)
  for (let i = 0; i < 16; i++) {
    ctx.advance(0.025);
    timers.runAll();
  }
  assert.equal(ctx.oscillators.length, 10);
  assert.ok(ctx.oscillators[8].startTime > ctx.currentTime);

  // Stop at t=6.90: resets playhead to 0, clears timer, stops all sources, disconnects unstarted beat-4 voice
  const stopRes = engine.stop();
  assert.equal(stopRes.state.status, 'stopped');
  assert.equal(stopRes.state.currentBeat, 0);
  assert.equal(stopRes.state.activeVoiceCount, 0);
  assert.equal(timers.active.size, 0);
  assert.ok(ctx.getAllSources().every((s) => s.stopped));
  assert.equal(ctx.oscillators[8].disconnected, true, 'future unstarted source must be disconnected on stop');
  assert.equal(ctx.oscillators[9].disconnected, true, 'future unstarted click must be disconnected on stop');

  engine.dispose();
});

// ---------------------------------------------------------------------------
// 6. Live Tempo Changes During Playback
// ---------------------------------------------------------------------------

test('live tempo changes re-anchor smoothly, cancel stale future voices, and avoid boundary duplicates', async () => {
  const store = CueModel.createStore({ project: makeSimpleKickPublicProject(120, 4) });
  const ctx = createMockAudioContext({ initialState: 'running' });
  const timers = createMockTimers();

  const engine = CueAudio.createEngine({
    store,
    audioContext: ctx,
    setInterval: timers.setInterval.bind(timers),
    clearInterval: timers.clearInterval.bind(timers),
    startOffsetSec: 0,
    lookaheadSec: 0.3,
    loop: true
  });

  await engine.play(); // schedules beat 0 at t=0
  assert.equal(ctx.oscillators.length, 2);

  // Advance to t=0.35 (beat 0.7 at 120 BPM; horizon t=0.65 -> beat 1.3 schedules beat 1 at t=0.50)
  ctx.advance(0.35);
  engine.tick();
  assert.equal(ctx.oscillators.length, 4);
  const staleBeat1Body = ctx.oscillators[2];
  assert.ok(Math.abs(staleBeat1Body.startTime - 0.5) < 1e-6);
  assert.equal(staleBeat1Body.disconnected, false);

  // Drop tempo from 120 BPM to 60 BPM at t=0.35
  store.setTempo(60);

  // Playhead continuity: still beat 0.7 at t=0.35
  assert.ok(Math.abs(engine.getPlayheadBeat() - 0.7) < 1e-6);
  // Stale beat-1 voice scheduled for old t=0.50 must be cancelled & disconnected
  assert.equal(staleBeat1Body.disconnected, true);

  // At 60 BPM, remaining 0.3 beats take 0.30s -> beat 1 moves to t = 0.65
  ctx.advance(0.05); // t = 0.40, horizon = 0.70 -> beat 1.05
  engine.tick();
  const rescheduledBeat1Body = ctx.oscillators[4];
  assert.ok(rescheduledBeat1Body, 'beat 1 should be rescheduled at new tempo');
  assert.ok(Math.abs(rescheduledBeat1Body.startTime - 0.65) < 1e-6);
  assert.equal(rescheduledBeat1Body.disconnected, false);

  // Edge case: change tempo 1ms before beat 1 fires (t = 0.649, beat 1 at 0.650)
  ctx.advance(0.249); // t = 0.649
  store.setTempo(120);
  assert.equal(rescheduledBeat1Body.disconnected, true, 'voice 1ms in future must be cancelled');

  // Exactly one live (non-disconnected) body oscillator for beat 1 at t = 0.6495
  const liveBeat1Bodies = ctx.oscillators.filter(
    (o) => o.type === 'sine' && !o.disconnected && o.startTime > 0.6 && o.startTime < 0.7
  );
  assert.equal(liveBeat1Bodies.length, 1, 'no duplicate beat-1 voice across tempo boundary');
  assert.ok(Math.abs(liveBeat1Bodies[0].startTime - 0.6495) < 1e-6);

  engine.dispose();
});

// ---------------------------------------------------------------------------
// 7. seek() While Stopped, Paused, and Playing
// ---------------------------------------------------------------------------

test('seek clamps bounds and cleanly hands over playback to target position with zero residual voices', async () => {
  const res = CueModel.validateProject({
    schemaVersion: 1,
    name: 'Seek Pitches',
    tempo: 120,
    beatsPerBar: 4,
    lengthBars: 4,
    tracks: [
      { id: 't-bass', name: 'Bass', type: 'bass', color: '#7c6ef0', volume: 0.85, pan: 0, mute: false, solo: false }
    ],
    clips: [
      { id: 'c1', trackId: 't-bass', name: 'Bar1', startBeat: 0, durationBeats: 4, patternId: 'custom', patternSteps: 16, gain: 1, notes: [{ step: 0, pitch: 33, velocity: 0.9, durationSteps: 4 }] },
      { id: 'c2', trackId: 't-bass', name: 'Bar2', startBeat: 4, durationBeats: 4, patternId: 'custom', patternSteps: 16, gain: 1, notes: [{ step: 0, pitch: 45, velocity: 0.9, durationSteps: 4 }] }
    ]
  });
  assert.equal(res.ok, true);

  const ctx = createMockAudioContext({ initialState: 'running' });
  const timers = createMockTimers();
  const engine = CueAudio.createEngine({
    project: res.project,
    audioContext: ctx,
    setInterval: timers.setInterval.bind(timers),
    clearInterval: timers.clearInterval.bind(timers),
    startOffsetSec: 0,
    lookaheadSec: 0.12
  });

  // Stopped seek + validation
  assert.equal(engine.seek(Number.NaN).ok, false);
  assert.equal(engine.seek(-5).beat, 0);
  assert.equal(engine.seek(10).beat, 10);
  assert.equal(engine.getPlayheadBeat(), 10);
  engine.seek(0);

  // Play at beat 0 -> bass A1 (33 -> 55 Hz)
  await engine.play();
  assert.equal(ctx.oscillators.length, 2);
  assert.ok(Math.abs(ctx.oscillators[0].frequency.value - 55) < 1e-6);

  // Seek to beat 4 while playing at t = 0.10 -> stops A1, schedules A2 (45 -> 110 Hz)
  ctx.advance(0.1);
  const seekRes = engine.seek(4);
  assert.equal(seekRes.ok, true);
  assert.equal(seekRes.beat, 4);
  assert.equal(ctx.oscillators[0].stopped, true);
  assert.equal(ctx.oscillators[1].stopped, true);
  assert.equal(ctx.oscillators.length, 4);
  assert.ok(Math.abs(ctx.oscillators[2].frequency.value - 110) < 1e-6);
  assert.ok(Math.abs(ctx.oscillators[2].startTime - 0.1) < 1e-6);

  // Seek while paused updates beat without scheduling voices
  engine.pause();
  const countBeforePausedSeek = ctx.oscillators.length;
  engine.seek(8);
  assert.equal(engine.getPlayheadBeat(), 8);
  assert.equal(engine.getTransportState().status, 'paused');
  assert.equal(ctx.oscillators.length, countBeforePausedSeek);

  engine.dispose();
});

// ---------------------------------------------------------------------------
// 8. End-of-Arrangement (loop: false) vs Continuous Looping (loop: true)
// ---------------------------------------------------------------------------

test('non-looping playback ends cleanly at arrangement end; looping wraps seamlessly into next cycle', async () => {
  const project = makeSimpleKickProject(120, 4); // 16 beats
  const ctx = createMockAudioContext({ initialState: 'running' });
  const timers = createMockTimers();
  const events = [];

  const engine = CueAudio.createEngine({
    project,
    audioContext: ctx,
    setInterval: timers.setInterval.bind(timers),
    clearInterval: timers.clearInterval.bind(timers),
    startOffsetSec: 0,
    lookaheadSec: 0.12,
    loop: false
  });
  engine.subscribe((_state, meta) => events.push(meta.event));

  // Part A: Non-looping from beat 14 -> plays beats 14 and 15, then stops at beat 16
  engine.seek(14);
  await engine.play();
  for (let i = 0; i < 44; i++) {
    ctx.advance(0.025);
    timers.runAll();
  }

  assert.ok(events.includes('ended'), 'should emit ended event');
  assert.equal(engine.getTransportState().status, 'stopped');
  assert.equal(engine.getTransportState().currentBeat, 0);
  assert.equal(engine.getTransportState().activeVoiceCount, 0);
  assert.equal(timers.active.size, 0);
  assert.equal(ctx.oscillators.length, 4, 'only beats 14 and 15 (2 kicks x 2 oscs)');

  // Part B: Looping from beat 14 -> wraps past beat 16 into cycle 2 (beats 14, 15, 0, 1)
  engine.setLoop(true);
  engine.seek(14);
  await engine.play();
  const oscsAtLoopStart = ctx.oscillators.length; // includes beat 14 (2 oscs)
  assert.equal(oscsAtLoopStart, 6);

  for (let i = 0; i < 64; i++) { // 1.6s = 3.2 beats -> displayBeat 1.2
    ctx.advance(0.025);
    timers.runAll();
  }

  assert.equal(engine.getTransportState().status, 'playing');
  assert.ok(Math.abs(engine.getPlayheadBeat() - 1.2) < 1e-6);
  assert.equal(timers.active.size, 1);
  // 4 total kicks in looping run (beats 14, 15, 0, 1) -> 8 oscs added since Part A's 4
  assert.equal(ctx.oscillators.length, 12);

  engine.dispose();
});

// ---------------------------------------------------------------------------
// 9. Live Mixer (Mute/Solo/Volume) & Arrangement Edits Mid-Playback
// ---------------------------------------------------------------------------

test('track mute/solo/volume and live clip edits immediately reflect in audio buses and scheduling', async () => {
  const store = CueModel.createStore(); // starter project: 5 tracks, 124 BPM
  const ctx = createMockAudioContext({ initialState: 'running' });
  const timers = createMockTimers();

  const engine = CueAudio.createEngine({
    store,
    audioContext: ctx,
    setInterval: timers.setInterval.bind(timers),
    clearInterval: timers.clearInterval.bind(timers),
    startOffsetSec: 0,
    lookaheadSec: 0.12,
    loop: true
  });

  await engine.play();

  // Map track bus gains (created in project.trackOrder immediately after masterPreGain & masterGain)
  const trackOrder = store.getState().project.trackOrder;
  const busGainByTrack = {};
  for (let i = 0; i < trackOrder.length; i++) {
    busGainByTrack[trackOrder[i]] = ctx.gains[2 + i];
  }
  assert.equal(busGainByTrack['trk-kick'].gain.value, 0.9);

  // Mute kick -> bus drops to 0 immediately
  store.setTrackMute('trk-kick', true);
  assert.equal(busGainByTrack['trk-kick'].gain.value, 0);

  // Solo kick -> overrides mute! Kick restores to 0.9, all other tracks drop to 0
  store.setTrackSolo('trk-kick', true);
  assert.equal(busGainByTrack['trk-kick'].gain.value, 0.9);
  for (const tid of trackOrder) {
    if (tid !== 'trk-kick') assert.equal(busGainByTrack[tid].gain.value, 0);
  }

  // Volume change updates bus gain without cancelling/duplicating active voices
  const oscsBeforeVol = ctx.oscillators.length;
  store.setTrackVolume('trk-kick', 0.45);
  assert.equal(busGainByTrack['trk-kick'].gain.value, 0.45);
  assert.equal(ctx.oscillators.length, oscsBeforeVol);

  // Advance 1 beat (60 / 124 sec): only soloed kick schedules (2 oscs, 0 noise bufferSources)
  const bufBefore = ctx.bufferSources.length;
  const oscsBeforeBeat1 = ctx.oscillators.length;
  ctx.advance(60 / 124);
  engine.tick();
  assert.equal(ctx.bufferSources.length, bufBefore, 'muted/non-soloed hats/snares must not schedule');
  assert.equal(ctx.oscillators.length, oscsBeforeBeat1 + 2, 'only soloed kick should schedule');

  engine.dispose();

  // Arrangement edit mid-playback: deleting & creating clips reflects immediately
  const editStore = CueModel.createStore({ project: makeSimpleKickPublicProject(120, 4) });
  // Replace single 16-beat clip with one clip on beats 0..4 and one on beats 4..8
  editStore.resizeClip('clip-kick-1', { durationBeats: 4 });
  const added = editStore.createClip({ trackId: 'trk-kick', startBeat: 4, durationBeats: 4, patternId: 'kick-four' });
  assert.equal(added.ok, true);

  const ctx2 = createMockAudioContext({ initialState: 'running' });
  const timers2 = createMockTimers();
  const engine2 = CueAudio.createEngine({
    store: editStore,
    audioContext: ctx2,
    setInterval: timers2.setInterval.bind(timers2),
    clearInterval: timers2.clearInterval.bind(timers2),
    startOffsetSec: 0,
    lookaheadSec: 0.12,
    loop: false
  });

  await engine2.play();
  // Delete the upcoming clip at beats 4..8 while at beat 0
  editStore.deleteClip(added.clip.id);

  // Advance from t=0 to t=3.5s (beats 0..7): only beats 0, 1, 2, 3 should play (4 kicks = 8 oscs)
  for (let i = 0; i < 140; i++) {
    ctx2.advance(0.025);
    timers2.runAll();
  }
  assert.equal(ctx2.oscillators.length, 8, 'deleted clip at beats 4..8 must not schedule');

  // Create a new clip at beats 8..12 while playing at beat 7
  const createdLive = editStore.createClip({ trackId: 'trk-kick', startBeat: 8, durationBeats: 4, patternId: 'kick-four' });
  assert.equal(createdLive.ok, true);

  // Advance 0.6s (beat 7.0 -> 8.2): beat 8 of the newly created clip MUST schedule
  for (let i = 0; i < 24; i++) {
    ctx2.advance(0.025);
    timers2.runAll();
  }
  assert.equal(ctx2.oscillators.length, 10, 'newly created clip at beat 8 must schedule live');

  engine2.dispose();
});

// ---------------------------------------------------------------------------
// 10. auditionNote(), getMasterLevel(), and dispose() Lifecycle
// ---------------------------------------------------------------------------

test('auditionNote triggers pitched preview, getMasterLevel meters output, and dispose cleans up safely', async () => {
  const normKick = makeSimpleKickProject(120, 4);
  const store = CueModel.createStore({ project: normKick });
  // Mutating the passed-in normalized project externally must not corrupt the store
  normKick.tempo = 200;
  normKick.tracksById['trk-kick'].volume = 0;
  assert.equal(store.getState().project.tempo, 120);
  assert.equal(store.getState().project.tracksById['trk-kick'].volume, 0.9);

  let ownedCtx = null;
  const timers = createMockTimers();

  const engine = CueAudio.createEngine({
    store,
    createAudioContext: () => {
      ownedCtx = createMockAudioContext({ initialState: 'suspended' });
      return ownedCtx;
    },
    setInterval: timers.setInterval.bind(timers),
    clearInterval: timers.clearInterval.bind(timers),
    startOffsetSec: 0
  });

  // auditionNote works before play() and triggers exact pitched synth voice (A4 = 440 Hz)
  const ok = engine.auditionNote('synth', 69, 0.8, 0.5);
  assert.equal(ok, true);
  assert.ok(ownedCtx);
  assert.equal(ownedCtx.resumeCalls, 1);
  assert.equal(ownedCtx.oscillators.length, 2);
  assert.ok(Math.abs(ownedCtx.oscillators[0].frequency.value - 440) < 1e-6);

  // Meter returns 0 when stopped, > 0 when playing with signal
  assert.equal(engine.getMasterLevel(), 0);
  ownedCtx.analyserWaveform = (i) => (i % 2 === 0 ? 192 : 64);
  await engine.play();
  const level = engine.getMasterLevel();
  assert.ok(level > 0 && level <= 1, `expected meter level in (0, 1], got ${level}`);

  // dispose() closes owned AudioContext, clears timer, unsubscribes from store, and blocks further play
  engine.dispose();
  assert.equal(ownedCtx.closeCalls, 1);
  assert.equal(timers.active.size, 0);
  assert.equal((await engine.play()).reason, 'disposed');
  assert.equal(engine.auditionNote('kick', 36), false);

  // Store mutations after dispose must not throw or re-trigger anything
  assert.doesNotThrow(() => store.setTempo(140));

  // Externally injected AudioContext must NOT be closed by engine.dispose(), and public projects work in createEngine
  const externalCtx = createMockAudioContext({ initialState: 'running' });
  const extEngine = CueAudio.createEngine({
    project: makeSimpleKickPublicProject(120, 4),
    audioContext: externalCtx,
    setInterval: timers.setInterval.bind(timers),
    clearInterval: timers.clearInterval.bind(timers)
  });
  await extEngine.play();
  assert.ok(externalCtx.oscillators.length >= 2, 'engine constructed from public project must schedule voices');
  extEngine.dispose();
  assert.equal(externalCtx.closeCalls, 0, 'injected AudioContext must not be closed by dispose()');
});
