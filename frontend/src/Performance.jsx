import React from 'react';

export const number=value=>value==null?'—':value.toLocaleString('en-IN',{minimumFractionDigits:3,maximumFractionDigits:3});

export default function Performance({evidence}) {
  const metrics=Object.entries(evidence.comparison);
  return <section id="performance" className="benchmark-section" aria-labelledby="performance-title">
    <div className="section-heading"><div><span className="eyebrow">MEASURED OUTCOMES</span><h2 id="performance-title">Performance comparison</h2></div><span className="chip">ABBA · 2 trials per mode</span></div>
    <p className="section-copy">Controlled six-switch Mininet experiment. Equal-weight trial means; descriptive comparison, not statistical significance.</p>
    <div className="comparison-grid">{metrics.map(([key,m])=>{
      const max=Math.max(m.baseline,m.predictive_ml,1);
      const unfavorable=m.change_percent!=null&&(m.lower_is_better?m.change_percent>0:m.change_percent<0);
      return <article className="comparison-metric" key={key}>
        <div className="comparison-heading"><h3>{m.label}</h3><span className={unfavorable?'delta unfavorable':'delta'}>{m.change_percent==null?'No relative change defined':`${m.change_percent>0?'+':''}${m.change_percent.toFixed(2)}%`}</span></div>
        <div className="comparison-bars" role="img" aria-label={`${m.label}: baseline ${number(m.baseline)} ${m.unit}; ML ${number(m.predictive_ml)} ${m.unit}`}>
          {[['baseline','Static Dijkstra'],['predictive_ml','Predictive ML']].map(([mode,label])=><div className="bar-row" key={mode}><span>{label}</span><div className="bar-track"><i className={`bar ${mode}`} style={{width:`${m[mode]/max*100}%`}}/></div><strong>{number(m[mode])}<small> {m.unit}</small></strong></div>)}
        </div>
      </article>;
    })}</div>
    {evidence.comparison.congestion_link_seconds?.change_percent>0&&<p className="evidence-warning">Congestion exposure increased under ML by {evidence.comparison.congestion_link_seconds.change_percent.toFixed(2)}%. This result is retained alongside RTT, loss and goodput.</p>}
    <div className="table-scroll"><table className="trial-table"><caption>Individual validated trials · original ABBA order</caption><thead><tr><th scope="col">Trial / mode</th>{metrics.map(([key,m])=><th scope="col" key={key}>{m.label}<small>{m.unit}</small></th>)}<th scope="col">Verified traffic changes</th></tr></thead>
      <tbody>{evidence.trials.map(t=><tr key={t.index}><th scope="row">{t.index.toString().padStart(2,'0')} · {t.mode==='baseline'?'Baseline':'Predictive ML'}<small>Passed validation</small></th>{metrics.map(([key])=><td key={key}>{number(t.metrics[key])}</td>)}<td>{t.routing.tracked_changes_positive_hops}</td></tr>)}</tbody></table></div>
  </section>;
}
