'use strict';

var test = require('node:test');
var assert = require('node:assert/strict');
var CueModel = require('./model.js');

var LIMITS = CueModel.LIMITS;

function makePublicStarter() {
  return CueModel.toPublicProject(CueModel.createStarterProject());
}

test('1. starter project has valid normalized schema, 5 musical track types, 8 bars, and deterministic layout', function () {
  var a = CueModel.createStarterProject();
  var b = CueModel.createStarterProject();

  assert.equal(a.schemaVersion, LIMITS.SCHEMA_VERSION);
  assert.equal(a.beatsPerBar, 4);
  assert.equal(a.lengthBars, 8);
  assert.equal(CueModel.getTotalBeats(a), 32);
  assert.equal(a.trackOrder.length, 5);

  var types = a.trackOrder.map(function (id) { return a.tracksById[id].type; });
  assert.deepEqual(types, ['kick', 'snare', 'hat', 'bass', 'synth']);

  var clipIds = Object.keys(a.clipsById);
  assert.ok(clipIds.length >= 12, 'expected rich starter clip arrangement');

  // Verify non-overlapping sorted clips on every track
  a.trackOrder.forEach(function (trackId) {
    var ids = a.trackClipIds[trackId];
    assert.ok(ids.length >= 2);
    for (var i = 1; i < ids.length; i++) {
      var prev = a.clipsById[ids[i - 1]];
      var curr = a.clipsById[ids[i]];
      assert.ok(prev.startBeat + prev.durationBeats <= curr.startBeat);
    }
  });

  // Deterministic across repeated creations
  assert.deepEqual(CueModel.toPublicProject(a), CueModel.toPublicProject(b));

  var store = CueModel.createStore();
  var st = store.getState();
  assert.equal(st.selection.trackId, 'trk-kick');
  assert.equal(st.selection.clipId, 'clip-kick-1');
  assert.equal(st.history.canUndo, false);
  assert.equal(st.history.canRedo, false);
});

test('2. validation rejects bad schema versions, root shapes, and invalid beatsPerBar', function () {
  var badRoots = [null, undefined, [], 'project', 42, true];
  badRoots.forEach(function (root) {
    var res = CueModel.validateProject(root);
    assert.equal(res.ok, false);
    assert.ok(res.errors.length > 0);
  });

  var badVersions = [undefined, null, 0, 2, '1', NaN, Infinity, 1.5];
  badVersions.forEach(function (ver) {
    var raw = makePublicStarter();
    raw.schemaVersion = ver;
    var res = CueModel.validateProject(raw);
    assert.equal(res.ok, false, 'should reject schemaVersion ' + String(ver));
  });

  var badBeats = makePublicStarter();
  badBeats.beatsPerBar = 3;
  assert.equal(CueModel.validateProject(badBeats).ok, false);
});

test('3. validation rejects malformed and non-finite numeric values across project, tracks, clips, and notes', function () {
  var nonFiniteCases = [NaN, Infinity, -Infinity, '120', null];
  nonFiniteCases.forEach(function (badVal) {
    var pTempo = makePublicStarter();
    pTempo.tempo = badVal;
    assert.equal(CueModel.validateProject(pTempo).ok, false);

    var pVol = makePublicStarter();
    pVol.tracks[0].volume = badVal;
    assert.equal(CueModel.validateProject(pVol).ok, false);

    var pPan = makePublicStarter();
    pPan.tracks[0].pan = badVal;
    assert.equal(CueModel.validateProject(pPan).ok, false);

    var pStart = makePublicStarter();
    pStart.clips[0].startBeat = badVal;
    assert.equal(CueModel.validateProject(pStart).ok, false);

    var pDur = makePublicStarter();
    pDur.clips[0].durationBeats = badVal;
    assert.equal(CueModel.validateProject(pDur).ok, false);

    var pGain = makePublicStarter();
    pGain.clips[0].gain = badVal;
    assert.equal(CueModel.validateProject(pGain).ok, false);

    var pVel = makePublicStarter();
    pVel.clips[0].notes[0].velocity = badVal;
    assert.equal(CueModel.validateProject(pVel).ok, false);

    var pStepDur = makePublicStarter();
    pStepDur.clips[0].notes[0].durationSteps = badVal;
    assert.equal(CueModel.validateProject(pStepDur).ok, false);
  });

  // Out-of-range and malformed field values
  var badColor = makePublicStarter();
  badColor.tracks[0].color = 'red';
  assert.equal(CueModel.validateProject(badColor).ok, false);

  var badTrackType = makePublicStarter();
  badTrackType.tracks[0].type = 'kazoo';
  assert.equal(CueModel.validateProject(badTrackType).ok, false);

  var badPitch = makePublicStarter();
  badPitch.clips[0].notes[0].pitch = 12.5;
  assert.equal(CueModel.validateProject(badPitch).ok, false);

  var badStep = makePublicStarter();
  badStep.clips[0].notes[0].step = 16; // >= patternSteps (16)
  assert.equal(CueModel.validateProject(badStep).ok, false);

  var zeroVel = makePublicStarter();
  zeroVel.clips[0].notes[0].velocity = 0;
  assert.equal(CueModel.validateProject(zeroVel).ok, false);

  var blankName = makePublicStarter();
  blankName.name = '   ';
  assert.equal(CueModel.validateProject(blankName).ok, false);
});

