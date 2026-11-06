import {after,before,test} from 'node:test';
import assert from 'node:assert/strict';
import {generateKeyPairSync,createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {Miniflare,convertV4MiniflareOptions} from 'miniflare';
import {slots} from '../src/capacity.mjs';

let mf;
const origin='https://fleet.taucetiproject.org';
const admin='admin-test';
const id1='1'.repeat(32), id2='2'.repeat(32), id3='3'.repeat(32);
const token1='a'.repeat(64), token2='b'.repeat(64), token3='c'.repeat(64);
const runners=new Map();
const upstream=[];
let nextId=100,failRegistration=false,rejectRegistration=0,trustedPolicy=true,failRunnerRemoval=false,startJobOnRemoval=0;
const hash=value=>createHash('sha256').update(value).digest('hex');
before(async()=>{
  const {privateKey}=generateKeyPairSync('rsa',{modulusLength:2048});
  mf=new Miniflare(convertV4MiniflareOptions({modules:true,
    // Test-only RPC methods exercise persisted recovery and the actual alarm
    // handler without waiting two minutes or adding production debug routes.
    script:(await readFile(new URL('../dist/index.js',import.meta.url),'utf8'))+`
      export class TestFleetRegistry extends FleetRegistry {
        async seed(host) { return this.serialize(()=>this.ctx.storage.put('host:'+host.machine_id,host)); }
        async alarmTime() { return this.ctx.storage.getAlarm(); }
        async fireRecovery() { return this.alarm(); }
        async elapseAdmission(id,ms) {
          return this.serialize(()=>{ this.buckets.set(id,{tokens:0,at:Date.now()-ms}); });
        }
      }`,compatibilityDate:'2026-10-11',
    durableObjects:{REGISTRY:{className:'TestFleetRegistry',useSQLite:true}},
    bindings:{ADMIN_TOKEN:admin,GITHUB_APP_ID:'42',GITHUB_INSTALLATION_ID:'123',
      GITHUB_PRIVATE_KEY:privateKey.export({type:'pkcs8',format:'pem'}).toString()},
    outboundService:async req=>{
      const url=new URL(req.url);
      assert.equal(url.origin,'https://api.github.com');
      upstream.push({path:url.pathname,method:req.method,auth:req.headers.get('Authorization')});
      const respond=(value,status=200)=>Response.json(value,{status});
      if (url.pathname==='/app/installations/123/access_tokens') {
        assert.match(req.headers.get('Authorization'),/^Bearer ey/);
        const input=await req.json();
        assert.deepEqual(input.repositories,['TauCeti']);
        if (input.permissions.organization_self_hosted_runners) {
          assert.deepEqual(input.permissions,{organization_self_hosted_runners:'write'});
          return respond({token:'runner-grant',expires_at:new Date(Date.now()+3600000).toISOString()});
        }
        assert.deepEqual(input.permissions,{metadata:'read',actions:'read',statuses:'read'});
        return respond({token:'read-grant',expires_at:new Date(Date.now()+3600000).toISOString()});
      }
      if (url.pathname.startsWith('/orgs/')) assert.equal(req.headers.get('Authorization'),'Bearer runner-grant');
      else assert.equal(req.headers.get('Authorization'),'Bearer read-grant');
      if (url.pathname==='/orgs/TauCetiProject/actions/runner-groups/3') return respond({id:3,name:'TauCetiFleet',
        restricted_to_workflows:trustedPolicy,selected_workflows:['TauCetiProject/TauCeti/.github/workflows/pr-build.yml@refs/heads/main']});
      if (url.pathname==='/orgs/TauCetiProject/actions/runner-groups/3/runners') {
        const all=[...runners.values()],page=Number(url.searchParams.get('page') || 1);
        return respond({total_count:all.length,runners:all.slice((page-1)*100,page*100)});
      }
      if (url.pathname.endsWith('/generate-jitconfig')) {
        if (rejectRegistration) return respond({error:'rejected'},rejectRegistration);
        if (failRegistration) return respond({error:'internal error'},500);
        const input=await req.json();
        assert.equal(input.runner_group_id,3);
        const runner={id:nextId++,name:input.name,status:'online',busy:false,labels:input.labels.map(name=>({name}))};
        runners.set(runner.id,runner);
        return respond({runner,encoded_jit_config:'single-job-'+runner.id});
      }
      const runner=url.pathname.match(/^\/orgs\/TauCetiProject\/actions\/runners\/(\d+)$/);
      if (runner) {
        const id=Number(runner[1]);
        if (!runners.has(id)) return respond({error:'missing'},404);
        if (req.method==='DELETE') {
          if (failRunnerRemoval) return respond({error:'try later'},429);
          if (startJobOnRemoval===id) { runners.get(id).busy=true;startJobOnRemoval=0; }
          if (runners.get(id).busy) return respond({error:'runner busy'},422);
          runners.delete(id);return new Response(null,{status:204});
        }
        return respond(runners.get(id));
      }
      if (url.pathname==='/repos/TauCetiProject/TauCeti/actions/runs/9/jobs')
        return respond({total_count:2,jobs:[{id:1,runner_id:100},{id:2,runner_id:101}]});
      if (url.pathname==='/repos/TauCetiProject/TauCeti/actions/runs/9') return respond({id:9});
      throw new Error('Unexpected upstream endpoint: '+url.pathname);
    }}));
});
after(async()=>await mf?.dispose());
const request=(path,options={})=>mf.dispatchFetch(origin+path,options);
const adminRequest=(path,method='GET',data)=>request(path,{method,headers:{Authorization:'Bearer '+admin},body:data?JSON.stringify(data):undefined});
const hostRequest=(id,token,path,method='GET',data)=>request(path,{method,
  headers:{Authorization:'Bearer '+token,'X-Fleet-Machine':id},body:data?JSON.stringify(data):undefined});
async function enroll(id,token,invite,limits={}) {
  invite=invite.padEnd(64,'_');
  const grant=await adminRequest('/admin/invites','POST',{name:'test-'+id[0],hash:hash(invite),...limits});
  assert.equal(grant.status,200);
  const window=await grant.json();
  assert.equal(window.expires_in,7*24*3600);
  assert.ok(window.expires_at>Date.now()+6*24*3600*1000);
  const redeem=(machine_id=id,host_token=token)=>request('/v1/enroll',{method:'POST',headers:{Authorization:'Bearer '+invite},body:JSON.stringify({machine_id,host_token})});
  assert.equal((await redeem()).status,200);
  assert.equal((await redeem()).status,200);
  assert.equal((await redeem('f'.repeat(32),'x'.repeat(64))).status,409);
  assert.equal((await adminRequest('/admin/invites','POST',{name:'reset-used',hash:hash(invite)})).status,409);
}
test('operators can choose or extend an unredeemed invitation window',async()=>{
  const input={name:'delayed-zulip',hash:hash('long-lived-invite'),expires_in:2*24*3600};
  const first=await (await adminRequest('/admin/invites','POST',input)).json();
  assert.equal(first.expires_in,2*24*3600);
  const extension=await (await adminRequest('/admin/invites','POST',{...input,expires_in:14*24*3600})).json();
  assert.ok(extension.expires_at>first.expires_at);
  for (const expires_in of [0,3599,31*24*3600,1.5,'604800'])
    assert.equal((await adminRequest('/admin/invites','POST',{...input,expires_in})).status,400);
});
test('an empty or malformed invitation cannot authorize a host even if its hash was recorded',async()=>{
  await adminRequest('/admin/invites','POST',{name:'invalid-empty',hash:hash('')});
  for (const Authorization of ['', 'Bearer ', 'Bearer short', 'Basic '+'a'.repeat(64)]) {
    const response=await request('/v1/enroll',{method:'POST',headers:{Authorization},
      body:JSON.stringify({machine_id:'e'.repeat(32),host_token:'a'.repeat(64)})});
    assert.equal(response.status,401);
  }
});
test('one-time enrollment binds a durable credential to one machine',async()=>{
  assert.equal((await hostRequest(id1,token1,'/v1/policy')).status,401);
  await enroll(id1,token1,'invite-one');
  await enroll(id2,token2,'invite-two');
  assert.equal((await hostRequest(id1,token2,'/v1/policy')).status,401);
  assert.equal((await hostRequest(id2,token1,'/v1/policy')).status,401);
  assert.deepEqual(await (await hostRequest(id1,token1,'/v1/policy')).json(),{runner_group_id:3,max_jobs:4096,kinds:['staging']});
});
test('concurrent registration creates one runner and cannot change its group, labels, or machine',async()=>{
  const name='fleet-'+id1.slice(0,12)+'-'+'a'.repeat(16);
  const attempts=await Promise.all([1,2].map(()=>hostRequest(id1,token1,'/v1/runners','POST',{name,runner_group_id:99})));
  for (const response of attempts) assert.deepEqual(await response.json(),{runner_id:100,encoded_jit_config:'single-job-100'});
  assert.equal(upstream.filter(x=>x.path.endsWith('/generate-jitconfig')).length,1);
  assert.equal((await hostRequest(id1,token1,'/v1/runners','POST',{name:name.slice(0,-1)+'b'})).status,409);
  assert.equal((await hostRequest(id2,token2,'/v1/runners','POST',{name})).status,400);
  const second=await hostRequest(id2,token2,'/v1/runners','POST',{name:'fleet-'+id2.slice(0,12)+'-'+'b'.repeat(16)});
  assert.equal(second.status,200);
  assert.deepEqual((await (await hostRequest(id1,token1,'/v1/runners')).json()).runners.map(r=>r.id),[100]);
  assert.equal((await hostRequest(id1,token1,'/v1/runners/101','DELETE')).status,404);
  assert.ok(runners.has(101));
});
test('metadata reads are fixed to TauCeti and jobs are filtered to the host',async()=>{
  const jobs=await (await hostRequest(id1,token1,'/v1/github/actions/runs/9/jobs')).json();
  assert.deepEqual(jobs,{total_count:1,jobs:[{id:1,runner_id:100}]});
  assert.equal((await hostRequest(id1,token1,'/v1/github/actions/runs/9','DELETE')).status,404);
  assert.equal((await hostRequest(id1,token1,'/v1/github/repos/other/repo')).status,404);
  assert.equal((await hostRequest(id1,token1,'/v1/github/orgs/TauCetiProject/actions/runners','POST',{})).status,404);
});
test('runner absence does not erase ownership or hide a reappearing runner',async()=>{
  const runner=runners.get(100);
  runners.delete(100);
  assert.deepEqual((await (await hostRequest(id1,token1,'/v1/runners')).json()).runners,[]);
  assert.equal((await hostRequest(id1,token1,'/v1/runners','POST',
    {name:'fleet-'+id1.slice(0,12)+'-'+'d'.repeat(16)})).status,409);
  runners.set(100,runner);
  assert.deepEqual((await (await hostRequest(id1,token1,'/v1/runners')).json()).runners.map(r=>r.id),[100]);
});
test('retirement is owned and idempotent; revocation blocks all future host requests',async()=>{
  assert.equal((await hostRequest(id1,token1,'/v1/runners/100','DELETE')).status,200);
  assert.equal((await hostRequest(id1,token1,'/v1/runners/100','DELETE')).status,200);
  assert.equal((await hostRequest(id2,token2,'/v1/runners/100','DELETE')).status,404);
  assert.equal((await adminRequest('/admin/hosts/'+id1,'DELETE')).status,200);
  assert.equal((await hostRequest(id1,token1,'/v1/policy')).status,401);
  assert.equal((await hostRequest(id1,token1,'/v1/runners','POST',{name:'fleet-'+id1.slice(0,12)+'-'+'c'.repeat(16)})).status,401);
  assert.equal((await hostRequest(id2,token2,'/v1/policy')).status,200);
});
test('definite registration rejections do not pin capacity',async()=>{
  const id='4'.repeat(32),token='d'.repeat(64);
  await enroll(id,token,'rejected-registration');
  const name='fleet-'+id.slice(0,12)+'-'+'d'.repeat(16);
  rejectRegistration=422;
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{name})).status,503);
  rejectRegistration=0;
  const result=await hostRequest(id,token,'/v1/runners','POST',{name});
  assert.equal(result.status,200);
  const runner=await result.json();
  assert.equal((await hostRequest(id,token,'/v1/runners/'+runner.runner_id,'DELETE')).status,200);
});
test('revocation removes idle registrations and lets an acquired job finish before cleanup',async()=>{
  const id='5'.repeat(32),token='e'.repeat(64);
  await enroll(id,token,'revocation-cleanup');
  const result=await (await hostRequest(id,token,'/v1/runners','POST',
    {name:'fleet-'+id.slice(0,12)+'-'+'e'.repeat(16)})).json();
  runners.get(result.runner_id).busy=true;
  const busy=await (await adminRequest('/admin/hosts/'+id,'DELETE')).json();
  assert.deepEqual(busy,{revoked:true,runner_busy:true,cleanup_pending:false});
  assert.ok(runners.has(result.runner_id));
  assert.equal((await hostRequest(id,token,'/v1/policy')).status,401);
  runners.get(result.runner_id).busy=false;
  const idle=await (await adminRequest('/admin/hosts/'+id,'DELETE')).json();
  assert.deepEqual(idle,{revoked:true,runner_busy:false,cleanup_pending:false});
  assert.ok(!runners.has(result.runner_id));
});
test('cached readiness policy never replaces a fresh registration policy check',async()=>{
  const id='6'.repeat(32),token='f'.repeat(64);
  await enroll(id,token,'fresh-policy');
  assert.equal((await hostRequest(id,token,'/v1/policy')).status,200);
  trustedPolicy=false;
  const name='fleet-'+id.slice(0,12)+'-'+'f'.repeat(16);
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{name})).status,503);
  trustedPolicy=true;
  const result=await (await hostRequest(id,token,'/v1/runners','POST',{name})).json();
  assert.equal((await hostRequest(id,token,'/v1/runners/'+result.runner_id,'DELETE')).status,200);
});
test('one host cannot issue unlimited authenticated requests or consume another host allowance',async()=>{
  const id='7'.repeat(32),token='g'.repeat(64);
  await enroll(id,token,'rate-limited-host');
  const results=[];
  for (let i=0;i<33;i++) results.push((await hostRequest(id,token,'/v1/policy')).status);
  assert.deepEqual(results.slice(0,32),Array(32).fill(200));
  assert.equal(results[32],429);
  assert.equal((await hostRequest('6'.repeat(32),'f'.repeat(64),'/v1/policy')).status,200);
});
test('request refill scales with active slots and excludes empty records and excessive counts',async()=>{
  const namespace=await mf.getDurableObjectNamespace('REGISTRY');
  const registry=namespace.get(namespace.idFromName('fleet'));
  const id='e'.repeat(32),token='n'.repeat(64);
  const base={machine_id:id,name:'refill-host',hash:hash(token),issued:[],created_at:1};
  const requestPolicy=()=>hostRequest(id,token,'/v1/policy');
  const empty=Object.fromEntries(Array.from({length:128},(_,i)=>[i,{}]));
  await registry.seed({...base,slots:empty,max_jobs:4096});
  await registry.elapseAdmission(id,200);
  assert.equal((await requestPolicy()).status,429);
  const owned=Object.fromEntries(Array.from({length:128},(_,i)=>[i,{runner_id:1000+i}]));
  await registry.seed({...base,slots:owned});
  await registry.elapseAdmission(id,200);
  assert.equal((await requestPolicy()).status,429);
  await registry.seed({...base,slots:{0:{runner_id:1000},1:{pending:{name:'uncertain',at:Date.now()}}}});
  await registry.elapseAdmission(id,5500);
  assert.equal((await requestPolicy()).status,200);
  await registry.seed({...base,slots:{}});
});
test('an ambiguous registration does not create additional runners on retry',async()=>{
  await enroll(id3,token3,'invite-three');
  failRegistration=true;
  const name='fleet-'+id3.slice(0,12)+'-'+'c'.repeat(16);
  assert.equal((await hostRequest(id3,token3,'/v1/runners','POST',{name})).status,503);
  const before=upstream.filter(x=>x.path.endsWith('/generate-jitconfig')).length;
  assert.equal((await hostRequest(id3,token3,'/v1/runners','POST',{name})).status,409);
  assert.equal(upstream.filter(x=>x.path.endsWith('/generate-jitconfig')).length,before);
  assert.equal((await adminRequest('/admin/hosts/'+id3+'/reconcile','POST',{})).status,409);
  failRegistration=false;
});
test('hosts choose slot counts while registrations retain class and ownership checks',async()=>{
  const id='8'.repeat(32),token='h'.repeat(64);
  await enroll(id,token,'shared-two-slots',{max_jobs:2,kinds:['pr','staging']});
  const prefix='fleet-'+id.slice(0,12)+'-';
  const inputs=[{slot:0,kinds:['pr'],name:prefix+'1'.repeat(16)},
    {slot:1,kinds:['staging'],name:prefix+'2'.repeat(16)}];
  const responses=await Promise.all(inputs.map(input=>hostRequest(id,token,'/v1/runners','POST',input)));
  const registered=[];
  for (const response of responses) { assert.equal(response.status,200); registered.push(await response.json()); }
  const policy=await (await hostRequest(id,token,'/v1/policy')).json();
  assert.equal(policy.max_jobs,4096);
  const inventory=(await (await hostRequest(id,token,'/v1/runners')).json()).runners;
  assert.equal(inventory.length,2);
  const pr=inventory.find(r=>r.id===registered[0].runner_id).labels.map(l=>l.name);
  const staging=inventory.find(r=>r.id===registered[1].runner_id).labels.map(l=>l.name);
  assert.ok(pr.includes('tauceti-fleet-pr')); assert.ok(!pr.includes('tauceti-fleet'));
  assert.ok(!pr.includes('tauceti-fleet-staging'));
  assert.ok(staging.includes('tauceti-fleet-staging')); assert.ok(!staging.includes('tauceti-fleet-pr'));
  assert.ok(pr.includes('fleet-'+id)); assert.ok(pr.includes('fleet-instance-'+'1'.repeat(16)));
  assert.ok(pr.includes('fleet-host-test-8'));
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{...inputs[0],slot:1})).status,409);
  assert.equal((await adminRequest('/admin/hosts/'+id+'/capacity','POST',{max_jobs:1,kinds:['staging']})).status,409);
  runners.get(registered[1].runner_id).busy=true;
  for (const r of registered) assert.ok(runners.has(r.runner_id));
  assert.equal(runners.get(registered[1].runner_id).busy,true);
  const extra=await hostRequest(id,token,'/v1/runners','POST',{slot:23,kinds:['pr'],name:prefix+'3'.repeat(16)});
  assert.equal(extra.status,200);
  // Old operator requests cannot reinstate a runner-count ceiling.
  assert.equal((await adminRequest('/admin/hosts/'+id+'/capacity','POST',{max_jobs:1,kinds:['pr','staging']})).status,200);
  assert.equal((await (await hostRequest(id,token,'/v1/policy')).json()).max_jobs,4096);
  const hosts=(await (await adminRequest('/admin/hosts')).json()).hosts;
  assert.equal('max_jobs' in hosts.find(h=>h.machine_id===id),false);
  assert.equal((await adminRequest('/admin/hosts/'+id+'/capacity','POST',{max_jobs:24,kinds:['pr']})).status,409);
  assert.equal((await hostRequest(id,token,'/v1/slots/23/cancel-registration','POST',{name:prefix+'3'.repeat(16)})).status,200);
  const revoked=await (await adminRequest('/admin/hosts/'+id,'DELETE')).json();
  assert.equal(revoked.runner_busy,true);
  assert.ok(!runners.has(registered[0].runner_id)); assert.ok(runners.has(registered[1].runner_id));
  runners.get(registered[1].runner_id).busy=false;
  await adminRequest('/admin/hosts/'+id,'DELETE');
  assert.ok(!runners.has(registered[1].runner_id));
});
test('staging-only enrollment permits extra slots but refuses PR labels and invalid classes',async()=>{
  const id='9'.repeat(32),token='i'.repeat(64),name='fleet-'+id.slice(0,12)+'-'+'3'.repeat(16);
  await enroll(id,token,'staging-only');
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{name,kinds:['pr']})).status,403);
  for (const slot of [-1,4096,1.5,'1'])
    assert.equal((await hostRequest(id,token,'/v1/runners','POST',{name,slot})).status,400);
  for (const kinds of [['other'],[],['pr','pr'],null])
    assert.equal((await adminRequest('/admin/hosts/'+id+'/capacity','POST',{kinds})).status,400);
  assert.equal((await adminRequest('/admin/hosts/'+id+'/capacity','POST',{kinds:['pr','staging']})).status,200);
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{name,slot:4095,kinds:['pr']})).status,200);
});
test('persisted enrollment ceilings do not restrict existing hosts or older controllers',async()=>{
  const namespace=await mf.getDurableObjectNamespace('REGISTRY');
  const registry=namespace.get(namespace.idFromName('fleet'));
  const id='d'.repeat(32),token='m'.repeat(64),prefix='fleet-'+id.slice(0,12)+'-';
  await registry.seed({machine_id:id,name:'old-four-slot-host',hash:hash(token),issued:[],
    max_jobs:4,kinds:['pr','staging'],created_at:1,slots:{}});
  assert.equal((await (await hostRequest(id,token,'/v1/policy')).json()).max_jobs,4096);
  const first=await (await hostRequest(id,token,'/v1/runners','POST',
    {name:prefix+'8'.repeat(16),slot:0,kinds:['pr']})).json();
  runners.get(first.runner_id).busy=true;
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',
    {name:prefix+'9'.repeat(16),slot:24,kinds:['staging']})).status,200);
  assert.equal(runners.get(first.runner_id).busy,true);
});
test('an ambiguous registration pins only its own slot and legacy records retain ownership during migration',async()=>{
  const id='a'.repeat(32),token='j'.repeat(64),prefix='fleet-'+id.slice(0,12)+'-';
  await enroll(id,token,'independent-slots',{kinds:['staging']});
  failRegistration=true;
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{name:prefix+'4'.repeat(16),slot:0})).status,503);
  failRegistration=false;
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{name:prefix+'5'.repeat(16),slot:1})).status,200);
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{name:prefix+'4'.repeat(16),slot:0})).status,409);
  assert.equal((await hostRequest(id,token,'/v1/slots/0/cancel-registration','POST',{name:prefix+'4'.repeat(16)})).status,409);
  const legacy={machine_id:'old',runner_id:42,runner_name:'old-runner',jit:'old-jit',issued:[42]};
  assert.deepEqual(slots(legacy),{'0':{runner_id:42,runner_name:'old-runner',jit:'old-jit'}});
  assert.equal(legacy.runner_id,undefined); assert.deepEqual(legacy.issued,[42]);
  const pending={pending:{name:'uncertain',at:1}};
  assert.deepEqual(slots(pending),{'0':{pending:{name:'uncertain',at:1}}});
});
test('draining cancels a definitively rejected intent in any valid local slot',async()=>{
  const id='b'.repeat(32),token='k'.repeat(64),name='fleet-'+id.slice(0,12)+'-'+'6'.repeat(16);
  await enroll(id,token,'cancel-unlaunched',{kinds:['staging']});
  rejectRegistration=422;
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{name,slot:1})).status,503);
  rejectRegistration=0;
  assert.equal((await adminRequest('/admin/hosts/'+id+'/capacity','POST',{max_jobs:1,kinds:['staging']})).status,200);
  const before=upstream.filter(x=>x.path.endsWith('/generate-jitconfig')).length;
  assert.equal((await hostRequest(id,token,'/v1/slots/1/cancel-registration','POST',{name})).status,200);
  assert.equal(upstream.filter(x=>x.path.endsWith('/generate-jitconfig')).length,before);
});
test('stuck persisted state fails health, wakes recovery, and permits an unchanged host to retry',async()=>{
  const namespace=await mf.getDurableObjectNamespace('REGISTRY');
  const registry=namespace.get(namespace.idFromName('fleet'));
  const id='c'.repeat(32),token='l'.repeat(64),name='fleet-'+id.slice(0,12)+'-'+'7'.repeat(16);
  await registry.seed({machine_id:id,name:'stuck-host',hash:hash(token),issued:[],created_at:1,
    slots:{0:{pending:{name,at:Date.now()-700000,failure:{kind:'github_http',status:502,request_id:'abc'}}}}});
  const unhealthy=await request('/healthz');
  assert.equal(unhealthy.status,503);
  assert.equal((await unhealthy.json()).stuck_registrations,1);
  assert.ok(await registry.alarmTime()>Date.now());
  const posts=upstream.filter(x=>x.path.endsWith('/generate-jitconfig')).length;
  await registry.fireRecovery();
  assert.equal(upstream.filter(x=>x.path.endsWith('/generate-jitconfig')).length,posts);
  const healthy=await request('/healthz');
  assert.equal(healthy.status,200);
  assert.equal((await healthy.json()).stuck_registrations,0);
  const hosts=(await (await adminRequest('/admin/hosts')).json()).hosts;
  const recovered=hosts.find(h=>h.machine_id===id).slots[0];
  assert.equal(recovered.registration_pending,false);
  assert.equal(recovered.last_recovery.failure.status,502);
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{name})).status,200);
});

