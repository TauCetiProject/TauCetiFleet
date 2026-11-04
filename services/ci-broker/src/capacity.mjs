export const MAX_JOBS=4096;

export function capacity(host) {
  // Older controllers require max_jobs in /v1/policy. It is the protocol's
  // slot-index bound, never a per-host permission; local --max-jobs is authoritative.
  return {max_jobs:MAX_JOBS,kinds:host.kinds ?? ['staging']};
}
export function validKinds(kinds) {
  return Array.isArray(kinds) && kinds.length>=1 && kinds.length<=2 &&
    new Set(kinds).size===kinds.length && kinds.every(k=>['pr','staging'].includes(k));
}
export function slots(host) {
  if (!host.slots) {
    host.slots={};
    // Lazy migration retains ownership and ambiguous registration reservations.
    if (host.runner_id || host.pending) {
      host.slots['0']={};
      for (const key of ['runner_id','runner_name','jit','pending']) {
        if (host[key]!==undefined) host.slots['0'][key]=host[key];
        delete host[key];
      }
    }
  }
  return host.slots;
}
export function labels(id,name,kinds,hostName) {
  const result=['self-hosted','linux','x64','fleet-'+id,'fleet-instance-'+name.split('-').at(-1)];
  if (kinds.includes('staging')) result.push('tauceti-fleet');
  if (hostName) result.push('fleet-host-'+hostName.toLowerCase());
  return result.concat(kinds.map(k=>'tauceti-fleet-'+k));
}
