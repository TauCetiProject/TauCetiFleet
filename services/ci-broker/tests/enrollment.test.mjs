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
let nextId=100,failRegistration=false,rejectRegistration=0,trustedPolicy=true;
const hash=value=>createHash('sha256').update(value).digest('hex');
before(async()=>{
  const {privateKey}=generateKeyPairSync('rsa',{modulusLength:2048});
  mf=new Miniflare(convertV4MiniflareOptions({modules:true,
    script:await readFile(new URL('../dist/index.js',import.meta.url),'utf8'),compatibilityDate:'2026-10-11',
    durableObjects:{REGISTRY:{className:'FleetRegistry',useSQLite:true}},
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
        if (req.method==='DELETE') { runners.delete(id); return new Response(null,{status:204}); }
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
  assert.deepEqual(await (await hostRequest(id1,token1,'/v1/policy')).json(),{runner_group_id:3,max_jobs:1,kinds:['staging']});
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
test('a machine has one shared slot ceiling across explicitly authorized PR and staging classes',async()=>{
  const id='8'.repeat(32),token='h'.repeat(64);
  await enroll(id,token,'shared-two-slots',{max_jobs:2,kinds:['pr','staging']});
  const prefix='fleet-'+id.slice(0,12)+'-';
  const inputs=[{slot:0,kinds:['pr'],name:prefix+'1'.repeat(16)},
    {slot:1,kinds:['staging'],name:prefix+'2'.repeat(16)}];
  const responses=await Promise.all(inputs.map(input=>hostRequest(id,token,'/v1/runners','POST',input)));
  const registered=[];
  for (const response of responses) { assert.equal(response.status,200); registered.push(await response.json()); }
  const policy=await (await hostRequest(id,token,'/v1/policy')).json();
  assert.equal(policy.max_jobs,2);
  const inventory=(await (await hostRequest(id,token,'/v1/runners')).json()).runners;
  assert.equal(inventory.length,2);
  const pr=inventory.find(r=>r.id===registered[0].runner_id).labels.map(l=>l.name);
  const staging=inventory.find(r=>r.id===registered[1].runner_id).labels.map(l=>l.name);
  assert.ok(pr.includes('tauceti-fleet-pr')); assert.ok(!pr.includes('tauceti-fleet'));
  assert.ok(!pr.includes('tauceti-fleet-staging'));
  assert.ok(staging.includes('tauceti-fleet-staging')); assert.ok(!staging.includes('tauceti-fleet-pr'));
  assert.ok(pr.includes('fleet-'+id)); assert.ok(pr.includes('fleet-instance-'+'1'.repeat(16)));
  assert.ok(pr.includes('fleet-host-test-8'));
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{...inputs[1],slot:2})).status,403);
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{...inputs[0],slot:1})).status,409);
  assert.equal((await adminRequest('/admin/hosts/'+id+'/capacity','POST',{max_jobs:1,kinds:['staging']})).status,409);
  runners.get(registered[1].runner_id).busy=true;
  const revoked=await (await adminRequest('/admin/hosts/'+id,'DELETE')).json();
  assert.equal(revoked.runner_busy,true);
  assert.ok(!runners.has(registered[0].runner_id)); assert.ok(runners.has(registered[1].runner_id));
  runners.get(registered[1].runner_id).busy=false;
  await adminRequest('/admin/hosts/'+id,'DELETE');
  assert.ok(!runners.has(registered[1].runner_id));
});
test('staging-only enrollment refuses PR labels, extra slots, and invalid operator capacities',async()=>{
  const id='9'.repeat(32),token='i'.repeat(64),name='fleet-'+id.slice(0,12)+'-'+'3'.repeat(16);
  await enroll(id,token,'staging-only');
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{name,kinds:['pr']})).status,403);
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{name,slot:1})).status,403);
  for (const input of [{max_jobs:0,kinds:['staging']},{max_jobs:2,kinds:['other']},{max_jobs:2,kinds:[]},
    {max_jobs:2,kinds:['pr','pr']},{max_jobs:true,kinds:['pr']}])
    assert.equal((await adminRequest('/admin/hosts/'+id+'/capacity','POST',input)).status,400);
  assert.equal((await adminRequest('/admin/hosts/'+id+'/capacity','POST',{max_jobs:2,kinds:['pr','staging']})).status,200);
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{name,slot:1,kinds:['pr']})).status,200);
});
test('an ambiguous registration pins only its own slot and legacy records retain ownership during migration',async()=>{
  const id='a'.repeat(32),token='j'.repeat(64),prefix='fleet-'+id.slice(0,12)+'-';
  await enroll(id,token,'independent-slots',{max_jobs:2,kinds:['staging']});
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
test('draining cancels a definitively rejected intent even after its operator ceiling was lowered',async()=>{
  const id='b'.repeat(32),token='k'.repeat(64),name='fleet-'+id.slice(0,12)+'-'+'6'.repeat(16);
  await enroll(id,token,'cancel-unlaunched',{max_jobs:2,kinds:['staging']});
  rejectRegistration=422;
  assert.equal((await hostRequest(id,token,'/v1/runners','POST',{name,slot:1})).status,503);
  rejectRegistration=0;
  assert.equal((await adminRequest('/admin/hosts/'+id+'/capacity','POST',{max_jobs:1,kinds:['staging']})).status,200);
  const before=upstream.filter(x=>x.path.endsWith('/generate-jitconfig')).length;
  assert.equal((await hostRequest(id,token,'/v1/slots/1/cancel-registration','POST',{name})).status,200);
  assert.equal(upstream.filter(x=>x.path.endsWith('/generate-jitconfig')).length,before);
});
