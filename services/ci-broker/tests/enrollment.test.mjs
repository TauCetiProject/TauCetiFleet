import {after,before,test} from 'node:test';
import assert from 'node:assert/strict';
import {generateKeyPairSync,createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {Miniflare,convertV4MiniflareOptions} from 'miniflare';

let mf;
const origin='https://fleet.taucetiproject.org';
const admin='admin-test';
const id1='1'.repeat(32), id2='2'.repeat(32), id3='3'.repeat(32);
const token1='a'.repeat(64), token2='b'.repeat(64), token3='c'.repeat(64);
const runners=new Map();
const upstream=[];
let nextId=100,failRegistration=false;
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
        restricted_to_workflows:true,selected_workflows:['TauCetiProject/TauCeti/.github/workflows/pr-build.yml@refs/heads/main']});
      if (url.pathname.endsWith('/generate-jitconfig')) {
        if (failRegistration) return respond({error:'internal error'},500);
        const input=await req.json();
        assert.equal(input.runner_group_id,3);
        const runner={id:nextId++,name:input.name,status:'online',busy:false};
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
async function enroll(id,token,invite) {
  const grant=await adminRequest('/admin/invites','POST',{name:'test-'+id[0],hash:hash(invite)});
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
test('one-time enrollment binds a durable credential to one machine',async()=>{
  assert.equal((await hostRequest(id1,token1,'/v1/policy')).status,401);
  await enroll(id1,token1,'invite-one');
  await enroll(id2,token2,'invite-two');
  assert.equal((await hostRequest(id1,token2,'/v1/policy')).status,401);
  assert.equal((await hostRequest(id2,token1,'/v1/policy')).status,401);
  assert.deepEqual(await (await hostRequest(id1,token1,'/v1/policy')).json(),{runner_group_id:3});
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
test('retirement is owned and idempotent; revocation blocks all future host requests',async()=>{
  assert.equal((await hostRequest(id1,token1,'/v1/runners/100','DELETE')).status,200);
  assert.equal((await hostRequest(id1,token1,'/v1/runners/100','DELETE')).status,200);
  assert.equal((await hostRequest(id2,token2,'/v1/runners/100','DELETE')).status,404);
  assert.equal((await adminRequest('/admin/hosts/'+id1,'DELETE')).status,200);
  assert.equal((await hostRequest(id1,token1,'/v1/policy')).status,401);
  assert.equal((await hostRequest(id1,token1,'/v1/runners','POST',{name:'fleet-'+id1.slice(0,12)+'-'+'c'.repeat(16)})).status,401);
  assert.equal((await hostRequest(id2,token2,'/v1/policy')).status,200);
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
});
