const A = require('node:assert/strict');
const M = require('./model.js');
let n = 2718281;
function random(bound) { n = (Math.imul(n,1664525) + 1013904223) >>> 0; return Math.floor(n/4294967296 * bound); }
const store=M.createStore(); let changed=0,rejected=0,undoChecks=0,imports=0,maxClips=0;
for (let i=0;i<2500;i++) {
 const input=store.exportPublicProject(), track=input.tracks[random(input.tracks.length)], clip=input.clips[random(input.clips.length)]||{id:'none'};
 const before=store.exportJSON(); let result;
 switch(random(13)){
 case 0:case 1:case 2:result=store.createClip({trackId:track.id,startBeat:random(input.lengthBars*16)/4,durationBeats:(2+random(31))/4,name:'Cue '+i});break;
 case 3:result=store.moveClip(clip.id,{startBeat:random(input.lengthBars*16)/4});break;
 case 4:result=store.resizeClip(clip.id,{durationBeats:(2+random(36))/4});break;
 case 5:result=store.duplicateClip(clip.id);break;
 case 6:result=store.deleteClip(clip.id);break;
 case 7:result=store.setTrackMute(track.id,!track.mute);break;
 case 8:result=store.setTrackSolo(track.id,!track.solo);break;
 case 9:result=store.setTrackVolume(track.id,random(11)/10);break;
 case 10:result=store.renameClip(clip.id,'Phrase '+i);break;
 case 11:result=store.setTempo(40+random(201));break;
 case 12:result=store.importJSON('{"schemaVersion":1,"tracks":[]}');imports++;A.equal(result.ok,false);break;
 }
 const after=store.exportJSON(); const v=M.validateProject(JSON.parse(after)); A.ok(v.ok,JSON.stringify(v.errors));
 const state=JSON.parse(after); maxClips=Math.max(state.clips.length,maxClips);
 A.equal(new Set(state.clips.map(c=>c.id)).size,state.clips.length);
 for (const t of state.tracks){
   const cs=state.clips.filter(c=>t.id===c.trackId).sort((a,b)=>a.startBeat-b.startBeat);
   let end=0;
   for(const c of cs){A.ok(c.startBeat>=end);A.ok(c.durationBeats>=.5);end=c.startBeat+c.durationBeats;A.ok(end<=state.lengthBars*4);}
 }
 if(!result.ok){rejected++;A.equal(before,after);}
 if(result.changed){
  changed++; if(i%7===0){A.equal(store.undo().ok,true);A.equal(store.exportJSON(),before);A.equal(store.redo().ok,true);A.equal(store.exportJSON(),after);undoChecks++;}
 }
 if(i%83===0){const clone=M.createStore(); A.equal(clone.importJSON(after).ok,true);A.deepEqual(JSON.parse(clone.exportJSON()),JSON.parse(after));}
}
console.log(JSON.stringify({seed:2718281,actions:2500,changed,rejected,undoChecks,rejectedImports:imports,maxClips}));