test('temporary suspension retires idle JIT runners, preserves acquired jobs and resumes the same credential',async()=>{
  const id='f'.repeat(32),token='9'.repeat(64),ns=await mf.getDurableObjectNamespace('REGISTRY');
  const names=[90001,90002].map((n,i)=>'fleet-'+id.slice(0,12)+'-'+String(i+1).repeat(16));
  const host={machine_id:id,name:'suspended-host',hash:hash(token),issued:[90001,90002],kinds:['pr','staging'],slots:{}};
  for(let i=0;i<2;i++) {
    const labels=['self-hosted','linux','x64','fleet-'+id,'fleet-instance-'+String(i+1).repeat(16),'tauceti-fleet-pr','tauceti-fleet-staging','tauceti-fleet'];
    runners.set(90001+i,{id:90001+i,name:names[i],busy:i===0,status:'online',labels:labels.map(name=>({name}))});
    host.slots[String(i)]={runner_id:90001+i,runner_name:names[i],kinds:['pr','staging'],jit:'existing-jit'};
  }
  await ns.get(ns.idFromName('fleet')).seed(host);
  const start=upstream.length;
  assert.equal((await request('/admin/hosts/'+id+'/suspend',{method:'POST'})).status,404);
  assert.equal((await adminRequest('/admin/hosts/'+id+'/suspend','POST')).status,200);
  const listing=await (await adminRequest('/admin/hosts')).json();
  assert.equal(listing.hosts.find(h=>h.machine_id===id).suspended,true);
  assert.ok(!runners.has(90002));
  assert.ok(runners.get(90001).labels.some(l=>l.name==='tauceti-fleet-pr'));
  assert.equal(runners.get(90001).busy,true);
  assert.ok(upstream.slice(start).every(r=>!r.path.includes('/actions/runs/') && !(r.method==='DELETE' && r.path.endsWith('/90001'))));
  const rejected=await hostRequest(id,token,'/v1/runners','POST',{name:names[0],kinds:['pr','staging']});
  assert.equal(rejected.status,409);assert.equal((await rejected.json()).code,'host_suspended');
  assert.equal((await hostRequest(id,token,'/v1/policy')).status,200);
  assert.equal((await hostRequest(id,token,'/v1/runners')).status,200);
  assert.equal((await adminRequest('/admin/hosts/'+id+'/suspend','POST')).status,200);
  const resumed=await (await adminRequest('/admin/hosts/'+id+'/resume','POST')).json();
  assert.equal(resumed.suspended,false);assert.equal(resumed.cleanup_pending,false);
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{name:names[1],slot:1,kinds:['pr','staging']})).status,200);
});

