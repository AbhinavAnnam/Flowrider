import React from 'react';

export const timeLabel = value => value ? new Intl.DateTimeFormat('en-IN', {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Kolkata'}).format(new Date(value)) : '—';

export default function UtilChart({ history, clock }) {
  const end = new Date(clock).getTime()/1000, start=end-120;
  const x=t=>48+(t-start)/135*650, y=v=>160-v*125;
  const measured=history.map(p=>[new Date(p.timestamp).getTime()/1000,p.current]);
  const predicted=history.filter(p=>p.predicted!=null&&p.target_timestamp).map(p=>[new Date(p.target_timestamp).getTime()/1000,p.predicted]);
  const points=rows=>rows.map(([t,v])=>`${x(t)},${y(v)}`).join(' ');
  return <svg className="history-chart" viewBox="0 0 720 205" role="img" aria-label="Measured utilization and saved forecasts at their target timestamps">
    {[0,.4,.8,1].map(v=><g key={v}><line x1="48" x2="698" y1={y(v)} y2={y(v)} stroke={v===.8?'var(--amber)':'var(--border)'} strokeDasharray={v===.8?'4 5':'2 4'} opacity={v===.8?.5:1}/><text x="37" y={y(v)+4} textAnchor="end">{Math.round(v*100)}%</text></g>)}
    <defs><clipPath id="chart-clip"><rect x="48" y="23" width="650" height="140" /></clipPath></defs>
    <g clipPath="url(#chart-clip)">
      <polyline points={points(measured)} fill="none" stroke="var(--cyan)" strokeWidth="2.5" />
      <polyline points={points(predicted)} fill="none" stroke="var(--amber)" strokeWidth="2" strokeDasharray="6 4" />
      <line x1={x(end)} x2={x(end)} y1="28" y2="164" stroke="var(--muted)" strokeDasharray="3 4" />
    </g>
    {[start,start+60,end].map(t=><text key={t} x={x(t)} y="186" textAnchor="middle">{timeLabel(new Date(t*1000).toISOString())}</text>)}
    <text x="696" y="186" textAnchor="end">+15s</text>
    {!history.length&&<text x="370" y="100" textAnchor="middle">No valid measurements in this time window</text>}
  </svg>;
}
