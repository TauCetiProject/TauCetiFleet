import {slots} from './capacity.mjs';
import {UpstreamError} from './github.mjs';

const routingLabels=['tauceti-fleet','tauceti-fleet-pr','tauceti-fleet-staging'];

/** Suspend placement without revoking credentials or interrupting acquired jobs.
 * Persist before GitHub I/O; partial cleanup is explicit and safely retryable.
 */
export async function suspendHost(storage,github,host,suspend) {
  if (host.revoked) return {error:'revoked hosts require enrollment recovery'};
  if (suspend) {
    host.suspended=true;
    host.suspension_cleanup_pending=true;
    await storage.put('host:'+host.machine_id,host);
  }
  const inventory=await github.runners();
  const owned=new Map(Object.values(slots(host)).filter(s=>s.runner_id).map(s=>[s.runner_id,s]));
  const runners=inventory.filter(r=>owned.has(r.id) || (r.labels || []).some(l=>l.name==='fleet-'+host.machine_id));
  const updated=[],pending=[];
  // GitHub mutations affect labels only. They never delete a runner or cancel a job.
  for (let offset=0;offset<runners.length;offset+=4) await Promise.all(runners.slice(offset,offset+4).map(async runner=>{
    try {
      if (suspend) {
        for (const label of routingLabels) {
          if (!(runner.labels || []).some(l=>l.name===label)) continue;
          try { await github.request(`orgs/TauCetiProject/actions/runners/${runner.id}/labels/${label}`,{kind:'runners',method:'DELETE'}); }
          catch(error) { if (!(error instanceof UpstreamError) || error.status!==404) throw error; }
        }
      } else {
        const slot=owned.get(runner.id);
        if (!slot) { pending.push(runner.id); return; } // Never invent an orphan's authorized classes.
        const kinds=slot.kinds || ['staging'];
        const labels=[...(kinds.includes('staging')?['tauceti-fleet']:[]),...kinds.map(k=>'tauceti-fleet-'+k)];
        await github.request(`orgs/TauCetiProject/actions/runners/${runner.id}/labels`,{kind:'runners',method:'POST',data:{labels}});
      }
      updated.push(runner.id);
    } catch { pending.push(runner.id); }
  }));
  if (!suspend && !pending.length) host.suspended=false;
  host.suspension_cleanup_pending=!!pending.length;
  await storage.put('host:'+host.machine_id,host);
  return {suspended:!!host.suspended,cleanup_pending:!!pending.length,updated_runners:updated.sort((a,b)=>a-b),pending_runners:pending.sort((a,b)=>a-b)};
}