test('4. validation rejects duplicate track IDs, duplicate clip IDs, whitespace IDs, and unknown trackId refs', function () {
  var dupTrack = makePublicStarter();
  dupTrack.tracks[1].id = dupTrack.tracks[0].id;
  var resTrack = CueModel.validateProject(dupTrack);
  assert.equal(resTrack.ok, false);
  assert.ok(resTrack.errors.some(function (e) { return /Duplicate track id/.test(e); }));

  var dupClip = makePublicStarter();
  dupClip.clips[1].id = dupClip.clips[0].id;
  var resClip = CueModel.validateProject(dupClip);
  assert.equal(resClip.ok, false);
  assert.ok(resClip.errors.some(function (e) { return /Duplicate clip id/.test(e); }));

  var dangling = makePublicStarter();
  dangling.clips[0].trackId = 'trk-does-not-exist';
  var resRef = CueModel.validateProject(dangling);
  assert.equal(resRef.ok, false);
  assert.ok(resRef.errors.some(function (e) { return /unknown track/.test(e); }));

  var paddedId = makePublicStarter();
  paddedId.clips[0].id = ' clip-kick-1 ';
  assert.equal(CueModel.validateProject(paddedId).ok, false);
});

test('5. validation enforces collection bounds (tracks, clips, per-track clips, notes, lengthBars, JSON size)', function () {
  // 0 tracks
  var zeroTracks = makePublicStarter();
  zeroTracks.tracks = [];
  zeroTracks.clips = [];
  assert.equal(CueModel.validateProject(zeroTracks).ok, false);

  // Too many tracks (> MAX_TRACKS)
  var tooManyTracks = makePublicStarter();
  tooManyTracks.clips = [];
  tooManyTracks.tracks = [];
  for (var i = 0; i < LIMITS.MAX_TRACKS + 1; i++) {
    tooManyTracks.tracks.push({
      id: 'trk-' + i,
      name: 'Track ' + i,
      type: 'kick',
      color: '#e85d4a',
      volume: 0.8,
      pan: 0,
      mute: false,
      solo: false
    });
  }
  assert.equal(CueModel.validateProject(tooManyTracks).ok, false);

  // Too many clips on a single track (> MAX_CLIPS_PER_TRACK)
  var tooManyOnTrack = makePublicStarter();
  tooManyOnTrack.lengthBars = 32; // 128 beats -> 256 half-beat slots
  tooManyOnTrack.clips = [];
  for (var c = 0; c < LIMITS.MAX_CLIPS_PER_TRACK + 1; c++) {
    tooManyOnTrack.clips.push({
      id: 'c-' + c,
      trackId: 'trk-kick',
      name: 'Clip ' + c,
      startBeat: c * 0.5,
      durationBeats: 0.5,
      patternId: 'custom',
      patternSteps: 16,
      gain: 0.8,
      notes: []
    });
  }
  assert.equal(CueModel.validateProject(tooManyOnTrack).ok, false);

  // Too many total clips (> MAX_CLIPS)
  var tooManyTotal = makePublicStarter();
  tooManyTotal.lengthBars = 32;
  tooManyTotal.clips = [];
  for (var k = 0; k < LIMITS.MAX_CLIPS + 1; k++) {
    var tid = tooManyTotal.tracks[k % tooManyTotal.tracks.length].id;
    var slot = Math.floor(k / tooManyTotal.tracks.length);
    tooManyTotal.clips.push({
      id: 'tot-' + k,
      trackId: tid,
      name: 'Clip ' + k,
      startBeat: slot * 0.5,
      durationBeats: 0.5,
      patternId: 'custom',
      patternSteps: 16,
      gain: 0.8,
      notes: []
    });
  }
  assert.equal(CueModel.validateProject(tooManyTotal).ok, false);

  // Too many notes in one clip (> MAX_NOTES_PER_CLIP)
  var tooManyNotes = makePublicStarter();
  tooManyNotes.clips[0].notes = [];
  for (var n = 0; n < LIMITS.MAX_NOTES_PER_CLIP + 1; n++) {
    tooManyNotes.clips[0].notes.push({ step: n % 16, pitch: 36 + (n % 24), velocity: 0.8, durationSteps: 1 });
  }
  assert.equal(CueModel.validateProject(tooManyNotes).ok, false);

  // lengthBars out of bounds
  var tooShort = makePublicStarter();
  tooShort.clips = [];
  tooShort.lengthBars = LIMITS.MIN_BARS - 1;
  assert.equal(CueModel.validateProject(tooShort).ok, false);

  var tooLong = makePublicStarter();
  tooLong.lengthBars = LIMITS.MAX_BARS + 1;
  assert.equal(CueModel.validateProject(tooLong).ok, false);

  // Oversized JSON string
  var hugeJson = '{' + ' '.repeat(LIMITS.MAX_JSON_CHARS + 16) + '}';
  assert.equal(CueModel.deserializeProject(hugeJson).ok, false);
});

