// Explicitly opted-in real YouTube test. Use a dedicated unlisted test stream.
// Credentials stay in memory; restore the previous connection and pause the test schedule.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.REPLAY_PLAYWRIGHT_MODULE || 'playwright');
assert.equal(process.env.REPLAY_ALLOW_YOUTUBE_LIVE, '1', 'Explicit live-test opt-in required');
const web = 'http://127.0.0.1:13102';
const api = 'http://127.0.0.1:18092';
let watchUrl;
const evidencePath = process.env.REPLAY_LIVE_EVIDENCE || '/private/tmp/replay-auto-live-evidence.json';
const report = {started_at: new Date().toISOString(), headless: true, production_deployed: false,
  source: 'synthetic test pattern with 880Hz tone, 60 seconds', watch_url: watchUrl, viewer_samples: []};
const browser = await chromium.launch({headless: true, ...(process.env.REPLAY_CHROME_PATH ? {executablePath: process.env.REPLAY_CHROME_PATH} : {}),
  args: ['--autoplay-policy=no-user-gesture-required']});
const context = await browser.newContext({viewport:{width:1440,height:1000}});
const page = await context.newPage();
const viewerContext = await browser.newContext({viewport:{width:1280,height:900}});
const viewer = await viewerContext.newPage();
let authorization = '', ruleId, jobId, previousConnection, connectionChanged = false;
let baselineJobs = new Set();
page.on('request', request => {
  if(request.url().startsWith(api+'/api/') && request.headers().authorization) authorization=request.headers().authorization;
});
const request = async (path, options={}) => {
  const response = await context.request.fetch(api+'/api'+path,{...options,headers:{Authorization:authorization,
    Origin:web,'X-Replay-Client':'1',...options.headers}});
  assert.ok(response.ok(), `${path}: HTTP ${response.status()}`);
  return response.status() === 204 ? null : response.json();
};
const save = () => fs.writeFileSync(evidencePath,JSON.stringify(report,null,2),{mode:0o600});
try {
  await page.goto(web);
  await page.getByRole('button',{name:'개발용 미리보기로 접속',exact:true}).click();
  await page.locator('.connection').filter({hasText:'스튜디오 연결됨'}).waitFor({timeout:30000});
  const automationStatus = await request('/automations');
  assert.ok(automationStatus.youtube?.configured && automationStatus.youtube?.live_authorized, 'Connect YouTube broadcast management permission before testing');
  const existingRules = automationStatus.items;
  assert.ok(existingRules.every(item => !item.enabled && !item.active), 'Pause existing schedules before the live test');
  const jobs = await request('/broadcasts');
  assert.ok(jobs.every(item => ['completed','failed','stopped'].includes(item.state)), 'Another broadcast is active');
  baselineJobs = new Set(jobs.map(item => item.id));
  const existingConnections = await request('/stream-connections');
  if (existingConnections.some(item => item.target === 'youtube')) previousConnection = await request('/stream-connections/youtube/use', {method:'POST'});
  assert.ok(process.env.REPLAY_TEST_STREAM_KEY_FILE, 'Set the authorized stream key file path');
  let content=fs.readFileSync(process.env.REPLAY_TEST_STREAM_KEY_FILE,'utf8');
  let match=content.match(/^\s*(?:export\s+)?YOUTUBE_STREAM_KEY\s*=\s*(.+)$/m);
  assert.ok(match,'YouTube stream key missing');
  let key=match[1].trim();
  if ((key.startsWith('"') && key.endsWith('"')) || (key.startsWith("'") && key.endsWith("'"))) key=key.slice(1,-1);
  else key=key.replace(/\s+#.*$/,'');
  await request('/stream-connections/youtube',{method:'PUT',data:{server_url:'',stream_key:key}});
  connectionChanged = true;
  content=''; match=null; key='';
  await page.getByRole('navigation',{name:'스튜디오 메뉴'}).getByRole('link',{name:'자동 송출',exact:true}).click();
  const panel=page.getByRole('region',{name:'자동 송출',exact:true});
  await panel.getByRole('button',{name:'일정 만들기',exact:true}).click();
  const name='YouTube 실제 자동 송출 '+new Date().toISOString().slice(0,19);
  await panel.getByLabel('일정 이름',{exact:true}).fill(name);
  const media=(await request('/media')).find(item=>item.status==='ready'&&item.name==='auto-live-test-60s.mp4');
  assert.ok(media,'60-second fixture must be ready');
  await panel.getByRole('radio',{name:media.name,exact:true}).check();
  await panel.getByRole('checkbox',{name:'YouTube',exact:true}).check();
  const due=new Date(Date.now()+120000);
  await panel.getByLabel('송출 시간',{exact:true}).fill(due.toISOString().slice(11,16));
  await panel.getByLabel('시간대',{exact:true}).selectOption('UTC');
  await panel.getByRole('button',{name:'자동 송출 시작',exact:true}).click();
  await panel.getByRole('heading',{name,exact:true}).waitFor();
  const rule=(await request('/automations')).items.find(item=>item.name===name);
  assert.ok(rule && rule.next_run*1000<Date.now()+180000);
  ruleId=rule.id;
  Object.assign(report,{schedule_id:ruleId,scheduled_for:new Date(rule.next_run*1000).toISOString()});save();
  console.log(JSON.stringify({event:'schedule_created',scheduled_for:report.scheduled_for}));
  if (watchUrl) await viewer.goto(watchUrl,{waitUntil:'domcontentloaded',timeout:60000});
  for(const label of ['Reject all','모두 거부']) {
    const button=viewer.getByRole('button',{name:label,exact:true});
    if(await button.count()) await button.first().click();
  }
  const deadline=Date.now()+360000;
  let completed=false, screenshotCount=0, lastState='', lastViewerReload=0;
  while(Date.now()<deadline) {
    const current=(await request('/automations')).items.find(item=>item.id===ruleId);
    const run=current.history[0];
    const broadcasts=(await request('/broadcasts')).filter(item=>!baselineJobs.has(item.id)&&item.media_id===media.id&&item.target==='youtube');
    assert.ok(broadcasts.length<=1,'No duplicate live broadcasts');
    const job=broadcasts[0];
    if(job) {
      jobId=job.id;report.job={id:job.id,state:job.state,error_code:job.error_code,progress:job.progress};
      if(job.state!==lastState){console.log(JSON.stringify({event:'job_state',...report.job}));lastState=job.state;}
    }
    if(run) report.automation_run={id:run.id,state:run.state,error_code:run.error_code,youtube:run.youtube};
    if (!watchUrl && run?.youtube?.watch_url && run.youtube.live_confirmed) {
      watchUrl = run.youtube.watch_url;
      assert.match(watchUrl, /^https:\/\/www\.youtube\.com\/watch\?v=[\w-]{11}$/);
      report.watch_url = watchUrl;
      await viewer.goto(watchUrl,{waitUntil:'domcontentloaded',timeout:30000});
      for(const label of ['Reject all','모두 거부']) {
        const button=viewer.getByRole('button',{name:label,exact:true});
        if(await button.count()) await button.first().click();
      }
    }
    const sample=watchUrl ? await Promise.race([viewer.evaluate(()=>{
      const video=document.querySelector('video');
      const player=document.getElementById('movie_player');
      const response=player?.getPlayerResponse?.();
      if(video){video.muted=true;void video.play().catch(()=>{});}
      return {at:new Date().toISOString(),time:video?.currentTime||0,width:video?.videoWidth||0,height:video?.videoHeight||0,
        paused:video?.paused??true,ready:video?.readyState||0,frames:video?.getVideoPlaybackQuality?.().totalVideoFrames||0,
        player_state:player?.getPlayerState?.(),playability:response?.playabilityStatus?.status,
        video_id:response?.videoDetails?.videoId,is_live:response?.videoDetails?.isLive,
        live_content:response?.videoDetails?.isLiveContent};
    }).catch(()=>({at:new Date().toISOString(),unavailable:true})), new Promise(resolve=>setTimeout(()=>resolve({at:new Date().toISOString(),unavailable:true,reason:'viewer_timeout'}),5000))]) : {at:new Date().toISOString(),watch_url_missing:true};
    sample.platform_state = run?.youtube?.state;
    report.viewer_samples.push(sample);
    // An upcoming page may retain LIVE_STREAM_OFFLINE after the platform starts.
    if (watchUrl && run?.youtube?.state === 'live' && sample.playability === 'LIVE_STREAM_OFFLINE' && Date.now()-lastViewerReload>10000) {
      lastViewerReload=Date.now();
      await viewer.reload({waitUntil:'domcontentloaded',timeout:20000});
    }
    if(sample.width>0 && !sample.paused && screenshotCount<3){
      await viewer.screenshot({path:`/private/tmp/replay-auto-live-viewer-${++screenshotCount}.png`});
    }
    save();
    if(run?.state==='failed'||job?.state==='failed') throw new Error('Broadcast failed: '+(job?.error_code||run?.error_code));
    if(run?.state==='completed'){
      completed=true;
      assert.ok(run.youtube?.live_confirmed && run.youtube?.state === 'complete', 'YouTube must confirm actual start and completion');
      assert.ok(watchUrl, 'Managed broadcast watch URL missing');
      const expectedVideoId = new URL(watchUrl).searchParams.get('v');
      const playing=report.viewer_samples.filter(s=>s.width>0&&!s.paused&&s.frames>0&&s.video_id===expectedVideoId&&(s.is_live===true||s.platform_state==='live'));
      if(playing.length>=2 && playing.at(-1).frames>playing[0].frames){report.viewer_playback_observed=true;break;}
    }
    await new Promise(resolve=>setTimeout(resolve,4000));
  }
  assert.ok(completed,'Automation did not complete before deadline');
  report.sender_completed=true;
  assert.ok(report.viewer_playback_observed,'No evidence of moving YouTube playback');
  await page.reload();
  await page.getByRole('region',{name:'자동 송출',exact:true}).waitFor();
  await page.screenshot({path:'/private/tmp/replay-auto-live-automation.png',fullPage:true});
  report.passed=!!report.viewer_playback_observed;
  report.status=report.passed?'sender_and_playback_verified':'sender_verified_playback_unverified';save();
}catch(error){report.passed=false;report.error=String(error.message).slice(0,300);save();process.exitCode=1;
}finally{
  report.cleanup = {};
  try {
    if(ruleId) {
      await request('/automations/'+ruleId,{method:'PUT',data:{enabled:false}});
      report.cleanup.schedule_paused = true;
    }
    if(jobId && !['completed','failed','stopped'].includes(report.job?.state)) {
      await request('/broadcasts/'+jobId+'/stop',{method:'POST'});
      report.cleanup.stop_requested = true;
    }
    if(connectionChanged) {
      if(previousConnection) await request('/stream-connections/youtube',{method:'PUT',data:{
        server_url:previousConnection.server_url,stream_key:previousConnection.stream_key,channel_url:previousConnection.channel_url || ''}});
      else await request('/stream-connections/youtube',{method:'DELETE'});
      report.cleanup.connection_restored = true;
    }
  } catch { report.cleanup.failed = true; process.exitCode=1; }
  previousConnection = undefined;
  report.finished_at=new Date().toISOString();save();
  await browser.close();
  console.log(JSON.stringify({passed:report.passed,status:report.status,error:report.error,cleanup:report.cleanup,evidence:evidencePath}));
}