test('failed idle cleanup stays visibly suspended and is safely retryable without revocation',async()=>{
 const id='f'.repeat(32);failRunnerRemoval=true;
 try {
  const value=await (await adminRequest('/admin/hosts/'+id+'/suspend','POST')).json();
  assert.equal(value.suspended,true);assert.equal(value.cleanup_pending,true);assert.equal(value.pending_runners.length,1);
 }finally{failRunnerRemoval=false;}
 const value=await (await adminRequest('/admin/hosts/'+id+'/suspend','POST')).json();
 assert.equal(value.suspended,true);assert.equal(value.cleanup_pending,false);
 assert.equal((await hostRequest(id,'9'.repeat(64),'/v1/policy')).status,200);
 const revoked='5'.repeat(32);
 assert.equal((await adminRequest('/admin/hosts/'+revoked+'/resume','POST')).status,409);
});

test('a job acquiring its runner during suspension retirement keeps running with ownership retained',async()=>{
 const id='0123456789abcdef0123456789abcdef',token='8'.repeat(64),ns=await mf.getDurableObjectNamespace('REGISTRY');
 const name='fleet-'+id.slice(0,12)+'-'+'d'.repeat(16),runner_id=91001;
 runners.set(runner_id,{id:runner_id,name,status:'online',busy:false,labels:[{name:'fleet-'+id},{name:'tauceti-fleet-pr'}]});
 await ns.get(ns.idFromName('fleet')).seed({machine_id:id,name:'race-host',hash:hash(token),issued:[runner_id],kinds:['pr'],slots:{'0':{runner_id,runner_name:name,kinds:['pr'],jit:'one-job'}}});
 startJobOnRemoval=runner_id;
 const value=await (await adminRequest('/admin/hosts/'+id+'/suspend','POST')).json();
 assert.equal(value.cleanup_pending,false);assert.deepEqual(value.active_runners,[runner_id]);assert.equal(runners.get(runner_id).busy,true);
 const hosts=await (await adminRequest('/admin/hosts')).json();assert.equal(hosts.hosts.find(h=>h.machine_id===id).slots[0].runner_id,runner_id);
});