test('6. validation rejects physically impossible or overlapping clips while allowing touching clips', function () {
  var negStart = makePublicStarter();
  negStart.clips[0].startBeat = -0.5;
  assert.equal(CueModel.validateProject(negStart).ok, false);

  var tinyDur = makePublicStarter();
  tinyDur.clips[0].durationBeats = 0.25; // < MIN_CLIP_BEATS (0.5)
  assert.equal(CueModel.validateProject(tinyDur).ok, false);

  var pastEnd = makePublicStarter();
  pastEnd.clips[2].startBeat = 30;
  pastEnd.clips[2].durationBeats = 4; // ends at 34 > 32
  assert.equal(CueModel.validateProject(pastEnd).ok, false);

  var unaligned = makePublicStarter();
  unaligned.clips[0].startBeat = 0.1;
  assert.equal(CueModel.validateProject(unaligned).ok, false);

  // Overlapping clips on same track
  var overlap = makePublicStarter();
  overlap.clips[1].startBeat = 15.5; // clip-kick-1 is [0, 16)
  var resOverlap = CueModel.validateProject(overlap);
  assert.equal(resOverlap.ok, false);
  assert.ok(resOverlap.errors.some(function (e) { return /Overlapping clips/.test(e); }));

  // Exactly touching clips [0, 16) and [16, 24) are valid
  var touching = makePublicStarter();
  touching.clips[1].startBeat = 16;
  touching.clips[1].durationBeats = 8;
  assert.equal(CueModel.validateProject(touching).ok, true);
});

test('7. atomic import preserves current project, selection, and undo/redo history on invalid import', function () {
  var store = CueModel.createStore();
  store.setTempo(132);
  store.setProjectName('Before Import');
  store.undo(); // creates pastCount=1, futureCount=1
  store.selectClip('clip-bass-1');

  var beforeState = store.getState();
  var beforeJson = store.exportJSON();

  // Bad JSON syntax
  var r1 = store.importJSON('{not valid json');
  assert.equal(r1.ok, false);
  assert.equal(r1.changed, false);
  assert.ok(r1.errors.length > 0);

  // Bad schema object
  var badObj = makePublicStarter();
  badObj.schemaVersion = 99;
  var r2 = store.importProject(badObj);
  assert.equal(r2.ok, false);

  // Overlapping clips object
  var overlapObj = makePublicStarter();
  overlapObj.clips[1].startBeat = 8;
  var r3 = store.importJSON(JSON.stringify(overlapObj));
  assert.equal(r3.ok, false);

  // Verify zero mutation to project, selection, or history
  var afterState = store.getState();
  assert.equal(store.exportJSON(), beforeJson);
  assert.deepEqual(afterState.selection, beforeState.selection);
  assert.deepEqual(afterState.history, beforeState.history);
  assert.equal(afterState.history.pastCount, 1);
  assert.equal(afterState.history.futureCount, 1);
});

