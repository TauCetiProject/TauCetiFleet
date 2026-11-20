import {INFRA_GROUP} from './capacity.mjs';
export class UpstreamError extends Error {
  constructor(status,requestId=null) { super('GitHub request failed'); this.status=status; this.requestId=requestId; }
}
export class InventoryError extends Error {
  constructor() { super('Incomplete runner inventory'); }
}
export class PolicyError extends Error {
  constructor() { super('Untrusted runner policy'); }
}
const TAUCETI='TauCetiProject/TauCeti/.github/workflows/';
// TauCeti runs the shared harness from TauCetiTools main, never another branch or a fork.
const HARNESS='TauCetiProject/TauCetiTools/.github/workflows/';
const main=(prefix,names)=>names.map(name=>prefix+name+'@refs/heads/main');
const expect=(name,required,optional,repository=null)=>({name,required,allowed:new Set([...required,...optional]),
  repositories:repository===null?null:[repository].flat()});
// Our own infrastructure repositories. They take pull requests only from us and
// our agents, so their group admits any workflow of exactly these repositories;
// the repository list is the boundary, checked on every registration.
export const INFRA_REPOSITORIES=['TauCetiCI','TauCetiFleet','TauCetiTools','TauCetiWorker'].map(r=>'TauCetiProject/'+r);
// Expected settings of each runner group the broker registers runners in.
export const CHECKS={
  3:expect('TauCetiFleet',main(TAUCETI,['pr-build.yml']),
    [...main(TAUCETI,['nightly-verify.yml','pick-runner.yml','full-lint-label.yml']),...main(HARNESS,['pr-build.yml'])]),
  6:expect('TauCetiQueueControl',main(TAUCETI,['pick-runner.yml','pr-build.yml','publish-lake-cache.yml']),
    main(HARNESS,['pr-build.yml','publish-lake-cache.yml']),'TauCetiProject/TauCeti'),
  7:expect('TauCetiPRStatus',main(TAUCETI,['pr-status.yml','pr-labels.yml']),[],'TauCetiProject/TauCeti'),
  [INFRA_GROUP]:{name:'TauCetiInfra',workflows:false,repositories:INFRA_REPOSITORIES},
};
const encoder=new TextEncoder();
const b64=bytes=>btoa(String.fromCharCode(...new Uint8Array(bytes))).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');
const encode=value=>b64(encoder.encode(JSON.stringify(value)));
export class GitHub {
  constructor(env) { this.env=env; this.tokens=new Map(); this.cachedPolicy=new Map(); }
  async raw(path, token, method='GET', data) {
    const response=await fetch('https://api.github.com/'+path, {method,
      headers:{Accept:'application/vnd.github+json', ...(token?{Authorization:'Bearer '+token}:{}),
        'User-Agent':'TauCetiFleet/broker', 'X-GitHub-Api-Version':'2022-11-28', 'Content-Type':'application/json'},
      body:data===undefined?undefined:JSON.stringify(data), signal:AbortSignal.timeout(20000)});
    if (!response.ok) throw new UpstreamError(response.status,response.headers.get('x-github-request-id'));
    if (response.status===204) return null;
    const text=await response.text();
    if (text.length>4*1024*1024) throw new Error('GitHub response exceeds limit');
    return JSON.parse(text);
  }
  async token(kind) {
    const cached=this.tokens.get(kind);
    if (cached && cached.expires>Date.now()+60000) return cached.token;
    if (!this.env.GITHUB_APP_ID || !this.env.GITHUB_INSTALLATION_ID || !this.env.GITHUB_PRIVATE_KEY)
      throw new Error('App is not configured');
    const pem=this.env.GITHUB_PRIVATE_KEY.replace(/-----[^-]+-----|\s/g,'');
    const key=await crypto.subtle.importKey('pkcs8', Uint8Array.from(atob(pem),c=>c.charCodeAt(0)),
      {name:'RSASSA-PKCS1-v1_5',hash:'SHA-256'},false,['sign']);
    const now=Math.floor(Date.now()/1000);
    const unsigned=encode({alg:'RS256',typ:'JWT'})+'.'+encode({iat:now-60,exp:now+540,iss:this.env.GITHUB_APP_ID});
    const jwt=unsigned+'.'+b64(await crypto.subtle.sign('RSASSA-PKCS1-v1_5',key,encoder.encode(unsigned)));
    const permissions=kind==='runners'?{organization_self_hosted_runners:'write'}:{metadata:'read',actions:'read',statuses:'read'};
    const grant=await this.raw(`app/installations/${this.env.GITHUB_INSTALLATION_ID}/access_tokens`,jwt,'POST',
      {repositories:['TauCeti'],permissions});
    this.tokens.set(kind,{token:grant.token,expires:Date.parse(grant.expires_at)});
    return grant.token;
  }
  async request(path, {kind='read',method='GET',data}={}) {
    return this.raw(path, await this.token(kind),method,data);
  }
  // Runners of the given broker groups (by default 3 only), each tagged with
  // its group. Runners in other groups are never observed or removed, and a
  // special group's outage cannot affect hosts that do not use it.
  async runners(groups=[3]) {
    const found=[];
    for (const group of groups) {
      if (!Object.hasOwn(CHECKS,group)) throw new InventoryError();
      for (const runner of await this.groupRunners(group)) {
        if (found.some(r=>r.id===runner.id)) throw new InventoryError();
        found.push({...runner,runner_group_id:group});
      }
    }
    return found;
  }
  async groupRunners(group) {
    const found=[],ids=new Set();let total;
    for(let page=1;page<=20;page++) {
      const value=await this.request(`orgs/TauCetiProject/actions/runner-groups/${group}/runners?per_page=100&page=${page}`,{kind:'runners'});
      if(!Array.isArray(value.runners) || !Number.isInteger(value.total_count) || value.total_count<0 ||
         total!==undefined && total!==value.total_count)throw new InventoryError();
      for (const runner of value.runners) {
        if (!Number.isSafeInteger(runner.id) || typeof runner.name!=='string' || ids.has(runner.id))
          throw new InventoryError();
        ids.add(runner.id);
      }
      total=value.total_count;found.push(...value.runners);
      if(found.length===total)return found;
      if(!value.runners.length || found.length>total)break;
    }
    throw new InventoryError();
  }
  async policy({group=3,fresh=false}={}) {
    const cached=this.cachedPolicy.get(group);
    if (!fresh && cached?.expires>Date.now()) return cached.value;
    if (!Number.isInteger(group) || !Object.hasOwn(CHECKS,group)) throw new PolicyError();
    const settings=await this.request(`orgs/TauCetiProject/actions/runner-groups/${group}`,{kind:'runners'});
    const check=CHECKS[group];
    const selected=settings.selected_workflows || [];
    if (settings.id!==group || settings.name!==check.name) throw new PolicyError();
    // A group without a workflow check (infra) is bounded by its repositories alone.
    if (check.workflows!==false && (!settings.restricted_to_workflows ||
        check.required.some(workflow=>!selected.includes(workflow)) ||
        selected.some(workflow=>!check.allowed.has(workflow))))
      throw new PolicyError();
    if (check.repositories) {
      // Special groups must select exactly their repositories: TauCeti for the
      // privileged TauCeti workflows, the infrastructure repositories for infra.
      if (settings.visibility!=='selected') throw new PolicyError();
      const repositories=await this.request(`orgs/TauCetiProject/actions/runner-groups/${group}/repositories?per_page=100`,{kind:'runners'});
      const names=(repositories?.repositories || []).map(r=>r.full_name).sort();
      const expected=[...check.repositories].sort();
      if (repositories?.total_count!==expected.length || names.length!==expected.length ||
          names.some((name,i)=>name!==expected[i])) throw new PolicyError();
    }
    const value={runner_group_id:group};
    this.cachedPolicy.set(group,{expires:Date.now()+60000,value});
    return value;
  }
}
