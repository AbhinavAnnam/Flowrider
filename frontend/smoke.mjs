import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';

const base=process.env.DEMO_URL || 'http://127.0.0.1:8080';
const browser=await chromium.launch({headless:true, ...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
const page=await browser.newPage({viewport:{width:1440,height:1100}});
const errors=[];
page.on('pageerror',error=>errors.push(error.message));
page.on('console',message=>{if(message.type()==='error')errors.push(message.text());});
try {
  await page.goto(base);
  await page.getByRole('button',{name:'Phase 3 replay',exact:true}).click();
  await page.getByRole('heading',{name:'A clearer view of your network.'}).waitFor();
  assert.equal(await page.locator('.switch-node').count(),6);
  assert.equal(await page.locator('.link-label').count(),7);
  await page.getByRole('button',{name:'Next event',exact:true}).click();
  await page.waitForTimeout(300);
  let state=await (await fetch(base+'/api/state')).json();
  assert.equal(state.mode,'replay');
  assert(state.topology.links.some(l=>l.congestion_predicted));
  assert(state.routes.baseline && state.routes.proposed);
  assert(state.routes.changed);
  assert.equal(state.routes.forwarding_verified,false);
  await page.locator('.link-label').filter({hasText:'s1-s2'}).click();
  await page.getByRole('combobox',{name:'Inspect link'}).selectOption('s2-s5');
  await page.getByRole('button',{name:'Recalculate preview',exact:true}).click();
  await mkdir('test-results',{recursive:true});
  await page.screenshot({path:'test-results/dashboard-desktop.png',fullPage:true});
  await page.getByRole('button',{name:'Start replay',exact:true}).click();
  await page.waitForTimeout(500);
  const playing=await (await fetch(base+'/api/state')).json();
  assert(playing.playback.playing);
  assert(playing.playback.position > state.playback.position);
  await page.getByRole('button',{name:'Reset',exact:true}).click();
  await page.waitForTimeout(250);
  state=await (await fetch(base+'/api/state')).json();
  assert.equal(state.playback.position,0);
  await page.getByRole('button',{name:'LIVE',exact:true}).click();
  await page.getByText('LIVE · READ-ONLY NETWORK VIEW',{exact:true}).waitFor();
  state=await (await fetch(base+'/api/state')).json();
  assert.equal(state.mode,'live');
  assert.equal(state.health.telemetry,'unavailable');
  assert(state.topology.links.every(l=>l.current_utilization===null));
  await page.screenshot({path:'test-results/dashboard-live-unavailable.png',fullPage:true});
  await page.getByRole('button',{name:'REPLAY',exact:true}).click();
  for(const width of [320,768]){
    await page.setViewportSize({width,height:1000});
    await page.screenshot({path:`test-results/dashboard-${width}.png`,fullPage:true});
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth > window.innerWidth);
    assert.equal(overflow,false,`Page overflows at ${width}px`);
  }
  assert.deepEqual(errors,[]);
  console.log('PASS: real API data; 6 switches/7 links; Start/Reset/Next Event; route preview; replay/live separation; 320/768/1440 layouts; no browser errors.');
} finally {await browser.close();}