test('8. selection updates track/clip coherently without creating undo entries or truncating redo', function () {
  var store = CueModel.createStore();
  store.setTempo(130);
  store.setTempo(135);
  store.undo(); // past=1, future=1

  var resClip = store.selectClip('clip-synth-2');
  assert.equal(resClip.ok, true);
  assert.equal(store.getState().selection.clipId, 'clip-synth-2');
  assert.equal(store.getState().selection.trackId, 'trk-synth');

  // Selecting another track clears clipId from previous track
  var resTrack = store.selectTrack('trk-hat');
  assert.equal(resTrack.ok, true);
  assert.equal(store.getState().selection.trackId, 'trk-hat');
  assert.equal(store.getState().selection.clipId, null);

  // Deselecting clip explicitly
  store.selectClip('clip-hat-1');
  assert.equal(store.getState().selection.clipId, 'clip-hat-1');
  store.selectClip(null);
  assert.equal(store.getState().selection.clipId, null);
  assert.equal(store.getState().selection.trackId, 'trk-hat');

  // Invalid IDs fail cleanly
  assert.equal(store.selectTrack('trk-missing').ok, false);
  assert.equal(store.selectClip('clip-missing').ok, false);

  // History untouched by selection operations
  var hist = store.getState().history;
  assert.equal(hist.pastCount, 1);
  assert.equal(hist.futureCount, 1);
});

test('9. createClip fits open gaps intentionally, auto-places when start omitted, and rejects collisions', function () {
  var store = CueModel.createStore();
  // On trk-kick, starter clips are [0, 16), [20, 28), [28, 32) -> gap at [16, 20)
  // Ask for 8 beats starting at beat 18 -> must fit cleanly into [18, 20) (duration 2)
  var fitRes = store.createClip({ trackId: 'trk-kick', startBeat: 18, durationBeats: 8, name: 'Bridge Kick' });
  assert.equal(fitRes.ok, true);
  assert.equal(fitRes.clip.startBeat, 18);
  assert.equal(fitRes.clip.durationBeats, 2);
  assert.equal(store.getState().selection.clipId, fitRes.clip.id);

  // Attempting to create inside an occupied clip rejects cleanly
  var collideRes = store.createClip({ trackId: 'trk-kick', startBeat: 4, durationBeats: 2 });
  assert.equal(collideRes.ok, false);
  assert.equal(collideRes.reason, 'collision');

  // Auto-placement on trk-kick finds remaining gap [16, 18) and fits 2 beats
  var autoRes = store.createClip({ trackId: 'trk-kick', durationBeats: 4 });
  assert.equal(autoRes.ok, true);
  assert.equal(autoRes.clip.startBeat, 16);
  assert.equal(autoRes.clip.durationBeats, 2);

  // Track is now completely full [0, 32): auto-place fails with no_space
  var fullRes = store.createClip({ trackId: 'trk-kick', durationBeats: 1 });
  assert.equal(fullRes.ok, false);
  assert.equal(fullRes.reason, 'no_space');

  // Non-finite arguments rejected
  assert.equal(store.createClip({ trackId: 'trk-snare', startBeat: NaN, durationBeats: 2 }).ok, false);
  assert.equal(store.createClip({ trackId: 'trk-snare', startBeat: 0, durationBeats: Infinity }).ok, false);
});

