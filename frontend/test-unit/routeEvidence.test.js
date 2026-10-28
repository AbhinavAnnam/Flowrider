import test from 'node:test';
import assert from 'node:assert/strict';
import {positiveProof,activeInstalledPath} from '../src/routeEvidence.js';

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
