import {UpstreamError,InventoryError,PolicyError} from './github.mjs';
import {slots} from './capacity.mjs';

export const RECOVERY_DELAY=120000;
export const STUCK_AFTER=600000;

// Persist only an allowlisted diagnosis, never exception messages or bodies.
export function diagnosis(error) {
  if (error instanceof InventoryError) return {kind:'inventory_incomplete'};
  if (error instanceof PolicyError) return {kind:'untrusted_runner_policy'};
  return error instanceof UpstreamError
    ? {kind:'github_http',status:error.status,
      request_id:/^[A-Za-z0-9:-]{1,128}$/.test(error.requestId || '') ? error.requestId : null}
    : {kind:'transport_or_internal'};
}

class RecoveryBlocked extends Error {
  constructor(code) { super(code); this.code=code; }
}

export class RegistrationRecovery {
  constructor(storage,github) { this.storage=storage; this.github=github; }
  async hosts() { return Array.from((await this.storage.list({prefix:'host:'})).values()); }
  async schedule(hosts=undefined,replace=false) {
    hosts ??= await this.hosts();
    const times=hosts.flatMap(h=>Object.values(slots(h)).filter(s=>s.pending)
      .map(s=>s.pending.retry_at ?? s.pending.at+RECOVERY_DELAY));
    if (!times.length) { if (replace) await this.storage.deleteAlarm(); return; }
    const when=Math.max(Date.now()+1000,Math.min(...times));
    const current=await this.storage.getAlarm();
    if (replace || current===null || when<current) await this.storage.setAlarm(when);
  }
  async reserve(host) {
    // The intent and its wakeup survive together, including an interrupted POST.
    await this.storage.transaction(async tx=>{
      await tx.put('host:'+host.machine_id,host);
      const when=Date.now()+RECOVERY_DELAY;
      const current=await tx.getAlarm();
      if (current===null || when<current) await tx.setAlarm(when);
    });
  }
  async reconcile(host,index) {
    const slot=slots(host)[String(index)];
    if (!slot?.pending) throw new RecoveryBlocked('no_pending_registration');
    if (Date.now()-slot.pending.at<RECOVERY_DELAY) throw new RecoveryBlocked('registration_settling');
    await this.github.policy({fresh:true});
    // A complete inventory is required even when there are no matching runners.
    const matches=(await this.github.runners()).filter(r=>r.name===slot.pending.name);
    if (matches.some(r=>r.busy || r.status!=='offline')) throw new RecoveryBlocked('runner_active');
    for (const runner of matches) {
      try {
        const current=await this.github.request(`orgs/TauCetiProject/actions/runners/${runner.id}`,{kind:'runners'});
        if (current.busy || current.status!=='offline' || current.name!==slot.pending.name)
          throw new RecoveryBlocked('runner_active');
        await this.github.request(`orgs/TauCetiProject/actions/runners/${runner.id}`,{kind:'runners',method:'DELETE'});
      } catch (error) { if (!(error instanceof UpstreamError) || error.status!==404) throw error; }
    }
    const recovered={...slot,last_recovery:{at:Date.now(),reserved_at:slot.pending.at,removed:matches.map(r=>r.id),
      failure:slot.pending.failure || {kind:'interrupted'}}};
    delete recovered.pending;
    const saved={...host,slots:{...slots(host),[String(index)]:recovered}};
    await this.storage.put('host:'+host.machine_id,saved);
    host.slots=saved.slots;
    console.log(JSON.stringify({event:'ci_registration_recovered',machine_id:host.machine_id,slot:index,...recovered.last_recovery}));
    return {reconciled:true,removed:matches.map(r=>r.id)};
  }
  async run() {
    // Schedule another pass first, so a process interruption or exhausted alarm
    // retries cannot strand this work. Once all intents clear, stop waking up.
    await this.storage.setAlarm(Date.now()+RECOVERY_DELAY);
    const hosts=await this.hosts();
    for (const host of hosts) for (const [index,slot] of Object.entries(slots(host))) {
      if (!slot.pending || (slot.pending.retry_at ?? slot.pending.at+RECOVERY_DELAY)>Date.now()) continue;
      try { await this.reconcile(host,Number(index)); }
      catch (error) {
        const pending=slot.pending;
        pending.attempts=(pending.attempts || 0)+1;
        pending.recovery_error=error instanceof RecoveryBlocked ? {kind:error.code} : diagnosis(error);
        pending.retry_at=Date.now()+Math.min(900000,RECOVERY_DELAY*2**Math.min(pending.attempts-1,3));
        await this.storage.put('host:'+host.machine_id,host);
        console.error(JSON.stringify({event:'ci_registration_recovery_blocked',machine_id:host.machine_id,
          slot:Number(index),age_ms:Date.now()-pending.at,attempts:pending.attempts,...pending.recovery_error}));
      }
    }
    await this.schedule(hosts,true);
  }
}
