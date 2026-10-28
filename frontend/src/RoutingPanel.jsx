import React, {useEffect, useState} from 'react';
import {timeLabel} from './UtilChart.jsx';
import {positiveProof,pathText,activeInstalledPath} from './routeEvidence.js';

const mac = host => `00:00:00:00:00:${host.toString(16).padStart(2,'0')}`;

export default function RoutingPanel({mode, source, destination, recordedState}) {
  const [state,setState]=useState(null),[error,setError]=useState(''),[now,setNow]=useState(Date.now());
  useEffect(()=>{
    setState(null);setError('');
    if(mode!=='live') return;
    let active=true,sequence=0;
    const abort=new AbortController();
    async function refresh() {
      const version=++sequence;setNow(Date.now());
      try {
        const response=await fetch('/api/routing',{signal:abort.signal});
        if(!response.ok) throw Error('Controller routing API unavailable');
        const value=await response.json();
        if(active&&version===sequence){setState({...value,receivedAt:Date.now()});setError('');}
      } catch(e) {if(active&&version===sequence){setState(null);setError(e.message);}}
    }
    refresh();const timer=setInterval(refresh,2000);
    return()=>{active=false;abort.abort();clearInterval(timer);};
  },[mode]);
  if(recordedState){
    const [a,b]=[mac(source),mac(destination)].sort();
    const route=recordedState.routes.find(r=>r.source_mac===a&&r.destination_mac===b);
    const path=nodes=>pathText(source>destination&&nodes?[...nodes].reverse():nodes);
    const positive=positiveProof(route,source)&&activeInstalledPath(recordedState,route);
    return <section className="controller-routing" aria-label="Recorded controller forwarding"><h3>Recorded controller forwarding · h{source} → h{destination}</h3>
      <p>Snapshot: <time dateTime={recordedState.timestamp} title={recordedState.timestamp}>{timeLabel(recordedState.timestamp)} IST</time>. Historical evidence, not current switch state.</p>
      <dl className="routing-records"><dt>Static Dijkstra baseline</dt><dd>{path(route?.baseline_path)}</dd>
        <dt>ML recommendation only</dt><dd data-testid="recorded-recommended">{path(route?.recommended_route?.path)}</dd>
        <dt>Installed rule version</dt><dd data-testid="recorded-installed">{path(route?.installed_route?.path)}</dd>
        <dt>OpenFlow readback</dt><dd>{path(route?.observed_route?.path)}</dd></dl>
      <p>{positive?'Positive counters on every hop for this installed cookie.':route?.installed_route?'Installed rules reported; positive forwarding evidence is incomplete.':'No installed route in this snapshot; a recommendation does not establish forwarding.'}</p>
      <p>Cookie: <code>{route?.installed_route?.cookie_hex||'Not recorded'}</code> · {route?.installed_route?.reason||'No installation reason recorded'}</p>
      {route?.observed_route?.verified_timestamp&&<p>Readback timestamp: <code>{route.observed_route.verified_timestamp}</code></p>}
    </section>;
  }
  if(mode!=='live') return <section className="controller-routing" aria-label="Controller forwarding"><h3>Controller forwarding</h3><p>Replay shows calculated routes. Select Live to inspect actual controller forwarding.</p></section>;
  const [a,b]=[mac(source),mac(destination)].sort();
  const route=state?.snapshot?.routes?.find(r=>r.source_mac===a&&r.destination_mac===b);
  const fresh=state?.status==='available'&&!state.stale&&state.source==='live_mininet'&&
    state.snapshot_age_seconds!=null&&state.snapshot_age_seconds+Math.max(0,now-state.receivedAt)/1000<=5;
  const installed=route?.installed_route,observed=route?.observed_route;
  const exact=Boolean(installed?.cookie_hex&&observed?.cookie_hex&&installed.cookie_hex===observed.cookie_hex);
  const counts=exact?(observed.counters||[]).filter(c=>c.source_mac===mac(source)):[];
  const positive=positiveProof(route,source);
  const path=nodes=>nodes?(source>destination?[...nodes].reverse():nodes).map(n=>`s${n}`).join(' → '):'Not reported';
  return <section className="controller-routing" aria-label="Controller forwarding">
    <h3>Controller forwarding · h{source} → h{destination}</h3>
    <p role="status">{error||(!state?'Loading controller routing…':!fresh?'Stale or unavailable · last recorded state':route?`Controller state: ${route.status}`:'No controller route recorded for this pair')}</p>
    <dl className="routing-records">
      <dt>Baseline</dt><dd>{path(route?.baseline_path)}</dd>
      <dt>ML recommendation</dt><dd>{path(route?.recommended_route?.path)}</dd>
      <dt>{fresh?'Installed':'Last recorded installed'}</dt><dd>{path(installed?.path)}</dd>
      <dt>{fresh?'Observed OpenFlow':'Last recorded OpenFlow'}</dt><dd>{path(observed?.path)}</dd>
    </dl>
    {installed&&<p>Installation reason: {installed.reason} · cookie {installed.cookie_hex||'Not reported'}{route.guarded?' · ingress guarded':''}</p>}
    <p>{fresh&&positive?'Positive counters at every hop for the installed cookie':fresh&&exact?'Rules read back; positive forwarding counters are incomplete':'Current forwarding is not verified by this view.'} End-to-end delivery requires a host test.</p>
    {counts.length>0&&<p>Recorded counters: {counts.map(c=>`s${c.dpid}: ${c.packet_count.toLocaleString()} packets`).join(' · ')}. Counters can carry over on replaced rules.</p>}
    {observed&&<p>Readback: {observed.method} · {timeLabel(observed.verified_timestamp)} IST</p>}
    <h4>Latest controller events for this pair</h4>
    <ul className="routing-events">{(state?.events||[]).filter(e=>e.route_id===route?.route_id).slice(0,6).map(e=><li key={e.sequence}>{timeLabel(e.timestamp)} IST · {e.kind}{e.reason?` · ${e.reason}`:''}{e.confirmation_count?` · confirmation ${e.confirmation_count}`:''}</li>)}</ul>
    {!state?.events?.some(e=>e.route_id===route?.route_id)&&<p>No matching events in the current event window.</p>}
  </section>;
}
