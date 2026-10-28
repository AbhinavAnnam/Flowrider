import {test,expect} from 'playwright/test';

const cookie='0x4652000000000041';
const mac=i=>`00:00:00:00:00:0${i}`;
const route={route_id:'pair-3-6',source_mac:mac(3),destination_mac:mac(6),baseline_path:[3,2,5,6],
  recommended_route:{path:[3,4,5,6],policy:'predicted'},installed_route:{path:[3,2,5,6],cookie_hex:cookie,reason:'initial_baseline'},
  observed_route:{path:[3,2,5,6],cookie_hex:cookie,method:'openflow_flow_stats',verified_timestamp:'2026-10-28T15:00:00Z',
    counters:[3,2,5,6].map(dpid=>({dpid,source_mac:mac(3),packet_count:12}))}};
const links=[[1,2],[2,3],[3,4],[4,5],[5,6],[1,6],[2,5]].map(([source,target])=>({id:`s${source}-s${target}`,source,target,active:true,capacity_mbps:source===2&&target===5?200:100,current_utilization:.9,predicted_utilization:.95,base_delay_ms:5}));
const snapshot={timestamp:'2026-10-28T15:00:00Z',topology_epoch:12,policy:{},routes:[route],
  topology:{nodes:Array.from({length:6},(_,i)=>({id:i+1,name:`s${i+1}`,host:`h${i+1}`,observed:true})),links}};
function evidence(){
  const metrics={average_ms:{label:'Average RTT',unit:'ms',baseline:50,predictive_ml:25,change_percent:-50,lower_is_better:true},
    congestion_link_seconds:{label:'Congestion exposure',unit:'link-seconds',baseline:200,predictive_ml:400,change_percent:100,lower_is_better:true}};
  const down=structuredClone(snapshot);down.timestamp='2026-10-28T15:00:05Z';down.topology.links.find(l=>l.id==='s2-s5').active=false;down.routes[0].installed_route=null;
  const restored=structuredClone(snapshot);restored.timestamp='2026-10-28T15:00:10Z';
  return {status:'available',experiment:'SYNTHETIC UI TEST ONLY',note:'Synthetic UI test fixture',comparison:metrics,
    provenance:{model_sha256:'fixture-only'},
    trials:[1,2,3,4].map((index)=>({index,mode:[1,4].includes(index)?'baseline':'predictive_ml',status:'passed',trial_id:`fixture-${index}`,
      metrics:{average_ms:25,congestion_link_seconds:400},routing:{tracked_changes_positive_hops:2},snapshots:[snapshot],
      verified_traffic_changes:[],backbone_history:[{timestamp:snapshot.timestamp,current:.9,predicted:.95,target_timestamp:'2026-10-28T15:00:15Z'}],stages:[],rtt_stages:[],events:[{
        kind:'route_activated',sequence:10,timestamp:snapshot.timestamp,route_id:'pair-3-6',cookie_hex:cookie,
        previous_path:[3,4,5,6],path:[3,2,5,6],reason:'predicted_congestion',verification:'openflow_flow_stats',snapshot_index:0,
        forwarding_evidence:{timestamp:snapshot.timestamp,minimum_hop_packets:12,actual_source_host:3}}]})),
    recovery:{status:'available',failed_links:['s2-s5'],down_timestamp:down.timestamp,restored_timestamp:restored.timestamp,snapshots:[snapshot,down,restored],events:[],source_directory:'synthetic-test-only'}};
}

test('missing evidence stays unavailable and never substitutes results',async({page})=>{
  await page.route('**/api/benchmark',r=>r.fulfill({json:{status:'unavailable',reason:'Benchmark data unavailable: prepare a package',trials:[],comparison:{}}}));
  await page.goto('/');
  await expect(page.getByText('Benchmark data unavailable: prepare a package')).toBeVisible();
  await expect(page.getByRole('button',{name:'Phase 3 replay'})).toBeVisible();
  await expect(page.getByText('50.348')).toHaveCount(0);
});

test('route recommendations stay separate from installed versions; failure and restoration are selectable',async({page})=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/api/benchmark',r=>r.fulfill({json:evidence()}));
  await page.goto('/');
  await expect(page.getByRole('heading',{name:'Performance comparison',exact:true})).toBeVisible();
  await page.getByLabel('Measured traffic pair').selectOption('3-6');
  await expect(page.getByRole('region',{name:'Recorded controller forwarding'}).first().getByTestId('recorded-recommended')).toContainText('s3 → s4 → s5 → s6');
  await expect(page.getByRole('region',{name:'Recorded controller forwarding'}).first().getByTestId('recorded-installed')).toContainText('s3 → s2 → s5 → s6');
  await page.getByRole('button',{name:/route_activated/}).first().click();
  await expect(page.getByTestId('selected-event')).toContainText(cookie);
  await expect(page.getByTestId('selected-event')).toContainText('Positive counters');
  await page.getByLabel('Recovery snapshot',{exact:true}).fill('1');
  await expect(page.getByTestId('recovery-topology').locator('[data-link="s2-s5"]')).toHaveAttribute('data-state','inactive');
  await page.getByLabel('Recovery snapshot',{exact:true}).fill('2');
  await expect(page.getByTestId('recovery-topology').locator('[data-link="s2-s5"]')).toHaveAttribute('data-state','recovered');
  await expect(page.getByText(/Congestion exposure increased under ML/)).toBeVisible();
  expect(errors).toEqual([]);
});

test('navigation and evidence remain usable on a small screen',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await page.route('**/api/benchmark',r=>r.fulfill({json:evidence()}));
  await page.goto('/');
  await expect(page.getByRole('link',{name:'Link Failure & Recovery',exact:true})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
});

test('Phase 3 replay and read-only live mode retain their controls',async({page})=>{
  await page.route('**/api/benchmark',r=>r.fulfill({json:{status:'unavailable',reason:'No benchmark in this test',trials:[],comparison:{}}}));
  await page.goto('/');
  await page.getByRole('button',{name:'Phase 3 replay'}).click();
  await expect(page.getByRole('button',{name:'Start replay',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Start replay',exact:true}).click();
  await expect(page.getByRole('button',{name:'Pause',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Pause',exact:true}).click();
  await page.getByRole('button',{name:'LIVE',exact:true}).click();
  await expect(page.getByText('LIVE · READ-ONLY NETWORK VIEW',{exact:true})).toBeVisible();
  await expect(page.getByText('LIVE · CONTROLLER READBACK',{exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:'Refresh live',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'Benchmark',exact:true}).click();
  await expect(page.getByRole('heading',{name:'Benchmark data unavailable'})).toBeVisible();
});
