import {test} from 'node:test';
import assert from 'node:assert/strict';
import {PolicyError,UpstreamError} from '../src/github.mjs';
import {HarnessAdmission,INTERVAL,REFRESH,RETAIN,entry,parseSource} from '../src/harness.mjs';

const OLD='a'.repeat(40), NEW='b'.repeat(40), STRAY='c'.repeat(40), RUNNING='d'.repeat(40);
const main=w=>'TauCetiProject/TauCeti/.github/workflows/'+w+'@refs/heads/main';
const FLEET=[main('pr-build.yml'),main('fleet-smoke.yml')];
const CONTROL=[main('pr-build.yml'),main('pick-runner.yml'),main('publish-lake-cache.yml')];

function source(revision,changes={}) {
  return JSON.stringify({schema:'tauceti-ci.source-candidate/v2',
    execution:{forge:{kind:'github',url:'https://github.com'},name:'TauCetiProject/TauCeti',id:1256881326},
    workflow:'.github/workflows/source-candidate.yml',
    harness:{forge:{kind:'github',url:'https://github.com'},name:'TauCetiProject/TauCetiTools',id:1426390374},
    harness_revision:revision,...changes});
}
function fixture({revision=NEW,fleet=[...FLEET,entry('pr-build.yml',OLD)],
  control=[...CONTROL,entry('pr-build.yml',OLD),entry('publish-lake-cache.yml',OLD)],reviewed=[OLD,NEW,RUNNING],running=new Set()}={}) {
  let clock=1_000_000_000_000, alarm=null;
  const records=new Map();
  const storage={get:async k=>structuredClone(records.get(k)),put:async(k,v)=>records.set(k,structuredClone(v)),
    getAlarm:async()=>alarm,setAlarm:async v=>{alarm=v;}};
  const groups={3:{id:3,name:'TauCetiFleet',restricted_to_workflows:true,selected_workflows:fleet},
    6:{id:6,name:'TauCetiQueueControl',restricted_to_workflows:true,selected_workflows:control}};
  const calls={patch:[],source:0,runs:0};
  const github={
    harnessSource:async()=>{calls.source++; return typeof revision==='function'?revision():source(revision);},
    reviewedHarness:async r=>reviewed.includes(r),
    group:async id=>structuredClone(groups[id]),
    updateGroup:async(id,data)=>{calls.patch.push({id,...structuredClone(data)}); groups[id]={...groups[id],...structuredClone(data)};},
    unfinishedHarness:async()=>{calls.runs++; return running;},
  };
  const admission=new HarnessAdmission(storage,github,{now:()=>clock});
  return {admission,groups,calls,github,storage,advance:ms=>{clock+=ms;},alarm:()=>alarm,now:()=>clock};
}

test('main\'s harness revision is admitted to every group that runs its reusable workflows',async()=>{
  const f=fixture();
  const state=await f.admission.reconcile();
  assert.ok(f.groups[3].selected_workflows.includes(entry('pr-build.yml',NEW)));
  assert.ok(!f.groups[3].selected_workflows.includes(entry('publish-lake-cache.yml',NEW)));
  for (const file of ['pr-build.yml','publish-lake-cache.yml'])
    assert.ok(f.groups[6].selected_workflows.includes(entry(file,NEW)));
  // Static entries and the previous pin stay: nothing in flight could be stranded.
  for (const w of [...FLEET,entry('pr-build.yml',OLD)]) assert.ok(f.groups[3].selected_workflows.includes(w));
  assert.deepEqual(state.admitted,{TauCetiFleet:[OLD,NEW],TauCetiQueueControl:[OLD,NEW]});
  assert.equal(f.calls.patch.length,2);
  await f.admission.reconcile();
  assert.equal(f.calls.patch.length,2,'an admitted state is not rewritten');
});