test('10. moveClip clamps to timeline edges, magnetically clamps against neighbors, supports strict mode & cross-track move', function () {
  var store = CueModel.createStore();

  // Starter trk-snare has [4, 16), [16, 24), [24, 32) -> [0, 4) is open.
  // Moving clip-snare-1 past 0 clamps to 0.
  var leftClamp = store.moveClip('clip-snare-1', { startBeat: -10 });
  assert.equal(leftClamp.ok, true);
  assert.equal(leftClamp.clip.startBeat, 0);

  // Create a 2-beat clip in trk-kick gap [16, 20) at beat 16 -> [16, 18)
  var created = store.createClip({ trackId: 'trk-kick', startBeat: 16, durationBeats: 2 });
  assert.equal(created.ok, true);
  var shortId = created.clip.id;

  // Move it into clip-kick-2 [20, 28) with desiredStart=19 -> magnetically clamps flush to 18 ([18, 20))
  var magRes = store.moveClip(shortId, { startBeat: 19 });
  assert.equal(magRes.ok, true);
  assert.equal(magRes.clip.startBeat, 18);

  // Strict mode rejects overlapping move instead of clamping
  var strictRes = store.moveClip(shortId, { startBeat: 19, fitMode: 'strict' });
  assert.equal(strictRes.ok, false);
  assert.equal(strictRes.reason, 'collision');

  // No-op move (same track and position) does not push undo history
  var pastBefore = store.getState().history.pastCount;
  var noopRes = store.moveClip(shortId, { startBeat: 18 });
  assert.equal(noopRes.ok, true);
  assert.equal(noopRes.changed, false);
  assert.equal(store.getState().history.pastCount, pastBefore);

  // Cross-track move into an open gap on trk-synth ([0, 4) is open)
  var crossRes = store.moveClip(shortId, { trackId: 'trk-synth', startBeat: 1 });
  assert.equal(crossRes.ok, true);
  assert.equal(crossRes.clip.trackId, 'trk-synth');
  assert.equal(crossRes.clip.startBeat, 1);
  assert.equal(store.getState().project.trackClipIds['trk-kick'].indexOf(shortId), -1);
  assert.ok(store.getState().project.trackClipIds['trk-synth'].indexOf(shortId) !== -1);

  // Attempting to move 4-beat clip-kick-3 [28, 32) onto a track with only a 1-beat gap fails cleanly
  // trk-synth currently has shortId [1, 3) and clip-synth-1 [4, 16) -> only 1-beat gaps [0,1) and [3,4)
  var blockedCross = store.moveClip('clip-kick-3', { trackId: 'trk-synth', startBeat: 0 });
  assert.equal(blockedCross.ok, false);
  assert.equal(blockedCross.reason, 'collision');

  // Moving clip past end of timeline clamps to totalBeats - durationBeats
  store.deleteClip('clip-kick-3'); // opens [28, 32)
  var rightClamp = store.moveClip('clip-kick-2', { startBeat: 99 }); // dur=8 -> clamps to 24
  assert.equal(rightClamp.ok, true);
  assert.equal(rightClamp.clip.startBeat, 24);
});

test('11. resizeClip enforces min duration, neighbor collision bounds, timeline end, and left-edge trimming', function () {
  var store = CueModel.createStore();

  // clip-kick-1 is [0, 16), next clip is clip-kick-2 [20, 28)
  // Right-edge grow to 30 clamps to 20 (flush with clip-kick-2)
  var growRes = store.resizeClip('clip-kick-1', { durationBeats: 30 });
  assert.equal(growRes.ok, true);
  assert.equal(growRes.clip.durationBeats, 20);

  // Right-edge shrink below MIN_CLIP_BEATS clamps to MIN_CLIP_BEATS (0.5)
  var shrinkRes = store.resizeClip('clip-kick-1', { durationBeats: 0.1 });
  assert.equal(shrinkRes.ok, true);
  assert.equal(shrinkRes.clip.durationBeats, LIMITS.MIN_CLIP_BEATS);

  // Left-edge resize of clip-kick-2 [20, 28): grow left past clip-kick-1 [0, 0.5) clamps to 0.5, preserving end=28
  var leftGrow = store.resizeClip('clip-kick-2', { startBeat: 0 });
  assert.equal(leftGrow.ok, true);
  assert.equal(leftGrow.clip.startBeat, 0.5);
  assert.equal(leftGrow.clip.durationBeats, 27.5);

  // Left-edge trim right past end-MIN_CLIP_BEATS clamps to 27.5 (duration 0.5)
  var leftTrim = store.resizeClip('clip-kick-2', { startBeat: 28 });
  assert.equal(leftTrim.ok, true);
  assert.equal(leftTrim.clip.startBeat, 27.5);
  assert.equal(leftTrim.clip.durationBeats, LIMITS.MIN_CLIP_BEATS);

  // Last clip right-edge resize clamps at totalBeats (32)
  store.resizeClip('clip-kick-3', { durationBeats: 2 }); // [28, 30)
  var endClamp = store.resizeClip('clip-kick-3', { durationBeats: 20 });
  assert.equal(endClamp.ok, true);
  assert.equal(endClamp.clip.startBeat, 28);
  assert.equal(endClamp.clip.durationBeats, 4);

  // Reject non-finite values
  assert.equal(store.resizeClip('clip-kick-3', { durationBeats: NaN }).ok, false);
  assert.equal(store.resizeClip('clip-kick-3', { startBeat: Infinity }).ok, false);
});

