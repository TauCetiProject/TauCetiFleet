import {PolicyError} from './github.mjs';

// TauCeti's protected main names the shared CI harness revision in `.tauceti/ci-source.json`.
// That file is the only source of truth: the runner-group allow-lists are derived from it here,
// on a Durable Object alarm and whenever the queue asks before a staging dispatch.
export const PROJECT={name:'TauCetiProject/TauCeti',id:1256881326};
export const HARNESS={name:'TauCetiProject/TauCetiTools',id:1426390374};
export const SOURCE_PATH='.tauceti/ci-source.json';
// The harness reusable workflows whose jobs run on each restricted group. Host controllers and
// `GitHub.policy` accept exactly these files, so a new one needs a Fleet change either way.
export const GROUPS=[
  {id:3,name:'TauCetiFleet',files:['pr-build.yml']},
  {id:6,name:'TauCetiQueueControl',files:['pr-build.yml','publish-lake-cache.yml']},
];
export const INTERVAL=60000;
// A revision stays admitted this long after main, an unfinished run or the group last needed it.
export const RETAIN=24*3600*1000;
// The queue's nudge may force a fresh read at most this often.
export const REFRESH=5000;
const ENTRY=/^TauCetiProject\/TauCetiTools\/\.github\/workflows\/([A-Za-z0-9_.-]+)@([0-9a-f]{40})$/;
const HEX40=/^[0-9a-f]{40}$/;

export const entry=(file,revision)=>`${HARNESS.name}/.github/workflows/${file}@${revision}`;
export function harnessEntry(value) {
  const match=ENTRY.exec(value);
  return match && {file:match[1],revision:match[2]};
}

// The protected-main policy, reduced to what admission needs. Anything unexpected admits nothing.
export function parseSource(text) {
  let value;
  try { value=JSON.parse(text); } catch { throw new PolicyError(); }
  const same=(identity,expected)=>identity?.name===expected.name && identity?.id===expected.id;
  if (value?.schema!=='tauceti-ci.source-candidate/v2' || !same(value.harness,HARNESS) ||
      value.harness?.forge?.kind!=='github' || !same(value.execution,PROJECT) ||
      typeof value.harness_revision!=='string' || !HEX40.test(value.harness_revision) ||
      typeof value.workflow!=='string' || !/^\.github\/workflows\/[A-Za-z0-9_.-]+\.ya?ml$/.test(value.workflow))
    throw new PolicyError();
  return {revision:value.harness_revision,workflow:value.workflow};
}

export class HarnessAdmission {
  constructor(storage,github,{now=()=>Date.now()}={}) { this.storage=storage; this.github=github; this.now=now; }
  async state() { return (await this.storage.get('harness')) ?? {revisions:{}}; }

  // Admit main's revision in every group before anything can use it, and prune only what no
  // recent main, no unfinished caller run and no recent group observation still needs.
  async reconcile() {
    const now=this.now(), state=await this.state();
    try {
      const source=parseSource(await this.github.harnessSource());
      if (!await this.github.reviewedHarness(source.revision)) throw new PolicyError();
      const needed={...state.revisions,[source.revision]:now};
      const groups=[];
      for (const spec of GROUPS) {
        const group=await this.github.group(spec.id);
        if (group?.id!==spec.id || group.name!==spec.name || group.restricted_to_workflows!==true ||
            !Array.isArray(group.selected_workflows)) throw new PolicyError();
        for (const workflow of group.selected_workflows) {
          const found=harnessEntry(workflow);
          if (found && needed[found.revision]===undefined) needed[found.revision]=now; // grace for the unknown
        }
        groups.push({spec,group});
      }
      const expired=revision=>revision!==source.revision && now-needed[revision]>RETAIN;
      let running=new Set();
      if (groups.some(({group})=>group.selected_workflows.some(w=>{const f=harnessEntry(w);return f && expired(f.revision);}))) {
        // An incomplete listing proves nothing, so it prunes nothing.
        running=await this.github.unfinishedHarness(source.workflow);
        for (const revision of running ?? []) needed[revision]=now;
      }
      const admitted={};
      for (const {spec,group} of groups) {
        const current=group.selected_workflows;
        const keep=current.filter(w=>{
          const found=harnessEntry(w);
          return !found || !spec.files.includes(found.file) || running===null || !expired(found.revision);
        });
        const desired=[...new Set([...keep,...spec.files.map(f=>entry(f,source.revision))])].sort();
        const sorted=[...current].sort();
        if (desired.length!==sorted.length || desired.some((w,i)=>w!==sorted[i])) {
          await this.github.updateGroup(spec.id,{name:spec.name,restricted_to_workflows:true,selected_workflows:desired});
          const check=await this.github.group(spec.id,{fresh:true});
          if (!spec.files.every(f=>check?.selected_workflows?.includes(entry(f,source.revision)))) throw new PolicyError();
          console.log(JSON.stringify({event:'harness_allow_list_reconciled',group:spec.name,revision:source.revision,
            added:desired.filter(w=>!current.includes(w)),removed:current.filter(w=>!desired.includes(w))}));
          admitted[spec.name]=revisions(check.selected_workflows,spec.files);
        } else admitted[spec.name]=revisions(desired,spec.files);
      }
      for (const revision of Object.keys(needed))
        if (expired(revision) && !Object.values(admitted).some(list=>list.includes(revision))) delete needed[revision];
      const next={revisions:needed,main:source.revision,workflow:source.workflow,admitted,checked_at:now};
      await this.storage.put('harness',next);
      return next;
    } catch (error) {
      const failed={...state,error:{kind:error instanceof PolicyError?'untrusted_harness_policy':
        error?.status?'github_http':'transport_or_internal',status:error?.status ?? null},failed_at:now};
      await this.storage.put('harness',failed);
      console.error(JSON.stringify({event:'harness_allow_list_blocked',...failed.error}));
      return failed;
    }
  }

  // Answer whether `revision` is admitted wherever the harness needs it, refreshing when it is not.
  async status(revision) {
    let state=await this.state();
    const stale=!state.checked_at || this.now()-state.checked_at>=INTERVAL;
    if ((stale || !ready(state,revision)) && this.now()-(state.attempted_at ?? 0)>=REFRESH) {
      await this.storage.put('harness',{...state,attempted_at:this.now()});
      state=await this.reconcile();
      await this.storage.put('harness',{...state,attempted_at:this.now()});
    }
    return report(state,revision);
  }

  async tick() {
    await this.reconcile();
    await this.schedule();
  }
  async schedule() {
    const when=this.now()+INTERVAL, current=await this.storage.getAlarm();
    if (current===null || current>when) await this.storage.setAlarm(when);
  }
}

function revisions(workflows,files) {
  const by=new Map();
  for (const w of workflows) {
    const found=harnessEntry(w);
    if (found && files.includes(found.file)) by.set(found.revision,(by.get(found.revision) ?? new Set()).add(found.file));
  }
  return [...by].filter(([,f])=>files.every(x=>f.has(x))).map(([r])=>r).sort();
}
function ready(state,revision) {
  return HEX40.test(revision || '') && GROUPS.every(g=>state.admitted?.[g.name]?.includes(revision));
}
export function report(state,revision) {
  return {main_revision:state.main ?? null,revision:revision ?? null,ready:ready(state,revision),
    admitted:state.admitted ?? {},checked_at:state.checked_at ?? null,error:state.error && state.failed_at>(state.checked_at ?? 0)?state.error:null};
}
