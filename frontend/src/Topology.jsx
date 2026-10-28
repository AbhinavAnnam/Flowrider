import React from 'react';

const positions = {1:[120,120],2:[360,70],3:[600,120],4:[600,340],5:[360,390],6:[120,340]};
const hosts = {1:[66,65],2:[360,16],3:[654,65],4:[654,395],5:[360,444],6:[66,395]};
export const percent = value => value == null ? '—' : `${Math.round(value * 100)}%`;

export const pathLinks = path => (path||[]).slice(1).map((v,i)=>{
  const u=Number(String(path[i]).replace('s','')),w=Number(String(v).replace('s',''));
  return `s${Math.min(u,w)}-s${Math.max(u,w)}`;
});

export default function Topology({ topology, routes={}, selected, onSelect=()=>{}, onNode=()=>{}, verifiedPath=[], previousPath=[], recoveredLinks=[] }) {
  const baseline = new Set(routes.baseline?.links || []);
  const proposed = new Set(routes.proposed?.links || []);
  const verified=new Set(pathLinks(verifiedPath)),previous=new Set(pathLinks(previousPath));
  return <div className="topology-scroll"><svg className="topology" viewBox="0 0 720 470" aria-label="Six-switch, seven-link FlowRider topology">
    <defs><pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".8" fill="var(--border)" /></pattern></defs>
    <rect width="720" height="470" fill="url(#dots)" />
    {topology.links.map(link => {
      const [x1,y1] = positions[link.source], [x2,y2] = positions[link.target];
      const recovered=recoveredLinks.includes(link.id)&&link.active===true;
      const color = link.active !== true ? 'var(--muted)' : recovered?'var(--green)':link.congested ? 'var(--red)' : link.congestion_predicted ? 'var(--amber)' : 'var(--line)';
      return <g key={link.id} data-link={link.id} data-state={link.active===false?'inactive':recovered?'recovered':link.active===true?'active':'unknown'}>
        <title>{link.id}: {link.active===false?'inactive / failed':recovered?'restored in recorded topology':link.active===true?'active':'unknown'}{verified.has(link.id)?' · verified installed path':''}</title>
        {previous.has(link.id)&&<line className="previous-path" x1={x1-5} y1={y1-5} x2={x2-5} y2={y2-5} stroke="var(--transition)" strokeWidth="3" strokeDasharray="3 6"/>}
        {baseline.has(link.id) && <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--cyan)" strokeWidth="12" opacity=".22" />}
        <line x1={x1} y1={y1} x2={x2} y2={y2} stroke={color} strokeWidth={selected === link.id ? 4 : 2.5} strokeDasharray={link.active === false ? '4 6' : undefined} />
        {proposed.has(link.id) && <line x1={x1+5} y1={y1+3} x2={x2+5} y2={y2+3} stroke="var(--amber)" strokeWidth="2" strokeDasharray="7 6" />}
        {verified.has(link.id)&&link.active===true&&<line className="verified-path" x1={x1-4} y1={y1+4} x2={x2-4} y2={y2+4} stroke="var(--green)" strokeWidth="4"/>}
      </g>;
    })}
    {topology.links.map(link => {
      const [a,b] = positions[link.source], [c,d] = positions[link.target];
      const x=(a+c)/2, y=(b+d)/2;
      return <g key={link.id} role="button" tabIndex="0" aria-label={`${link.id}: ${percent(link.current_utilization)} measured, ${percent(link.predicted_utilization)} predicted. Inspect link.`}
        className={`link-label ${selected === link.id ? 'selected' : ''}`} onClick={() => onSelect(link.id)}
        onKeyDown={e => {if (['Enter',' '].includes(e.key)) {e.preventDefault();onSelect(link.id);}}}>
        <rect x={x-56} y={y-23} width="112" height="46" rx="7" />
        <text x={x} y={y-5} textAnchor="middle" className={link.congested ? 'danger-text' : ''}>{link.active===false?'INACTIVE':recoveredLinks.includes(link.id)?'RESTORED':percent(link.current_utilization)} <tspan className="forecast-text">{link.active!==false&&!recoveredLinks.includes(link.id)&&`→ ${percent(link.predicted_utilization)}`}</tspan></text>
        <text x={x} y={y+12} textAnchor="middle" className="capacity-label">{link.id} · {link.capacity_mbps} Mbps</text>
      </g>;
    })}
    {topology.nodes.map(node => {
      const [x,y]=positions[node.id], [hx,hy]=hosts[node.id];
      return <g key={node.id}>
        <line x1={x} y1={y} x2={hx} y2={hy} stroke="var(--border-bright)" strokeDasharray="3 5" />
        <rect x={hx-19} y={hy-10} width="38" height="20" rx="4" fill="var(--surface)" stroke="var(--border-bright)" />
        <text x={hx} y={hy+4} textAnchor="middle" className="host-label">{node.host}</text>
        <g role="button" tabIndex="0" aria-label={`Switch ${node.name}, select as route destination`} className="switch-node" onClick={() => onNode(node.id)}
          onKeyDown={e=>{if(['Enter',' '].includes(e.key)){e.preventDefault();onNode(node.id);}}}>
          <circle cx={x} cy={y} r="29" className={node.observed ? 'observed' : ''} />
          <rect x={x-13} y={y-11} width="26" height="15" rx="3" fill="none" stroke="var(--cyan)" strokeWidth="1.7" />
          <path d={`M${x-8} ${y-6}h4m4 0h4m-16 5h4m4 0h4`} stroke="var(--cyan)" strokeWidth="1.8" />
          <text x={x} y={y+17} textAnchor="middle">{node.name}</text>
          <circle cx={x+22} cy={y-19} r="4" fill={node.observed ? 'var(--cyan)' : 'var(--muted)'} />
        </g>
      </g>;
    })}
  </svg></div>;
}
