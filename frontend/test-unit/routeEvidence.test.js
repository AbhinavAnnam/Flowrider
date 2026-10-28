import test from 'node:test';
import assert from 'node:assert/strict';
import {positiveProof,activeInstalledPath,nextVerifiedReroute,eventSnapshotIndex,recoveryPositions} from '../src/routeEvidence.js';

const route=()=>({installed_route:{cookie_hex:'0x4652000000000041',path:[3,2,5,6]},observed_route:{cookie_hex:'0x4652000000000041',
  counters:[3,2,5,6].map(dpid=>({dpid,source_mac:'00:00:00:00:00:03',packet_count:10}))}});
test('recommendations alone are never forwarding evidence',()=>{
  assert.equal(positiveProof({recommended_route:{path:[3,2,5,6]}},3),false);
});
test('exact cookies, source direction and every unique hop are required',()=>{
  assert.equal(positiveProof(route(),3),true);
  for(const fault of ['cookie','duplicate','zero','direction']){
    const r=route();
    if(fault==='cookie')r.observed_route.cookie_hex='0x4652000000000042';
    if(fault==='duplicate')r.observed_route.counters[3].dpid=3;
    if(fault==='zero')r.observed_route.counters[3].packet_count=0;
    if(fault==='direction')r.observed_route.counters[3].source_mac='00:00:00:00:00:06';
    assert.equal(positiveProof(r,3),false,fault);
  }
});
test('a failed path cannot be highlighted as verified by earlier positive counters',()=>{
  const snap={topology:{links:[{id:'s2-s3',active:true},{id:'s2-s5',active:false},{id:'s5-s6',active:true}]}};
  assert.equal(activeInstalledPath(snap,route()),false);
  snap.topology.links[1].active=true;
  assert.equal(activeInstalledPath(snap,route()),true);
});

test('next verified reroute cycles only proven ML transitions, including a late OVS proof',()=>{
  const event=(n)=>({sequence:n,kind:'route_activated',timestamp:`2026-10-28T15:00:0${n}Z`,reason:'predicted_congestion',
    cookie_hex:`0x${n}`,route_id:'pair',previous_path:[3,2,5,6],path:[3,4,5,6],verification:'openflow_flow_stats',
    forwarding_evidence:{minimum_hop_packets:9,timestamp:`2026-10-28T15:00:0${n+1}Z`},snapshot_index:null});
  const first=event(1),second=event(3),unproven={...event(2),forwarding_evidence:null};
  const snapshot={timestamp:'2026-10-28T15:00:03.100Z',routes:[{route_id:'pair',installed_route:{cookie_hex:'0x3',path:[3,4,5,6]}}]};
  const trial={events:[first,unproven,second],snapshots:[snapshot]};
  assert.equal(nextVerifiedReroute(trial,null),first);assert.equal(nextVerifiedReroute(trial,first),second);
  assert.equal(nextVerifiedReroute(trial,second),first);
  assert.equal(eventSnapshotIndex(trial,second),0,'actual installed snapshot before independent proof is retained');
  snapshot.routes[0].installed_route.cookie_hex='0x4';assert.equal(eventSnapshotIndex(trial,second),null);
  assert.equal(nextVerifiedReroute({events:[unproven]},null),null);
});

test('recovery shortcuts select actual before, replacement and restored snapshots',()=>{
  const pair=path=>({source_mac:'00:00:00:00:00:02',destination_mac:'00:00:00:00:00:05',installed_route:path?{path}:null});
  const snap=(second,active,path)=>({timestamp:`2026-10-28T15:00:0${second}Z`,routes:[pair(path)],topology:{links:[{id:'s2-s5',active}]}});
  const recovery={failed_links:['s2-s5'],down_timestamp:'2026-10-28T15:00:01Z',restored_timestamp:'2026-10-28T15:00:03Z',
    snapshots:[snap(0,true,[2,5]),snap(1,false,null),snap(2,false,[2,1,6,5]),snap(3,true,null),snap(4,true,[2,5])]};
  assert.deepEqual(recoveryPositions(recovery,2,5),{before:0,down:2,restored:4,invalidation:1});
  assert.equal(recoveryPositions({snapshots:[]},2,5).down,null);
});
