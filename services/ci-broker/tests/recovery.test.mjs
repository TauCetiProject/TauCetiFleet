import {test} from 'node:test';
import assert from 'node:assert/strict';
import {GitHub,UpstreamError,InventoryError,PolicyError} from '../src/github.mjs';
import {RegistrationRecovery,RECOVERY_DELAY,diagnosis} from '../src/recovery.mjs';

function fixture({matches=[],inventoryError=null,policyError=null}={}) {
  const name='fleet-'+'1'.repeat(12)+'-'+'a'.repeat(16);
  const host={machine_id:'1'.repeat(32),slots:{0:{pending:{name,at:Date.now()-RECOVERY_DELAY-1000}}}};
  const records=new Map([['host:'+host.machine_id,structuredClone(host)]]);
  const deleted=[];
  let alarm=null;
  const storage={
    get:async key=>structuredClone(records.get(key)),
    put:async(key,value)=>records.set(key,structuredClone(value)),
    list:async()=>structuredClone(records),
    getAlarm:async()=>alarm,setAlarm:async value=>{alarm=value;},deleteAlarm:async()=>{alarm=null;},
    transaction:async callback=>callback(storage),
  };
  const github={policy:async({fresh})=>{assert.equal(fresh,true);if(policyError)throw policyError;},
    runners:async()=>{if(inventoryError)throw inventoryError;return structuredClone(matches);},
    request:async(path,{method}={})=>{
      const id=Number(path.split('/').at(-1));
      if(method==='DELETE') { deleted.push(id); return null; }
      return structuredClone(matches.find(r=>r.id===id));
    }};
  return {storage,github,host,name,deleted,recovery:new RegistrationRecovery(storage,github)};
}
const stored=async f=>(await f.storage.get('host:'+f.host.machine_id)).slots[0];

