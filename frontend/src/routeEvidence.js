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
