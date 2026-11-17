import {test} from 'node:test';
import assert from 'node:assert/strict';
import {GitHub,PolicyError,UpstreamError} from '../src/github.mjs';
const prefix='TauCetiProject/TauCeti/.github/workflows/';
const build=prefix+'pr-build.yml@refs/heads/main';
const nightly=prefix+'nightly-verify.yml@refs/heads/main';
const smoke=prefix+'fleet-smoke.yml@refs/heads/main';
const picker=prefix+'pick-runner.yml@refs/heads/main';
const labelRouter=prefix+'full-lint-label.yml@refs/heads/main';
const reviewed=new Set(['8902fa12a4faafc7bb512ab0baa9a452249b93aa','150b56c3ab743b1f2a120f0639d98586455d7f2a',
  'fe61c4f22f1e59c4393fe24ed32d22787f3d5e9f','a7662ab81f900586db3586ec3a11c38a5f46962d',
  'abf810bf5064bf549ba76fac568da9d1dbb32362']);
function client(workflows,calls=[]) {
  const github=new GitHub({});
  github.request=async path=>{
    calls.push(path);
    const compare=/^repos\/TauCetiProject\/TauCetiTools\/compare\/([0-9a-f]{40})\.\.\.main\?per_page=1$/.exec(path);
    if (compare) {
      if (!reviewed.has(compare[1])) throw new UpstreamError(404);
      return {status:'ahead'};
    }
    return {id:3,name:'TauCetiFleet',restricted_to_workflows:true,selected_workflows:workflows};
  };
  github.anonymous=async()=>{ throw new Error('tests never reach the network'); };
  return github;
}
test('old and expanded trusted policies allow a live update',async()=>{
  for(const workflows of [[build],[build,smoke],[build,nightly],[build,smoke,nightly],
    [build,picker],[build,labelRouter],[build,smoke,nightly,picker,labelRouter]])
    assert.deepEqual(await client(workflows).policy({fresh:true}),{runner_group_id:3});
});
test('nightly access never permits PR refs, other repos or arbitrary workflows',async()=>{
  for(const invalid of [nightly.replace('refs/heads/main','refs/pull/1/merge'),
    picker.replace('refs/heads/main','refs/pull/1/merge'),
    labelRouter.replace('refs/heads/main','refs/pull/1/merge'),
    nightly.replace('TauCetiProject/TauCeti/','someone/Other/'),
    picker.replace('TauCetiProject/TauCeti/','someone/Other/'),
    prefix+'untrusted.yml@refs/heads/main'])
    await assert.rejects(client([build,invalid]).policy({fresh:true}),PolicyError);
  await assert.rejects(client([nightly]).policy({fresh:true}),PolicyError);
  await assert.rejects(client([picker,labelRouter]).policy({fresh:true}),PolicyError);
});

test('source staging permits exactly the reviewed shared harness revision',async()=>{
 const source='TauCetiProject/TauCetiTools/.github/workflows/pr-build.yml@8902fa12a4faafc7bb512ab0baa9a452249b93aa';
 assert.deepEqual(await client([build,nightly,source]).policy({fresh:true}),{runner_group_id:3});
 const controlSource=source.split('@')[0]+'@150b56c3ab743b1f2a120f0639d98586455d7f2a';
 assert.deepEqual(await client([build,nightly,source,controlSource]).policy({fresh:true}),{runner_group_id:3});
 const proofSource=source.split('@')[0]+'@fe61c4f22f1e59c4393fe24ed32d22787f3d5e9f';
 assert.deepEqual(await client([build,nightly,source,controlSource,proofSource]).policy({fresh:true}),{runner_group_id:3});
 const finalizerSource=source.split('@')[0]+'@a7662ab81f900586db3586ec3a11c38a5f46962d';
 assert.deepEqual(await client([build,nightly,source,controlSource,proofSource,finalizerSource]).policy({fresh:true}),{runner_group_id:3});
 for (const bad of [source.split('@')[0]+'@refs/heads/other',source.slice(0,-1)+'0'])
  await assert.rejects(client([build,bad]).policy({fresh:true}),PolicyError);
});

test('the shared harness at TauCetiTools main is accepted, but no other branch or fork',async()=>{
 const main='TauCetiProject/TauCetiTools/.github/workflows/pr-build.yml@refs/heads/main';
 const calls=[];
 assert.deepEqual(await client([build,nightly,main],calls).policy({fresh:true}),{runner_group_id:3});
 assert.equal(calls.filter(c=>c.includes('/compare/')).length,0);
 for (const bad of [main.replace('refs/heads/main','main'),main.replace('refs/heads/main','refs/heads/other'),
   main.replace('TauCetiProject/','someone/'),main.replace('pr-build.yml','publish-lake-cache.yml')])
  await assert.rejects(client([build,bad]).policy({fresh:true}),PolicyError);
});

test('any harness revision on TauCetiTools main is accepted without a broker release',async()=>{
 const pinned='TauCetiProject/TauCetiTools/.github/workflows/pr-build.yml@abf810bf5064bf549ba76fac568da9d1dbb32362';
 const calls=[];
 const github=client([build,pinned],calls);
 assert.deepEqual(await github.policy({fresh:true}),{runner_group_id:3});
 assert.deepEqual(await github.policy({fresh:true}),{runner_group_id:3});
 assert.equal(calls.filter(c=>c.includes('/compare/')).length,1);
 for (const bad of ['TauCetiProject/TauCetiTools/.github/workflows/pr-build.yml@'+'0'.repeat(40),
   'TauCetiProject/TauCetiTools/.github/workflows/publish-lake-cache.yml@abf810bf5064bf549ba76fac568da9d1dbb32362',
   'someone/TauCetiTools/.github/workflows/pr-build.yml@abf810bf5064bf549ba76fac568da9d1dbb32362'])
  await assert.rejects(client([build,bad]).policy({fresh:true}),PolicyError);
});

test('a token that cannot read TauCetiTools falls back to the public compare',async()=>{
 const pinned='TauCetiProject/TauCetiTools/.github/workflows/pr-build.yml@abf810bf5064bf549ba76fac568da9d1dbb32362';
 const github=client([build,pinned]);
 const request=github.request;
 github.request=async path=>{ if(path.includes('/compare/')) throw new UpstreamError(403); return request(path); };
 const anonymous=[];
 github.anonymous=async path=>{ anonymous.push(path); return {status:'identical'}; };
 assert.deepEqual(await github.policy({fresh:true}),{runner_group_id:3});
 assert.equal(anonymous.length,1);
 const refused=client([build,pinned]);
 refused.request=async path=>{ if(path.includes('/compare/')) throw new UpstreamError(403); return request(path); };
 refused.anonymous=async()=>{ throw new UpstreamError(404); };
 await assert.rejects(refused.policy({fresh:true}),PolicyError);
});