test('the queue sees readiness only once every group admits the requested revision',async()=>{
  const f=fixture();
  const status=await f.admission.status(NEW);
  assert.equal(status.ready,true);
  assert.equal(status.main_revision,NEW);
  assert.equal((await f.admission.status(STRAY)).ready,false);
  assert.equal((await f.admission.status(null)).ready,false);
});

test('a hand-removed entry is restored on the next check',async()=>{
  const f=fixture();
  await f.admission.reconcile();
  f.groups[6].selected_workflows=f.groups[6].selected_workflows.filter(w=>w!==entry('publish-lake-cache.yml',NEW));
  f.advance(INTERVAL);
  assert.equal((await f.admission.status(NEW)).ready,true);
  assert.ok(f.groups[6].selected_workflows.includes(entry('publish-lake-cache.yml',NEW)));
});

test('a new main revision is admitted when the queue asks, before the next alarm',async()=>{
  let revision=OLD;
  const f=fixture({revision:()=>source(revision)});
  await f.admission.reconcile();
  revision=NEW;
  f.advance(REFRESH);
  assert.equal((await f.admission.status(NEW)).ready,true);
  f.advance(1);
  // Forced reads are throttled; a repeated miss answers from the stored state.
  const before=f.calls.source;
  await f.admission.status(STRAY);
  assert.equal(f.calls.source,before);
});

test('revisions that are not on TauCetiTools main, or a foreign policy, admit nothing',async()=>{
  for (const options of [{revision:STRAY},{revision:()=>source(NEW,{harness:{name:'someone/Tools',id:1}})},
    {revision:()=>source(NEW,{execution:{name:'TauCetiProject/TauCeti',id:2}})},{revision:()=>'not json'}]) {
    const f=fixture(options);
    const state=await f.admission.reconcile();
    assert.equal(f.calls.patch.length,0);
    assert.equal(state.error.kind,'untrusted_harness_policy');
    assert.equal((await f.admission.status(STRAY)).ready,false);
  }
});

test('a renamed or unrestricted group is never written',async()=>{
  for (const change of [{name:'Other'},{restricted_to_workflows:false}]) {
    const f=fixture();
    Object.assign(f.groups[6],change);
    const state=await f.admission.reconcile();
    assert.equal(f.calls.patch.length,0);
    assert.equal(state.error.kind,'untrusted_harness_policy');
  }
});

test('GitHub failures are recorded without changing the groups',async()=>{
  const f=fixture();
  f.github.harnessSource=async()=>{ throw new UpstreamError(502); };
  const state=await f.admission.reconcile();
  assert.deepEqual(state.error,{kind:'github_http',status:502});
  assert.equal(f.calls.patch.length,0);
});

test('old revisions are pruned only after the retention window and when no unfinished run uses them',async()=>{
  const f=fixture({running:new Set([OLD])});
  await f.admission.reconcile();
  f.advance(RETAIN+1);
  await f.admission.reconcile();
  assert.ok(f.groups[3].selected_workflows.includes(entry('pr-build.yml',OLD)),'an unfinished run keeps its revision');
  assert.equal(f.calls.runs,1);
  // The run finished; its revision restarts the retention window from when it was last needed.
  f.github.unfinishedHarness=async()=>new Set();
  f.advance(RETAIN-1);
  await f.admission.reconcile();
  assert.ok(f.groups[3].selected_workflows.includes(entry('pr-build.yml',OLD)));
  f.advance(2);
  const state=await f.admission.reconcile();
  for (const id of [3,6]) assert.ok(!f.groups[id].selected_workflows.some(w=>w.endsWith('@'+OLD)));
  assert.deepEqual(state.admitted,{TauCetiFleet:[NEW],TauCetiQueueControl:[NEW]});
  assert.equal(state.revisions[OLD],undefined,'forgotten once removed everywhere');
  for (const w of FLEET) assert.ok(f.groups[3].selected_workflows.includes(w),'static entries are untouched');
  for (const w of CONTROL) assert.ok(f.groups[6].selected_workflows.includes(w));
});

