import {slots} from './capacity.mjs';
import {UpstreamError} from './github.mjs';

/** JIT labels are read-only. Block replacement registrations, retire idle
 * runners and let busy one-job registrations finish and deregister normally. */
export async function suspendHost(storage,github,host,suspend) {
  if (host.revoked) return {error:'revoked hosts require enrollment recovery'};
  host.suspended=suspend;
  host.suspension_cleanup_pending=suspend;
  await storage.put('host:'+host.machine_id,host);
  if (!suspend) return {suspended:false,cleanup_pending:false,retired_runners:[],active_runners:[],pending_runners:[]};
  const inventory=await github.runners();
  const owned=new Set(Object.values(slots(host)).filter(s=>s.runner_id).map(s=>s.runner_id));
  const runners=inventory.filter(r=>owned.has(r.id) || (r.labels || []).some(l=>l.name==='fleet-'+host.machine_id));
  const retired=[],active=[],pending=[];
  for (let offset=0;offset<runners.length;offset+=4) await Promise.all(runners.slice(offset,offset+4).map(async runner=>{
    const path=`orgs/TauCetiProject/actions/runners/${runner.id}`;
    try {
      try {
        const current=await github.request(path,{kind:'runners'});
        if (current.busy) { active.push(runner.id); return; }
        try { await github.request(path,{kind:'runners',method:'DELETE'}); }
        catch(error) {
          // Acquisition can race our read. GitHub refuses deleting a busy
          // runner; retain ownership and let that acquired job finish.
          if (error instanceof UpstreamError && error.status===422 &&
              (await github.request(path,{kind:'runners'})).busy) {
            active.push(runner.id);return;
          }
          throw error;
        }
      } catch(error) { if (!(error instanceof UpstreamError) || error.status!==404) throw error; }
      for (const slot of Object.values(slots(host))) if (slot.runner_id===runner.id) {
        delete slot.runner_id;delete slot.jit;
      }
      retired.push(runner.id);
    } catch { pending.push(runner.id); }
  }));
  host.suspension_cleanup_pending=!!pending.length;
  await storage.put('host:'+host.machine_id,host);
  return {suspended:true,cleanup_pending:!!pending.length,retired_runners:retired.sort((a,b)=>a-b),
    active_runners:active.sort((a,b)=>a-b),pending_runners:pending.sort((a,b)=>a-b)};
}
