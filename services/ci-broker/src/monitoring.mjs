/** Host-scoped telemetry forwarding runs independently of runner registration. */
export async function telemetry(request, env, storage) {
  const id = request.headers.get('X-Fleet-Machine') || '';
  if (!/^[a-f0-9]{32}$/.test(id)) return Response.json({error:'unauthorized'}, {status:401});
  const token = (request.headers.get('Authorization') || '').replace(/^Bearer /, '');
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token)))).map(v=>v.toString(16).padStart(2,'0')).join('');
  const host = await storage.get('host:'+id);
  if (!host || host.revoked || host.hash !== hash) return Response.json({error:'unauthorized'}, {status:401});
  const registry = JSON.parse(env.OPERATIONS_PUBLIC_HOSTS || '[]');
  const approved = registry.find(h=>h.machine_id===id);
  if (new URL(request.url).pathname==='/v1/monitoring-policy' && request.method==='GET')
    return Response.json({enabled:Boolean(approved && env.OPERATIONS_INGEST_TOKEN),alias:approved?.alias || null,interval_seconds:15});
  if (!approved || !env.OPERATIONS_INGEST_TOKEN) return Response.json({error:'public telemetry is not configured for this host'}, {status:503});
  let value;
  try {
    const reader = request.body.getReader(); let bytes=0; const chunks=[];
    while (true) { const {value:part,done}=await reader.read(); if (done) break; bytes+=part.byteLength; if (bytes>64000) { await reader.cancel(); throw new Error('limit'); } chunks.push(part); }
    const buffer = new Uint8Array(bytes); let at=0; for (const chunk of chunks) { buffer.set(chunk,at); at+=chunk.byteLength; }
    value=JSON.parse(new TextDecoder().decode(buffer));
    if (value.schema!=='tauceti.operations.hosts-update/v1' || value.hosts?.length!==1 || value.hosts[0].machine_id!==id || value.hosts[0].alias!==approved.alias)
      throw new Error('scope');
    // Same allowlist as the CI receiver. Unknown private fields never leave the broker.
    exact(value,'schema source observed_at hosts'); exact(value.source,'id epoch sequence');
    const h=value.hosts[0];
    exact(h,'machine_id alias observed_at transport_at connected coverage source_revision controller_error ci_enabled workers_enabled ci_max_jobs worker_limit resources cpu_idle_fraction memory_pressure compiler slots workers slots_truncated workers_truncated');
    exact(h.resources,'cpus memory_total memory_available'); exact(h.compiler,'observed_at waiting_guests guests healthy');
    if (!Array.isArray(h.slots) || h.slots.length>128 || !Array.isArray(h.workers) || h.workers.length>128) throw new Error('limit');
    h.slots.forEach(s=>exact(s,'slot instance_id runner_id runner_name phase busy started_at vcpus memory_gib compiler'));
    h.workers.forEach(w=>exact(w,'id state alive observed_at'));
    for (const row of [value.source,h.resources,h.compiler,...h.slots,...h.workers])
      for (const [key,scalar] of Object.entries(row)) if (key!=='compiler') primitive(scalar);
    for (const slot of h.slots) if (slot.compiler !== undefined && slot.compiler !== null) {
      exact(slot.compiler,'observed_at waiting_now running_now completed oldest_wait wait_seconds run_seconds lost');
      primitive(slot.compiler.observed_at);
      for (const key of ['waiting_now','running_now','completed','oldest_wait','wait_seconds','run_seconds','lost']) numeric(slot.compiler[key]);
    }
    for (const [key,scalar] of Object.entries(h)) if (!['resources','compiler','slots','workers'].includes(key)) primitive(scalar);
    if (!/^[a-f0-9-]{36}$/.test(value.source.id) || !Number.isSafeInteger(value.source.epoch) || !Number.isSafeInteger(value.source.sequence)) throw new Error('source');
    for (const key of ['cpus','memory_total','memory_available']) numeric(h.resources[key]);
    for (const key of ['ci_max_jobs','worker_limit','cpu_idle_fraction']) numeric(h[key]);
    numeric(h.compiler.waiting_guests); numeric(h.compiler.guests);
    for (const s of h.slots) {
      for (const key of ['slot','vcpus','memory_gib']) numeric(s[key]);
      if (s.runner_id!==null && (typeof s.runner_id!=='string' || !/^\d{1,20}$/.test(s.runner_id))) throw new Error('runner');
      if (s.instance_id!==null && !/^[a-f0-9]{16}$/.test(s.instance_id)) throw new Error('instance');
    }
    // The receiver performs the complete versioned contract validation too.
  } catch { return Response.json({error:'invalid public telemetry'}, {status:400}); }
  try {
    const response = await fetch('https://ci-data.taucetiproject.org/ingest/operations/host', {
      method:'POST', headers:{Authorization:'Bearer '+env.OPERATIONS_INGEST_TOKEN,'Content-Type':'application/json'},
      body:JSON.stringify(value), redirect:'error', signal:AbortSignal.timeout(2000),
    });
    return Response.json({accepted:response.ok}, {status:response.ok?200:response.status===400?400:503});
  } catch { return Response.json({error:'telemetry observer unavailable'}, {status:503}); }
}
function exact(value, fields) {
  if (!value || typeof value!=='object' || Array.isArray(value) || Object.keys(value).some(k=>!fields.split(' ').includes(k))) throw new Error('private field');
}

function primitive(value) {
  if (value===null || typeof value==='boolean' || (typeof value==='number' && Number.isFinite(value))) return;
  if (typeof value==='string' && value.length<=300 && !/[\x00-\x1f\x7f]/.test(value)) return;
  throw new Error('invalid public scalar');
}
function numeric(value) { if (value!==null && (typeof value!=='number' || !Number.isFinite(value) || value<0)) throw new Error('numeric'); }
