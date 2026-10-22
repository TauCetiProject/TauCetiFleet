export class UpstreamError extends Error {
  constructor(status) { super('GitHub request failed'); this.status=status; }
}
const encoder=new TextEncoder();
const b64=bytes=>btoa(String.fromCharCode(...new Uint8Array(bytes))).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');
const encode=value=>b64(encoder.encode(JSON.stringify(value)));
export class GitHub {
  constructor(env) { this.env=env; this.tokens=new Map(); this.cachedPolicy=null; }
  async raw(path, token, method='GET', data) {
    const response=await fetch('https://api.github.com/'+path, {method,
      headers:{Accept:'application/vnd.github+json', Authorization:'Bearer '+token,
        'User-Agent':'TauCetiFleet/broker', 'X-GitHub-Api-Version':'2022-11-28', 'Content-Type':'application/json'},
      body:data===undefined?undefined:JSON.stringify(data), signal:AbortSignal.timeout(20000)});
    if (!response.ok) throw new UpstreamError(response.status);
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
  async policy({fresh=false}={}) {
    if (!fresh && this.cachedPolicy?.expires>Date.now()) return this.cachedPolicy.value;
    const group=await this.request('orgs/TauCetiProject/actions/runner-groups/3',{kind:'runners'});
    const required='TauCetiProject/TauCeti/.github/workflows/pr-build.yml@refs/heads/main';
    const allowed=new Set([required,'TauCetiProject/TauCeti/.github/workflows/fleet-smoke.yml@refs/heads/main']);
    const selected=group.selected_workflows || [];
    if (group.id!==3 || group.name!=='TauCetiFleet' || !group.restricted_to_workflows ||
        !selected.includes(required) || selected.some(w=>!allowed.has(w))) throw new Error('Untrusted runner policy');
    const value={runner_group_id:3};
    this.cachedPolicy={expires:Date.now()+60000,value};
    return value;
  }
}
