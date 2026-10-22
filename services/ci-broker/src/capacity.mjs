export function capacity(host) {
  return {max_jobs:host.max_jobs ?? 1,kinds:host.kinds ?? ['staging']};
}
export function validCapacity(value) {
  return Number.isInteger(value.max_jobs) && value.max_jobs>=1 && value.max_jobs<=16 &&
    Array.isArray(value.kinds) && value.kinds.length>=1 && value.kinds.length<=2 &&
    new Set(value.kinds).size===value.kinds.length && value.kinds.every(k=>['pr','staging'].includes(k));
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
