import { DurableObject } from 'cloudflare:workers';
import {GitHub, UpstreamError} from './github.mjs';

const HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
  'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "frame-ancestors 'none'"};
const ORG = 'TauCetiProject';
const APP = 'tauceti-fleet';
function json(value, status=200) {
  return Response.json(value, {status, headers: HEADERS});
}
function html(value, status=200) {
  return new Response('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'+value,
    {status, headers: {...HEADERS, 'Content-Type': 'text/html; charset=utf-8'}});
}
function escape(value) {
  return value.replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;');
}
async function digest(value) {
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))))
    .map(v=>v.toString(16).padStart(2, '0')).join('');
}
async function authenticates(req, token) {
  const header = req.headers.get('Authorization') || '';
  return Boolean(token && header.startsWith('Bearer ') &&
    await digest(header.slice(7)) === await digest(token));
}
export function manifest(origin) {
  return {name: APP, url: 'https://github.com/TauCetiProject/TauCetiFleet',
    description: 'Enroll disposable TauCeti CI runners without distributing GitHub administrative credentials.',
    hook_attributes: {url: origin+'/webhook/github', active: false},
    redirect_url: origin+'/registration-complete', public: false,
    default_permissions: {metadata: 'read', actions: 'read', statuses: 'read', organization_self_hosted_runners: 'write'},
    default_events: [], request_oauth_on_install: false};
}