test('an incomplete run listing prunes nothing',async()=>{
  const f=fixture({running:null});
  await f.admission.reconcile();
  f.advance(RETAIN+1);
  await f.admission.reconcile();
  assert.ok(f.groups[3].selected_workflows.includes(entry('pr-build.yml',OLD)));
});

test('entries the broker does not manage are left alone',async()=>{
  const foreign='TauCetiProject/TauCetiTools/.github/workflows/publish-lake-cache.yml@'+OLD;
  const f=fixture({fleet:[...FLEET,foreign]});
  await f.admission.reconcile();
  f.advance(RETAIN+1);
  await f.admission.reconcile();
  assert.ok(f.groups[3].selected_workflows.includes(foreign));
});

test('the alarm reconciles and always leaves a wakeup',async()=>{
  const f=fixture();
  await f.admission.tick();
  assert.equal(f.alarm(),f.now()+INTERVAL);
  await f.storage.setAlarm(f.now()+5);
  await f.admission.schedule();
  assert.equal(f.alarm(),f.now()+5,'an earlier recovery wakeup is kept');
});

test('the policy parser accepts the live TauCeti policy shape only',()=>{
  assert.deepEqual(parseSource(source(NEW)),{revision:NEW,workflow:'.github/workflows/source-candidate.yml'});
  for (const bad of [source('B'.repeat(40)),source(NEW,{workflow:'../x.yml'}),source(NEW,{schema:'v1'})])
    assert.throws(()=>parseSource(bad),PolicyError);
});
test('the GitHub client reads main\'s policy conditionally and falls back to a public read',async()=>{
  const {GitHub}=await import('../src/github.mjs');
  const seen=[];
  const content={type:'file',encoding:'base64',size:10,content:btoa(source(NEW))};
  const original=globalThis.fetch;
  let refuseToken=false;
  globalThis.fetch=async(url,init)=>{
    seen.push({url,headers:init.headers});
    if (refuseToken && init.headers.Authorization) return new Response('{}',{status:403});
    if (init.headers['If-None-Match']==='"v1"') return new Response(null,{status:304});
    return new Response(JSON.stringify(content),{status:200,headers:{etag:'"v1"'}});
  };
  try {
    const github=new GitHub({});
    github.token=async()=>'installation';
    assert.equal(await github.harnessSource(),source(NEW));
    assert.equal(await github.harnessSource(),source(NEW),'a 304 reuses the cached body');
    assert.equal(seen[1].headers['If-None-Match'],'"v1"');
    assert.ok(seen[0].url.endsWith('repos/TauCetiProject/TauCeti/contents/.tauceti/ci-source.json?ref=main'));
    refuseToken=true;
    const anonymous=new GitHub({});
    anonymous.token=async()=>'installation';
    assert.equal(await anonymous.harnessSource(),source(NEW));
    assert.equal(seen.at(-1).headers.Authorization,undefined);
  } finally { globalThis.fetch=original; }
});

test('unfinished caller runs report their harness revisions, or null when incomplete',async()=>{
  const {GitHub}=await import('../src/github.mjs');
  const github=new GitHub({});
  github.token=async()=>'installation';
  const pages={queued:{total_count:1,workflow_runs:[{referenced_workflows:[
    {path:'TauCetiProject/TauCetiTools/.github/workflows/pr-build.yml@'+RUNNING,sha:RUNNING},
    {path:'someone/Else/.github/workflows/pr-build.yml@'+STRAY}]}]}};
  github.cached=async path=>{
    assert.ok(path.startsWith('repos/TauCetiProject/TauCeti/actions/workflows/source-candidate.yml/runs?status='));
    return pages[new URL('https://x/'+path).searchParams.get('status')] ?? {total_count:0,workflow_runs:[]};
  };
  assert.deepEqual([...await github.unfinishedHarness('.github/workflows/source-candidate.yml')],[RUNNING]);
  pages.in_progress={total_count:101,workflow_runs:[]};
  assert.equal(await github.unfinishedHarness('.github/workflows/source-candidate.yml'),null);
});