test('reserving registration persists a wakeup before any upstream request',async()=>{
  const f=fixture();
  f.host.slots[0].pending.at=Date.now();
  await f.recovery.reserve(f.host);
  assert.deepEqual(await stored(f),f.host.slots[0]);
  assert.ok(await f.storage.getAlarm()>=f.host.slots[0].pending.at+RECOVERY_DELAY);
  const first=await f.storage.getAlarm();
  await f.recovery.reserve(f.host);
  assert.equal(await f.storage.getAlarm(),first);
});
test('an alarm recovers an interrupted registration without a host request or operator',async()=>{
  const f=fixture();
  await f.recovery.run();
  assert.equal((await stored(f)).pending,undefined);
  assert.deepEqual((await stored(f)).last_recovery.failure,{kind:'interrupted'});
  assert.equal(await f.storage.getAlarm(),null);
  assert.deepEqual(f.deleted,[]);
  await f.recovery.run(); // at-least-once alarm delivery is harmless
  assert.equal(await f.storage.getAlarm(),null);
});
test('a registration is never cleared before the settling interval',async()=>{
  const f=fixture();
  f.host.slots[0].pending.at=Date.now();
  await f.storage.put('host:'+f.host.machine_id,f.host);
  await f.recovery.run();
  assert.ok((await stored(f)).pending);
  assert.ok(await f.storage.getAlarm()>Date.now());
});
test('only exact offline matches are removed and the original diagnosis is retained',async()=>{
  const f=fixture();
  const failure={kind:'github_http',status:502,request_id:'request-123'};
  f.host.slots[0].pending.failure=failure;
  await f.storage.put('host:'+f.host.machine_id,f.host);
  f.github.runners=async()=>[{id:10,name:f.name,status:'offline',busy:false},
    {id:11,name:'unrelated',status:'offline',busy:false}];
  f.github.request=async(path,{method})=>{
    assert.ok(path.endsWith('/10'));
    if(method==='DELETE')f.deleted.push(10);
    else return {id:10,name:f.name,status:'offline',busy:false};
  };
  await f.recovery.run();
  assert.deepEqual(f.deleted,[10]);
  assert.deepEqual((await stored(f)).last_recovery.failure,failure);
});
test('online, busy, missing status and newly active runners remain reserved and retried',async()=>{
  for(const runner of [{status:'online',busy:false},{status:'offline',busy:true},{busy:false}]) {
    const f=fixture();
    f.github.runners=async()=>[{id:10,name:f.name,...runner}];
    await f.recovery.run();
    assert.equal((await stored(f)).pending.recovery_error.kind,'runner_active');
    assert.deepEqual(f.deleted,[]);
    assert.ok(await f.storage.getAlarm()>Date.now());
  }
  const f=fixture();
  f.github.runners=async()=>[{id:10,name:f.name,status:'offline',busy:false}];
  f.github.request=async()=>({id:10,name:f.name,status:'online',busy:true});
  await f.recovery.run();
  assert.ok((await stored(f)).pending);
  assert.deepEqual(f.deleted,[]);
});
test('inventory, policy and upstream outages retain ownership and retry beyond six alarms',async()=>{
  for(const options of [{inventoryError:new InventoryError()},{policyError:new PolicyError()},
    {inventoryError:new UpstreamError(503)}]) {
    const f=fixture(options);
    for(let attempt=1;attempt<=8;attempt++) {
      const host=await f.storage.get('host:'+f.host.machine_id);
      host.slots[0].pending.retry_at=Date.now()-1;
      await f.storage.put('host:'+host.machine_id,host);
      await f.recovery.run();
      assert.equal((await stored(f)).pending.attempts,attempt);
      assert.ok(await f.storage.getAlarm()>Date.now());
    }
    assert.deepEqual(f.deleted,[]);
    f.github.runners=async()=>[];f.github.policy=async()=>{};
    const host=await f.storage.get('host:'+f.host.machine_id);
    host.slots[0].pending.retry_at=Date.now()-1;
    await f.storage.put('host:'+host.machine_id,host);
    await f.recovery.run();
    assert.equal((await stored(f)).pending,undefined);
  }
});
test('one blocked slot does not prevent recovery of another slot or host',async()=>{
  const f=fixture();
  f.host.slots[1]={pending:{name:'other',at:Date.now()-RECOVERY_DELAY-1000}};
  await f.storage.put('host:'+f.host.machine_id,f.host);
  await f.storage.put('host:second',{machine_id:'second',slots:{0:{pending:{name:'third',at:1}}}});
  f.github.runners=async()=>[{id:10,name:f.name,status:'online',busy:true}];
  await f.recovery.run();
  const host=await f.storage.get('host:'+f.host.machine_id);
  assert.ok(host.slots[0].pending);
  assert.equal(host.slots[1].pending,undefined);
  assert.equal((await f.storage.get('host:second')).slots[0].pending,undefined);
});
test('legacy pending reservations get a wakeup when health monitoring adopts them',async()=>{
  const f=fixture();
  await f.recovery.schedule();
  assert.ok(await f.storage.getAlarm()>Date.now());
});
test('failure diagnostics exclude raw error messages and credentials',()=>{
  assert.deepEqual(diagnosis(new Error('Bearer secret enrollment code')),{kind:'transport_or_internal'});
  assert.deepEqual(diagnosis(new UpstreamError(502,'abc')),{kind:'github_http',status:502,request_id:'abc'});
  assert.deepEqual(diagnosis(new UpstreamError(502,'arbitrary untrusted header')),{kind:'github_http',status:502,request_id:null});
  assert.deepEqual(diagnosis(new InventoryError()),{kind:'inventory_incomplete'});
  assert.deepEqual(diagnosis(new PolicyError()),{kind:'untrusted_runner_policy'});
});
test('a failed recovery write preserves the reservation for the next alarm',async()=>{
  const f=fixture();
  const put=f.storage.put;
  let failed=false;
  f.storage.put=async(key,value)=>{
    if(!value.slots[0].pending && !failed) { failed=true;throw new Error('storage unavailable'); }
    return put(key,value);
  };
  await f.recovery.run();
  assert.ok((await stored(f)).pending);
  assert.ok(await f.storage.getAlarm()>Date.now());
  const host=await f.storage.get('host:'+f.host.machine_id);
  host.slots[0].pending.retry_at=Date.now()-1;
  await put('host:'+host.machine_id,host);
  await f.recovery.run();
  assert.equal((await stored(f)).pending,undefined);
});
test('authoritative inventory rejects duplicates, changed totals and missing pages',async()=>{
  const runner={id:1,name:'example'};
  for(const pages of [
    [{total_count:2,runners:[runner,runner]}],
    [{total_count:2,runners:[runner]},{total_count:3,runners:[{id:2,name:'second'}]}],
    [{total_count:2,runners:[runner]},{total_count:2,runners:[]}],
    [{total_count:1,runners:[{name:'missing id'}]}],
  ]) {
    const github=new GitHub({});let index=0;
    github.request=async()=>pages[index++];
    await assert.rejects(github.runners(),/Incomplete runner inventory/);
  }
});