export class FleetRegistry extends DurableObject {
  constructor(ctx, env) { super(ctx, env); this.github=new GitHub(env); this.queue=Promise.resolve(); }
  fetch(req) {
    // Serialize the whole decision, including GitHub awaits, so concurrent host
    // requests cannot register twice or overwrite an administrator's revocation.
    const result=this.queue.then(()=>this.handle(req));
    this.queue=result.catch(()=>{});
    return result;
  }
  async handle(req) {
    const url = new URL(req.url);
    // All mutations and authorization checks run on one strongly consistent object.
    if (url.pathname === '/healthz') {
      return json({service: 'tauceti-ci-broker', app_configured: Boolean(this.env.GITHUB_APP_ID && this.env.GITHUB_PRIVATE_KEY)});
    }
    if (url.pathname === '/setup/register' && req.method === 'GET') {
      return html(`<title>Register TauCeti Fleet</title><h1>Register the TauCeti Fleet GitHub App</h1>
        <p>Creates a private App owned by TauCetiProject. It reads CI results and manages organization runners.</p>
        <div id="form">Checking setup link…</div><script>
        const token=location.hash.slice(1); history.replaceState(null,'',location.pathname);
        fetch('/setup/manifest',{headers:{Authorization:'Bearer '+token}}).then(async r=>{
          document.getElementById('form').innerHTML=r.ok?await r.text():'This setup link is invalid or expired.';
        });</script>`);
    }
    if (url.pathname === '/admin/setup' && req.method === 'POST') {
      if (!await authenticates(req, this.env.ADMIN_TOKEN)) return json({error: 'unauthorized'}, 401);
      const value = await req.json();
      if (!/^[A-Za-z0-9_-]{43,128}$/.test(value.state || '')) return json({error: 'invalid state'}, 400);
      const prior = await this.ctx.storage.get('setup');
      if (prior?.code || await this.ctx.storage.get('app')) return json({error: 'registration already received'}, 409);
      await this.ctx.storage.put('setup', {hash: await digest(value.state), expires: Date.now()+3600000});
      return json({ready: true});
    }
    if (url.pathname === '/setup/manifest' && req.method === 'GET') {
      const setup = await this.ctx.storage.get('setup');
      const token = (req.headers.get('Authorization') || '').replace(/^Bearer /, '');
      if (!setup || setup.expires < Date.now() || setup.hash !== await digest(token) || setup.code)
        return json({error: 'invalid or expired setup'}, 401);
      const value = escape(JSON.stringify(manifest(url.origin)));
      return html(`<form method="post" action="https://github.com/organizations/${ORG}/settings/apps/new?state=${token}">
        <input type="hidden" name="manifest" value="${value}"><button>Create tauceti-fleet on GitHub</button></form>`);
    }
    if (url.pathname === '/registration-complete' && req.method === 'GET') {
      const setup = await this.ctx.storage.get('setup');
      const state = url.searchParams.get('state') || '';
      const code = url.searchParams.get('code') || '';
      if (!setup || setup.expires < Date.now() || setup.hash !== await digest(state) || !/^[a-f0-9]{40}$/i.test(code))
        return html('<h1>Invalid or expired registration callback</h1>', 400);
      if (setup.code && setup.code !== code) return html('<h1>Registration already received</h1>', 409);
      await this.ctx.storage.put('setup', {...setup, code});
      return html(`<title>App registered</title><h1>Registration received</h1>
        <p>The setup process will save the App credentials privately. Next, install the App only on TauCeti.</p>
        <p><a href="https://github.com/organizations/${ORG}/settings/apps/${APP}/installations" rel="noreferrer">Install the App on TauCetiProject</a></p>
        <script>history.replaceState(null,'',location.pathname)</script>`);
    }
    if (url.pathname === '/admin/setup' && req.method === 'GET') {
      if (!await authenticates(req, this.env.ADMIN_TOKEN)) return json({error: 'unauthorized'}, 401);
      const setup = await this.ctx.storage.get('setup');
      return json(setup && setup.expires > Date.now() && setup.code ? {code: setup.code} : {pending: true});
    }
    if (url.pathname === '/admin/setup' && req.method === 'DELETE') {
      if (!await authenticates(req, this.env.ADMIN_TOKEN)) return json({error: 'unauthorized'}, 401);
      await this.ctx.storage.delete('setup');
      return json({cleared: true});
    }
    if (url.pathname.startsWith('/admin/') && await authenticates(req,this.env.ADMIN_TOKEN)) {
      if (url.pathname==='/admin/invites' && req.method==='POST') {
        const input=await req.json();
        if (!/^[a-zA-Z0-9_.-]{1,64}$/.test(input.name || '') || !/^[a-f0-9]{64}$/.test(input.hash || ''))
          return json({error:'invalid invite'},400);
        await this.ctx.storage.put('invite:'+input.hash,{name:input.name,expires:Date.now()+3600000});
        return json({expires_in:3600});
      }
      if (url.pathname==='/admin/hosts' && req.method==='GET') {
        const hosts=await this.ctx.storage.list({prefix:'host:',limit:1000});
        return json({hosts:Array.from(hosts.values(), h=>({machine_id:h.machine_id,name:h.name,revoked:!!h.revoked,
          runner_id:h.runner_id || null,registration_pending:!!h.pending,created_at:h.created_at}))});
      }
      const revoke=url.pathname.match(/^\/admin\/hosts\/([a-f0-9]{32})$/);
      if (revoke && req.method==='DELETE') {
        const host=await this.ctx.storage.get('host:'+revoke[1]);
        if (!host) return json({error:'not found'},404);
        await this.ctx.storage.put('host:'+revoke[1],{...host,revoked:true});
        return json({revoked:true});
      }
      const reconcile=url.pathname.match(/^\/admin\/hosts\/([a-f0-9]{32})\/reconcile$/);
      if (reconcile && req.method==='POST') {
        const host=await this.ctx.storage.get('host:'+reconcile[1]);
        if (!host?.pending) return json({error:'no pending registration'},409);
        if (Date.now()-host.pending.at<120000) return json({error:'wait two minutes before reconciling a registration'},409);
        await this.github.policy();
        const matches=[]; let complete=false;
        for (let page=1;page<=20;page++) {
          const data=await this.github.request(`orgs/${ORG}/actions/runner-groups/3/runners?per_page=100&page=${page}`,{kind:'runners'});
          matches.push(...data.runners.filter(r=>r.name===host.pending.name));
          if (page*100>=data.total_count) { complete=true; break; }
          if (!data.runners.length) break;
        }
        if (!complete) return json({error:'runner inventory incomplete'},503);
        if (matches.some(r=>r.busy || r.status!=='offline')) return json({error:'matching runner is active; inspect in GitHub before recovery'},409);
        for (const runner of matches) {
          try { await this.github.request(`orgs/${ORG}/actions/runners/${runner.id}`,{kind:'runners',method:'DELETE'}); }
          catch (e) { if (!(e instanceof UpstreamError) || e.status!==404) throw e; }
        }
        delete host.pending;
        await this.ctx.storage.put('host:'+host.machine_id,host);
        return json({reconciled:true,removed:matches.map(r=>r.id)});
      }
    }
    if (url.pathname==='/v1/enroll' && req.method==='POST') {
      const input=await req.json();
      if (!/^[a-f0-9]{32}$/.test(input.machine_id || '') || !/^[A-Za-z0-9_-]{43,128}$/.test(input.host_token || ''))
        return json({error:'invalid host identity'},400);
      const token=(req.headers.get('Authorization') || '').replace(/^Bearer /,'');
      const inviteHash=await digest(token);
      const key='invite:'+inviteHash;
      const invite=await this.ctx.storage.get(key);
      const hash=await digest(input.host_token);
      // The host persists its generated credential before redeeming. An exact retry
      // can recover a lost response without admitting a second machine.
      if (!invite || invite.expires<Date.now()) return json({error:'invalid or expired invite'},401);
      const old=await this.ctx.storage.get('host:'+input.machine_id);
      if (invite.machine_id && (invite.machine_id!==input.machine_id || old?.hash!==hash || old.revoked))
        return json({error:'invite already used'},409);
      if (!invite.machine_id && old) return json({error:'host already enrolled'},409);
      await this.ctx.storage.transaction(async tx=>{
        await tx.put(key,{...invite,machine_id:input.machine_id});
        await tx.put('host:'+input.machine_id,old || {machine_id:input.machine_id,hash,name:invite.name,created_at:Date.now(),issued:[]});
      });
      return json({machine_id:input.machine_id,runner_group_id:3});
    }
    if (url.pathname.startsWith('/v1/')) {
      const id=req.headers.get('X-Fleet-Machine') || '';
      const token=(req.headers.get('Authorization') || '').replace(/^Bearer /,'');
      const host=/^[a-f0-9]{32}$/.test(id)?await this.ctx.storage.get('host:'+id):null;
      if (!host || host.revoked || host.hash!==await digest(token)) return json({error:'unauthorized'},401);
      const save=()=>this.ctx.storage.put('host:'+id,host);
      if (url.pathname==='/v1/policy' && req.method==='GET') return json(await this.github.policy());
      if (url.pathname==='/v1/runners' && req.method==='GET') {
        let runner;
        if (host.runner_id) {
          try { runner=await this.github.request(`orgs/${ORG}/actions/runners/${host.runner_id}`,{kind:'runners'}); }
          catch (e) {
            if (!(e instanceof UpstreamError) || e.status!==404) throw e;
            delete host.runner_id; delete host.jit; await save();
          }
        }
        return json({runners:runner?[runner]:[]});
      }
      if (url.pathname==='/v1/runners' && req.method==='POST') {
        const input=await req.json();
        if (!new RegExp('^fleet-'+id.slice(0,12)+'-[a-f0-9]{16}$').test(input.name || ''))
          return json({error:'invalid runner name'},400);
        if (host.pending) return json({error:'registration outcome uncertain; operator reconciliation required'},409);
        if (host.runner_id) {
          if (host.runner_name===input.name && host.jit) return json({runner_id:host.runner_id,encoded_jit_config:host.jit});
          return json({error:'host already has registered capacity'},409);
        }
        await this.github.policy();
        // Reserve before the non-idempotent GitHub POST. An ambiguous result fails
        // closed instead of silently creating extra runners on a retry.
        host.pending={name:input.name,at:Date.now()};
        await save();
        const value=await this.github.request(`orgs/${ORG}/actions/runners/generate-jitconfig`,{kind:'runners',method:'POST',
          data:{name:input.name,runner_group_id:3,labels:['self-hosted','linux','x64','tauceti-fleet','fleet-'+id],work_folder:'_work'}});
        host.runner_id=value.runner.id; host.runner_name=input.name; host.jit=value.encoded_jit_config;
        host.issued=[...host.issued,value.runner.id].slice(-4096);
        delete host.pending;
        await save();
        return json({runner_id:host.runner_id,encoded_jit_config:host.jit});
      }
      const retire=url.pathname.match(/^\/v1\/runners\/([1-9][0-9]*)$/);
      if (retire && req.method==='DELETE') {
        const runnerId=Number(retire[1]);
        if (runnerId!==host.runner_id) {
          // Repeating retirement of our own old runner is harmless.
          return host.issued.includes(runnerId)?json({retired:true}):json({error:'not found'},404);
        }
        try { await this.github.request(`orgs/${ORG}/actions/runners/${runnerId}`,{kind:'runners',method:'DELETE'}); }
        catch (e) { if (!(e instanceof UpstreamError) || e.status!==404) throw e; }
        delete host.runner_id; delete host.jit;
        await save();
        return json({retired:true});
      }
      // Fixed read-only repository endpoints, never an arbitrary GitHub proxy.
      const read=url.pathname.match(/^\/v1\/github\/(actions\/runs\/[1-9][0-9]*(?:\/jobs)?|commits\/[a-f0-9]{40}\/statuses)$/);
      if (read && req.method==='GET') {
        const query=read[1].endsWith('/jobs')?'?filter=all&per_page=100':read[1].endsWith('/statuses')?'?per_page=100':'';
        const result=await this.github.request('repos/TauCetiProject/TauCeti/'+read[1]+query);
        if (read[1].endsWith('/jobs')) {
          if (result.total_count>result.jobs.length) throw new Error('Incomplete job inventory');
          result.jobs=result.jobs.filter(j=>host.issued.includes(j.runner_id));
          result.total_count=result.jobs.length;
        }
        return json(result);
      }
    }
    return json({error: 'not found'}, 404);
  }
}

export default {
  async fetch(req, env) {
    try {
      if (Number(req.headers.get('Content-Length') || 0) > 16384) return json({error: 'request too large'}, 413);
      if (req.body) {
        const reader=req.body.getReader();
        const chunks=[]; let size=0;
        while (true) {
          const {done,value}=await reader.read();
          if (done) break;
          size+=value.length;
          if (size>16384) { await reader.cancel(); return json({error:'request too large'},413); }
          chunks.push(value);
        }
        const body=new Uint8Array(size); let offset=0;
        for (const chunk of chunks) { body.set(chunk,offset); offset+=chunk.length; }
        req=new Request(req,{body});
      }
      return await env.REGISTRY.get(env.REGISTRY.idFromName('fleet')).fetch(req);
    } catch {
      // Never log callback URLs, setup codes, tokens, or upstream response bodies.
      return json({error: 'request failed'}, 503);
    }
  }
};
