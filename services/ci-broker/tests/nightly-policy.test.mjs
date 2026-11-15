import {test} from 'node:test';
import assert from 'node:assert/strict';
import {GitHub,PolicyError} from '../src/github.mjs';
const prefix='TauCetiProject/TauCeti/.github/workflows/';
const build=prefix+'pr-build.yml@refs/heads/main';
const nightly=prefix+'nightly-verify.yml@refs/heads/main';
const smoke=prefix+'fleet-smoke.yml@refs/heads/main';
function client(workflows) {
  const github=new GitHub({});
  github.request=async()=>({id:3,name:'TauCetiFleet',restricted_to_workflows:true,selected_workflows:workflows});
  return github;
}
test('old and expanded trusted policies allow a live update',async()=>{
  for(const workflows of [[build],[build,smoke],[build,nightly],[build,smoke,nightly]])
    assert.deepEqual(await client(workflows).policy({fresh:true}),{runner_group_id:3});
});
test('nightly access never permits PR refs, other repos or arbitrary workflows',async()=>{
  for(const invalid of [nightly.replace('refs/heads/main','refs/pull/1/merge'),
    nightly.replace('TauCetiProject/TauCeti/','someone/Other/'),prefix+'untrusted.yml@refs/heads/main'])
    await assert.rejects(client([build,invalid]).policy({fresh:true}),PolicyError);
  await assert.rejects(client([nightly]).policy({fresh:true}),PolicyError);
});

test('source staging permits exactly the reviewed shared harness revision',async()=>{
 const source='TauCetiProject/TauCetiTools/.github/workflows/pr-build.yml@8902fa12a4faafc7bb512ab0baa9a452249b93aa';
 assert.deepEqual(await client([build,nightly,source]).policy({fresh:true}),{runner_group_id:3});
 const controlSource=source.split('@')[0]+'@150b56c3ab743b1f2a120f0639d98586455d7f2a';
 assert.deepEqual(await client([build,nightly,source,controlSource]).policy({fresh:true}),{runner_group_id:3});
 for (const bad of [source.split('@')[0]+'@refs/heads/main',source.slice(0,-1)+'0'])
  await assert.rejects(client([build,bad]).policy({fresh:true}),PolicyError);
});