test('12. duplicateClip and deleteClip preserve non-overlap invariants and clean selection state', function () {
  var store = CueModel.createStore();

  // trk-kick has [0,16), [20,28), [28,32) -> gap [16, 20) of size 4.
  // Duplicating clip-kick-3 (dur 4) cannot fit after 32, falls back to open gap [16, 20).
  var dup = store.duplicateClip('clip-kick-3');
  assert.equal(dup.ok, true);
  assert.equal(dup.clip.startBeat, 16);
  assert.equal(dup.clip.durationBeats, 4);
  assert.notEqual(dup.clip.id, 'clip-kick-3');
  assert.equal(store.getState().selection.clipId, dup.clip.id);

  // Now trk-kick is completely full: duplicating again fails with no_space
  var dupFail = store.duplicateClip('clip-kick-3');
  assert.equal(dupFail.ok, false);
  assert.equal(dupFail.reason, 'no_space');

  // Delete selected duplicated clip -> clears clipId, keeps trackId
  var del = store.deleteClip(dup.clip.id);
  assert.equal(del.ok, true);
  assert.equal(store.getState().project.clipsById[dup.clip.id], undefined);
  assert.equal(store.getState().selection.clipId, null);
  assert.equal(store.getState().selection.trackId, 'trk-kick');

  // Deleting unknown clip fails
  assert.equal(store.deleteClip('clip-unknown').ok, false);
});

test('13. track mute/solo/volume/pan/rename and effective audibility matrix', function () {
  var store = CueModel.createStore();

  // Initially all audible
  var proj = store.getState().project;
  assert.equal(CueModel.isTrackAudible(proj, 'trk-kick'), true);
  assert.equal(CueModel.isTrackAudible(proj, 'trk-bass'), true);

  // Mute kick -> kick silent, bass audible
  store.setTrackMute('trk-kick', true);
  proj = store.getState().project;
  assert.equal(CueModel.isTrackAudible(proj, 'trk-kick'), false);
  assert.equal(CueModel.isTrackAudible(proj, 'trk-bass'), true);

  // Solo bass -> only bass audible
  store.setTrackSolo('trk-bass', true);
  proj = store.getState().project;
  assert.equal(CueModel.isTrackAudible(proj, 'trk-bass'), true);
  assert.equal(CueModel.isTrackAudible(proj, 'trk-synth'), false);
  assert.equal(CueModel.isTrackAudible(proj, 'trk-kick'), false);

  // Solo kick as well -> both soloed tracks audible (solo overrides mute)
  store.toggleTrackSolo('trk-kick');
  proj = store.getState().project;
  assert.equal(CueModel.isTrackAudible(proj, 'trk-kick'), true);
  assert.equal(CueModel.isTrackAudible(proj, 'trk-bass'), true);
  assert.equal(CueModel.isTrackAudible(proj, 'trk-hat'), false);

  // Clear solos -> mute state resumes
  store.setTrackSolo('trk-bass', false);
  store.setTrackSolo('trk-kick', false);
  proj = store.getState().project;
  assert.equal(CueModel.isTrackAudible(proj, 'trk-kick'), false);
  assert.equal(CueModel.isTrackAudible(proj, 'trk-hat'), true);

  // Volume clamping and non-finite rejection
  store.setTrackVolume('trk-kick', 2.5);
  assert.equal(store.getState().project.tracksById['trk-kick'].volume, 1);
  store.setTrackVolume('trk-kick', -0.4);
  assert.equal(store.getState().project.tracksById['trk-kick'].volume, 0);
  assert.equal(store.setTrackVolume('trk-kick', NaN).ok, false);

  // Pan clamping and non-finite rejection
  store.setTrackPan('trk-hat', 3);
  assert.equal(store.getState().project.tracksById['trk-hat'].pan, 1);
  store.setTrackPan('trk-hat', -3);
  assert.equal(store.getState().project.tracksById['trk-hat'].pan, -1);
  assert.equal(store.setTrackPan('trk-hat', Infinity).ok, false);

  // Rename track
  assert.equal(store.renameTrack('trk-hat', '  Crisp Hats  ').ok, true);
  assert.equal(store.getState().project.tracksById['trk-hat'].name, 'Crisp Hats');
  assert.equal(store.renameTrack('trk-hat', '   ').ok, false);
});

