import { test } from 'node:test';
import assert from 'node:assert/strict';
import { telemetry } from '../src/monitoring.mjs';
const id='a'.repeat(32), alias='yael', token='host-secret';
const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(token)))).map(v=>v.toString(16).padStart(2,'0')).join('');
const host={machine_id:id,alias,observed_at:null,transport_at:null,connected:true,coverage:'unavailable',source_revision:'',controller_error:false,ci_enabled:null,workers_enabled:null,ci_max_jobs:null,worker_limit:null,
 resources:{cpus:null,memory_total:null,memory_available:null},cpu_idle_fraction:null,memory_pressure:null,compiler:{observed_at:null,waiting_guests:null,guests:null,healthy:false},slots:[],workers:[],slots_truncated:false,workers_truncated:false};
const frame={schema:'tauceti.operations.hosts-update/v1',source:{id:crypto.randomUUID(),epoch:1,sequence:1},observed_at:'2026-11-06T00:00:00Z',hosts:[host]};
function request(value=frame,auth=token){return new Request('https://broker/v1/monitoring',{method:'POST',headers:{Authorization:'Bearer '+auth,'X-Fleet-Machine':id},body:JSON.stringify(value)});}
test('host telemetry keeps enrollment credential at broker and never reads GitHub',async t=>{
 let forwarded;
 t.mock.method(globalThis,'fetch',async (url,options)=>{forwarded={url,options};return Response.json({accepted:true});});
 const env={OPERATIONS_PUBLIC_HOSTS:JSON.stringify([{machine_id:id,alias}]),OPERATIONS_INGEST_TOKEN:'telemetry-only'};
 const storage={get:async()=>({machine_id:id,hash,revoked:false})};
 const detailed=structuredClone(frame);
 detailed.hosts[0].capacity_context={observed_at:null,scope:'organization_all_workflow_jobs',coverage:'unavailable',observer_error:false,running_jobs:{github:null,namespace:null,'self-hosted':null}};
 detailed.hosts[0].admission={ci_kinds:['pr','staging'],staging_slots:1,memory_budget_gib:64,reservation_error:false};
 detailed.hosts[0].activity={observed_at:null,window_hours:24,finished_rounds:8,failed_rounds:1,unknown_rounds:2};
 detailed.hosts[0].worker_memory={working_set:123,charged:456,maximum:null,high:800,swap:0,oom_kill:2,stall_some:0.1,stall_full:null};
 detailed.hosts[0].worker_admission={state:'paused',reason:'memory_reserve'};
 detailed.hosts[0].ci_cooldown={until:'2026-11-06T01:00:00Z'};
 assert.equal((await telemetry(request(detailed),env,storage)).status,200);
 assert.equal(forwarded.url,'https://ci-data.taucetiproject.org/ingest/operations/host');
 assert.equal(forwarded.options.headers.Authorization,'Bearer telemetry-only');
 assert.ok(!JSON.stringify(forwarded).includes(token));
 const bad=structuredClone(frame);bad.hosts[0].private_path='/private/credential';
 forwarded=null;assert.equal((await telemetry(request(bad),env,storage)).status,400);assert.equal(forwarded,null);
 const privateMemory=structuredClone(detailed);privateMemory.hosts[0].worker_memory.cgroup='/private/credential';
 assert.equal((await telemetry(request(privateMemory),env,storage)).status,400);
 const privateReason=structuredClone(detailed);privateReason.hosts[0].worker_admission.reason='/private/credential';
 assert.equal((await telemetry(request(privateReason),env,storage)).status,400);
 assert.equal((await telemetry(request(frame,'wrong'),env,storage)).status,401);
 const other=structuredClone(frame);other.hosts[0].machine_id='b'.repeat(32);
 assert.equal((await telemetry(request(other),env,storage)).status,400);
});

test('ordinary enable discovers its public alias from broker policy without GitHub or a second token',async t=>{
 let fetches=0;t.mock.method(globalThis,'fetch',async()=>{fetches++;throw new Error('policy must be local');});
 const env={OPERATIONS_PUBLIC_HOSTS:JSON.stringify([{machine_id:id,alias}]),OPERATIONS_INGEST_TOKEN:'relay'};
 const storage={get:async()=>({machine_id:id,hash,revoked:false})};
 const req=new Request('https://broker/v1/monitoring-policy',{headers:{Authorization:'Bearer '+token,'X-Fleet-Machine':id}});
 const response=await telemetry(req,env,storage);assert.equal(response.status,200);
 assert.deepEqual(await response.json(),{enabled:true,alias,interval_seconds:15});assert.equal(fetches,0);
});
