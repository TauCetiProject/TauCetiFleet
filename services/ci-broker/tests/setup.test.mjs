import {after, before, test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {Miniflare, convertV4MiniflareOptions} from 'miniflare';

let mf;
const origin='https://fleet.taucetiproject.org';
const admin='admin-test-token';
const state='a'.repeat(64);
const code='b'.repeat(40);
before(async()=>{
  mf=new Miniflare(convertV4MiniflareOptions({modules: true, script: await readFile(new URL('../dist/index.js', import.meta.url), 'utf8'),
    compatibilityDate: '2026-10-11', bindings:{ADMIN_TOKEN: admin},
    durableObjects:{REGISTRY:{className:'FleetRegistry', useSQLite:true}}}));
});
after(async()=>await mf?.dispose());
const request=(path, options={})=>mf.dispatchFetch(origin+path, options);
test('setup requires admin authentication; callback requires the bound unexpired state', async()=>{
  assert.equal((await request('/admin/setup')).status,401);
  assert.equal((await request('/admin/setup',{method:'POST',body:JSON.stringify({state})})).status,401);
  const headers={Authorization:'Bearer '+admin};
  assert.equal((await request('/admin/setup',{method:'POST',headers,body:JSON.stringify({state})})).status,200);
  assert.deepEqual(await (await request('/admin/setup',{headers})).json(),{pending:true});
  assert.equal((await request('/setup/manifest',{headers:{Authorization:'Bearer wrong'}})).status,401);
  const form=await (await request('/setup/manifest',{headers:{Authorization:'Bearer '+state}})).text();
  assert.match(form,/organization_self_hosted_runners/);
  assert.match(form,/TauCetiProject\/settings\/apps\/new/);
  assert.equal((await request('/registration-complete?code='+code+'&state=wrong')).status,400);
  assert.equal((await request('/registration-complete?code='+code+'&state='+state)).status,200);
  assert.deepEqual(await (await request('/admin/setup',{headers})).json(),{code});
  assert.equal((await request('/setup/manifest',{headers:{Authorization:'Bearer '+state}})).status,401);
  assert.equal((await request('/registration-complete?code='+'c'.repeat(40)+'&state='+state)).status,409);
  assert.equal((await request('/admin/setup',{method:'POST',headers,body:JSON.stringify({state})})).status,409);
  assert.equal((await request('/admin/setup',{method:'DELETE',headers})).status,200);
  assert.equal((await request('/registration-complete?code='+code+'&state='+state)).status,400);
});
test('public health and registration pages contain no credentials', async()=>{
  const health=await request('/healthz');
  assert.equal(health.headers.get('Cache-Control'),'no-store');
  assert.deepEqual(await health.json(),{service:'tauceti-ci-broker',app_configured:false});
  const page=await (await request('/setup/register')).text();
  assert.ok(!page.includes(admin));
  assert.match(page,/location.hash/);
});
