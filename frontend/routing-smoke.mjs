// UI-only fixtures; these are not evidence of a live routing experiment.
import {chromium} from 'playwright';
import assert from 'node:assert/strict';
import {mkdir} from 'node:fs/promises';

const base=process.env.DEMO_URL||'http://127.0.0.1:18080';
const browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
const page=await browser.newPage({viewport:{width:1440,height:1100}});
const errors=[];
page.on('pageerror',e=>errors.push(e.message));
let mode='fresh';
const cookie='0x465200000000007b';
const route={route_id:'test-pair',source_mac:'00:00:00:00:00:01',destination_mac:'00:00:00:00:00:04',
  status:'active',guarded:false,baseline_path:[1,2,5,4],recommended_route:{path:[1,6,5,4]},
  installed_route:{path:[1,6,5,4],cookie_hex:cookie,reason:'predicted_congestion'},
  observed_route:{path:[1,6,5,4],cookie_hex:cookie,method:'openflow_flow_stats',verified_timestamp:new Date().toISOString(),
    counters:[1,6,5,4].map(dpid=>({dpid,source_mac:'00:00:00:00:00:01',packet_count:7}))}};
try {
  await fetch(base+'/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'mode',mode:'live'})});
  await page.route('**/api/routing',async request=>{
    if(mode==='error')return request.fulfill({status:503,body:'Unavailable'});
    const record=structuredClone(route);
    if(mode==='mismatch')record.observed_route.cookie_hex='0x465200000000007c';
    await request.fulfill({json:{status:mode==='stale'?'stale':'available',stale:mode==='stale',source:'live_mininet',snapshot_age_seconds:mode==='stale'?60:0,
      snapshot:{routes:[record]},events:[{sequence:1,timestamp:new Date().toISOString(),route_id:'test-pair',kind:'route_activated',reason:'predicted_congestion'}]}});
  });
  await page.goto(base);
  await page.getByRole('button',{name:'Live',exact:true}).click();
  const panel=page.getByRole('region',{name:'Controller forwarding'});
  await panel.getByText('Positive counters at every hop for the installed cookie',{exact:false}).waitFor();
  assert.match(await panel.innerText(),/s1 → s6 → s5 → s4/);
  assert.match(await panel.innerText(),/0x465200000000007b/);
  await mkdir('test-results',{recursive:true});
  await page.screenshot({path:'test-results/controller-routing.png',fullPage:true});
  mode='mismatch';
  await panel.getByText('Current forwarding is not verified by this view.',{exact:false}).waitFor();
  mode='stale';
  await panel.getByText('Last recorded installed',{exact:true}).waitFor();
  assert.equal(await panel.getByText('Positive counters at every hop',{exact:false}).count(),0);
  mode='error';
  await panel.getByText('Controller routing API unavailable',{exact:true}).waitFor();
  assert.equal(await panel.getByText('s1 → s6 → s5 → s4',{exact:true}).count(),0);
  await page.getByRole('button',{name:'REPLAY',exact:true}).click();
  await panel.getByText('Replay shows calculated routes.',{exact:false}).waitFor();
  await page.getByRole('button',{name:'LIVE',exact:true}).click();
  mode='fresh';
  await panel.getByText('Positive counters at every hop',{exact:false}).waitFor();
  for(const width of [320,768]){
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`Overflow at ${width}`);
  }
  assert.deepEqual(errors,[]);
  console.log('PASS: UI fixtures cover actual/preview separation, exact cookies, stale/error clearing, live/replay, mobile layout, no page errors.');
} finally {await browser.close();}
