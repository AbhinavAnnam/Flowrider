export function positiveProof(route,source) {
  const installed=route?.installed_route,observed=route?.observed_route;
  if(!installed?.path?.length||!installed.cookie_hex||installed.cookie_hex!==observed?.cookie_hex)return false;
  const mac=`00:00:00:00:00:${source.toString(16).padStart(2,'0')}`;
  const counts=(observed.counters||[]).filter(c=>c.source_mac===mac);
  return counts.length===installed.path.length&&new Set(counts.map(c=>c.dpid)).size===installed.path.length&&
    counts.every(c=>installed.path.includes(c.dpid)&&c.packet_count>0);
}

export const pathText=path=>path?.length?path.map(n=>String(n).startsWith('s')?n:`s${n}`).join(' → '):'Not recorded';

export function activeInstalledPath(snapshot,route){
  const path=route?.installed_route?.path;
  return Boolean(path?.length&&path.slice(1).every((v,i)=>snapshot?.topology?.links?.some(l=>
    (l.id||l.link_id)===`s${Math.min(path[i],v)}-s${Math.max(path[i],v)}`&&l.active===true)));
}

export function nextVerifiedReroute(trial,current){
  const changes=(trial?.events||[]).filter(e=>e.kind==='route_activated'&&e.reason==='predicted_congestion'&&
    e.verification==='openflow_flow_stats'&&e.cookie_hex&&e.previous_path?.length&&e.path?.length&&
    e.path.join(',')!==e.previous_path.join(',')&&e.forwarding_evidence?.minimum_hop_packets>0)
    .sort((a,b)=>Date.parse(a.timestamp)-Date.parse(b.timestamp));
  return changes.length?changes[(changes.indexOf(current)+1)%changes.length]:null;
}

export function eventSnapshotIndex(trial,event){
  if(!event)return null;
  const snapshots=trial?.snapshots||[];
  if(event.kind!=='route_activated'||!event.cookie_hex)
    return Number.isInteger(event.snapshot_index)&&snapshots[event.snapshot_index]?event.snapshot_index:null;
  const activated=Date.parse(event.timestamp),target=Date.parse(event.forwarding_evidence?.timestamp||event.timestamp);
  const matches=snapshots.map((snapshot,index)=>({snapshot,index,time:Date.parse(snapshot.timestamp)})).filter(({snapshot,time})=>
    time>=activated&&Math.abs(time-target)<=5000&&snapshot.routes.some(r=>r.route_id===event.route_id&&
      r.installed_route?.cookie_hex===event.cookie_hex&&r.installed_route.path.join(',')===event.path?.join(',')));
  return matches.sort((a,b)=>Math.abs(a.time-target)-Math.abs(b.time-target))[0]?.index??null;
}

export function recordedPair(snapshot,source,destination){
  const [a,b]=[source,destination].sort((u,v)=>u-v);
  return snapshot?.routes.find(r=>parseInt(r.source_mac.slice(-2),16)===a&&parseInt(r.destination_mac.slice(-2),16)===b);
}

export function recoveryPositions(recovery,source,destination){
  const snapshots=recovery.snapshots||[],failed=recovery.failed_links||[];
  const before=snapshots.findLastIndex(s=>Date.parse(s.timestamp)<Date.parse(recovery.down_timestamp));
  const down=snapshots.map((s,index)=>({s,index})).filter(({s})=>Date.parse(s.timestamp)>=Date.parse(recovery.down_timestamp)&&
    Date.parse(s.timestamp)<Date.parse(recovery.restored_timestamp)&&s.topology.links.some(l=>failed.includes(l.id)&&!l.active));
  const old=recordedPair(snapshots[before],source,destination)?.installed_route?.path;
  const replacement=down.find(({s})=>{const path=recordedPair(s,source,destination)?.installed_route?.path;return path&&old&&path.join(',')!==old.join(',');});
  const restored=snapshots.findLastIndex(s=>Date.parse(s.timestamp)>=Date.parse(recovery.restored_timestamp)&&
    failed.every(id=>s.topology.links.some(l=>l.id===id&&l.active)));
  return {before:before<0?null:before,down:replacement?.index??down[0]?.index??null,restored:restored<0?null:restored,
    invalidation:down.find(({s})=>!recordedPair(s,source,destination)?.installed_route)?.index??null};
}
