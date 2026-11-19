export const MAX_JOBS=4096;

export function capacity(host) {
  // Older controllers require max_jobs in /v1/policy. It is the protocol's
  // slot-index bound, never a per-host permission; local --max-jobs is authoritative.
  return {max_jobs:MAX_JOBS,kinds:host.kinds ?? ['staging']};
}
// Build classes share the TauCetiFleet group. Each special class has its own
// selected-repository group; the broker, never the host, chooses the group.
export const GROUPS={pr:3,staging:3,'queue-control':6,'pr-status':7};
export const SPECIAL_GROUPS={'queue-control':6,'pr-status':7};
/** Classes an operator may authorize for a host. */
export function hostKinds(kinds) {
  return Array.isArray(kinds) && kinds.length>=1 && new Set(kinds).size===kinds.length &&
    kinds.every(k=>Object.hasOwn(GROUPS,k));
}
/** Classes of one runner: build classes together, or exactly one special class. */
export function runnerKinds(kinds) {
  return hostKinds(kinds) && (kinds.length===1 || kinds.every(k=>GROUPS[k]===3));
}
export function groupFor(kinds) {
  return GROUPS[kinds[0]];
}
/** Groups a host's runners can be in: its classes' and its live or reserved slots'. */
export function hostGroups(host) {
  const groups=(host.kinds ?? ['staging']).filter(k=>Object.hasOwn(GROUPS,k)).map(k=>GROUPS[k]);
  for (const slot of Object.values(slots(host))) {
    if (slot.pending) groups.push(slot.pending.group ?? 3);
    if (slot.runner_id) groups.push(groupFor(slot.kinds || ['staging']));
  }
  return [...new Set(groups.filter(g=>g!==undefined))].sort((a,b)=>a-b);
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