test('14. project tempo, name, and lengthBars mutation boundaries', function () {
  var store = CueModel.createStore();

  // Tempo min/max clamping & non-finite rejection
  store.setTempo(10);
  assert.equal(store.getState().project.tempo, LIMITS.MIN_TEMPO);
  store.setTempo(999);
  assert.equal(store.getState().project.tempo, LIMITS.MAX_TEMPO);
  assert.equal(store.setTempo(NaN).ok, false);
  assert.equal(store.setTempo(-Infinity).ok, false);

  // Name validation
  assert.equal(store.setProjectName('  Solar Cue  ').ok, true);
  assert.equal(store.getState().project.name, 'Solar Cue');
  assert.equal(store.setProjectName('').ok, false);
  assert.equal(store.setProjectName('x'.repeat(LIMITS.MAX_NAME_LENGTH + 1)).ok, false);

  // Extending lengthBars succeeds
  assert.equal(store.setLengthBars(16).ok, true);
  assert.equal(store.getState().project.lengthBars, 16);

  // Shrinking below existing clips rejects by default
  var shrinkFail = store.setLengthBars(4);
  assert.equal(shrinkFail.ok, false);
  assert.equal(shrinkFail.reason, 'clips_out_of_bounds');
  assert.equal(store.getState().project.lengthBars, 16);

  // Shrinking with truncate: true trims straddling clips and drops out-of-bounds clips
  var shrinkOk = store.setLengthBars(4, { truncate: true });
  assert.equal(shrinkOk.ok, true);
  var proj = store.getState().project;
  assert.equal(proj.lengthBars, 4);
  Object.keys(proj.clipsById).forEach(function (cid) {
    var cl = proj.clipsById[cid];
    assert.ok(cl.startBeat + cl.durationBeats <= 16);
  });

  // Invalid bars values rejected
  assert.equal(store.setLengthBars(3).ok, false);
  assert.equal(store.setLengthBars(64).ok, false);
  assert.equal(store.setLengthBars(8.5).ok, false);
});

test('15. undo, redo, and redo stack truncation upon a new mutating action', function () {
  var store = CueModel.createStore();
  var initialTempo = store.getState().project.tempo;
  var initialName = store.getState().project.name;

  store.setTempo(128);
  store.setProjectName('Edit One');
  store.deleteClip('clip-kick-3');

  assert.equal(store.getState().history.pastCount, 3);
  assert.equal(store.getState().history.futureCount, 0);

  // Undo 1: clip-kick-3 restored
  assert.equal(store.undo().ok, true);
  assert.ok(store.getState().project.clipsById['clip-kick-3']);
  assert.equal(store.getState().project.name, 'Edit One');

  // Undo 2: name restored
  assert.equal(store.undo().ok, true);
  assert.equal(store.getState().project.name, initialName);
  assert.equal(store.getState().project.tempo, 128);

  // Undo 3: tempo restored
  assert.equal(store.undo().ok, true);
  assert.equal(store.getState().project.tempo, initialTempo);
  assert.equal(store.canUndo(), false);
  assert.equal(store.undo().ok, false);

  // Redo 2 steps
  assert.equal(store.redo().ok, true);
  assert.equal(store.getState().project.tempo, 128);
  assert.equal(store.redo().ok, true);
  assert.equal(store.getState().project.name, 'Edit One');
  assert.equal(store.getState().history.futureCount, 1);

  // New mutating action MUST truncate redo stack
  store.setTrackVolume('trk-bass', 0.5);
  assert.equal(store.canRedo(), false);
  assert.equal(store.getState().history.futureCount, 0);
  assert.equal(store.redo().ok, false);
});

test('16. undo history is strictly bounded by maxHistory capacity', function () {
  var store = CueModel.createStore({ maxHistory: 4 });

  // Starting tempo is 124; apply 7 sequential changes: 100, 101, 102, 103, 104, 105, 106
  for (var t = 100; t <= 106; t++) {
    store.setTempo(t);
  }

  assert.equal(store.getState().project.tempo, 106);
  assert.equal(store.getState().history.pastCount, 4);

  // Undoing 4 times walks back: 105 -> 104 -> 103 -> 102
  var expected = [105, 104, 103, 102];
  expected.forEach(function (bpm) {
    assert.equal(store.undo().ok, true);
    assert.equal(store.getState().project.tempo, bpm);
  });

  // 5th undo is unavailable because oldest frames were dropped
  assert.equal(store.canUndo(), false);
  var extraUndo = store.undo();
  assert.equal(extraUndo.ok, false);
  assert.equal(extraUndo.reason, 'nothing_to_undo');
});

test('17. clip note/pattern editing, deterministic event expansion, and immutable state isolation', function () {
  var store = CueModel.createStore();

  // Toggle a new note on clip-kick-1 at step 2, pitch 36
  var beforeCount = store.getState().project.clipsById['clip-kick-1'].notes.length;
  var addNote = store.toggleClipNote('clip-kick-1', { step: 2, pitch: 36, velocity: 0.7, durationSteps: 1 });
  assert.equal(addNote.ok, true);
  assert.equal(addNote.clip.patternId, 'custom');
  assert.equal(addNote.clip.notes.length, beforeCount + 1);

  // Toggling same (step, pitch) removes it
  var remNote = store.toggleClipNote('clip-kick-1', { step: 2, pitch: 36 });
  assert.equal(remNote.ok, true);
  assert.equal(remNote.clip.notes.length, beforeCount);

  // Switch pattern preset
  var patRes = store.setClipPattern('clip-kick-1', 'kick-turnaround');
  assert.equal(patRes.ok, true);
  assert.equal(patRes.clip.patternId, 'kick-turnaround');

  // Event expansion determinism & mute/solo filtering
  var proj = store.getState().project;
  var evA = CueModel.expandProjectEvents(proj, { fromBeat: 0, toBeat: 8 });
  var evB = CueModel.expandProjectEvents(proj, { fromBeat: 0, toBeat: 8 });
  assert.deepEqual(evA, evB);
  assert.ok(evA.length > 0);
  evA.forEach(function (ev) {
    assert.ok(ev.beat >= 0 && ev.beat < 8);
  });

  store.setTrackSolo('trk-bass', true);
  var bassOnly = CueModel.expandProjectEvents(store.getState().project, { fromBeat: 0, toBeat: 16 });
  assert.ok(bassOnly.length > 0);
  assert.ok(bassOnly.every(function (ev) { return ev.trackId === 'trk-bass'; }));

  // External mutation of getState() snapshot must not corrupt store
  var leaked = store.getState();
  leaked.project.name = 'CORRUPTED';
  leaked.project.tracksById['trk-kick'].volume = 0;
  leaked.project.clipsById['clip-kick-1'].notes.length = 0;
  var fresh = store.getState();
  assert.notEqual(fresh.project.name, 'CORRUPTED');
  assert.ok(fresh.project.tracksById['trk-kick'].volume > 0);
  assert.ok(fresh.project.clipsById['clip-kick-1'].notes.length > 0);
});

test('18. stable JSON serialization round-trips identically and canonicalizes shuffled clip/note order', function () {
  var store = CueModel.createStore();
  store.setTempo(126.5);
  store.setProjectName('Canonical Session');
  store.resizeClip('clip-kick-1', { durationBeats: 14 });

  var json1 = store.exportJSON();
  var parsed = CueModel.deserializeProject(json1);
  assert.equal(parsed.ok, true);

  var json2 = CueModel.serializeProject(parsed.project);
  assert.equal(json1, json2);
  assert.deepEqual(CueModel.toPublicProject(parsed.project), store.exportPublicProject());

  // Shuffle clips array and notes arrays in public format -> re-validating yields identical canonical JSON
  var shuffled = JSON.parse(json1);
  shuffled.clips.reverse();
  shuffled.clips.forEach(function (cl) { cl.notes.reverse(); });

  var store2 = CueModel.createStore();
  var imp = store2.importJSON(JSON.stringify(shuffled));
  assert.equal(imp.ok, true);
  assert.equal(store2.exportJSON(), json1);
});
