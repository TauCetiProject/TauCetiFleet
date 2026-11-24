import {test} from 'node:test';
import assert from 'node:assert/strict';
import {GitHub,PolicyError} from '../src/github.mjs';
const prefix='TauCetiProject/TauCeti/.github/workflows/';
const build=prefix+'pr-build.yml@refs/heads/main';
const nightly=prefix+'nightly-verify.yml@refs/heads/main';
const smoke=prefix+'fleet-smoke.yml@refs/heads/main'; // deleted from TauCeti; no longer admitted
const picker=prefix+'pick-runner.yml@refs/heads/main';
const labelRouter=prefix+'full-lint-label.yml@refs/heads/main';
const apiDocs=prefix+'api-docs.yml@refs/heads/main';
function client(workflows,calls=[]) {
  const github=new GitHub({});
  github.request=async path=>{
    calls.push(path);
    return {id:3,name:'TauCetiFleet',restricted_to_workflows:true,selected_workflows:workflows};
  };
  return github;
}
test('old and expanded trusted policies allow a live update',async()=>{
  for(const workflows of [[build],[build,nightly],[build,picker],[build,labelRouter],[build,apiDocs],
    [build,nightly,picker,labelRouter,apiDocs]])
    assert.deepEqual(await client(workflows).policy({fresh:true}),{runner_group_id:3});
});
test('nightly access never permits PR refs, other repos or arbitrary workflows',async()=>{
  for(const invalid of [nightly.replace('refs/heads/main','refs/pull/1/merge'),
    picker.replace('refs/heads/main','refs/pull/1/merge'),
    labelRouter.replace('refs/heads/main','refs/pull/1/merge'),
    apiDocs.replace('refs/heads/main','refs/pull/1/merge'),
    apiDocs.replace('refs/heads/main','refs/heads/other'),
    apiDocs.replace('TauCetiProject/TauCeti/','someone/Other/'),
    nightly.replace('TauCetiProject/TauCeti/','someone/Other/'),
    picker.replace('TauCetiProject/TauCeti/','someone/Other/'),
    prefix+'untrusted.yml@refs/heads/main',smoke])
    await assert.rejects(client([build,invalid]).policy({fresh:true}),PolicyError);
  await assert.rejects(client([nightly]).policy({fresh:true}),PolicyError);
  await assert.rejects(client([picker,labelRouter]).policy({fresh:true}),PolicyError);
  await assert.rejects(client([apiDocs]).policy({fresh:true}),PolicyError);
});

test('the shared harness at TauCetiTools main is accepted, but no other branch or fork',async()=>{
 const main='TauCetiProject/TauCetiTools/.github/workflows/pr-build.yml@refs/heads/main';
 assert.deepEqual(await client([build,nightly,main]).policy({fresh:true}),{runner_group_id:3});
 for (const bad of [main.replace('refs/heads/main','main'),main.replace('refs/heads/main','refs/heads/other'),
   main.replace('TauCetiProject/','someone/'),main.replace('pr-build.yml','publish-lake-cache.yml')])
  await assert.rejects(client([build,bad]).policy({fresh:true}),PolicyError);
});

test('the API-docs and nightly harness files at TauCetiTools main are accepted, nothing else of the harness',async()=>{
 const harness=name=>'TauCetiProject/TauCetiTools/.github/workflows/'+name+'@refs/heads/main';
 assert.deepEqual(await client([build,harness('pr-build.yml'),harness('api-docs.yml'),harness('nightly-verify.yml')])
   .policy({fresh:true}),{runner_group_id:3});
 assert.deepEqual(await client([build,apiDocs,nightly,harness('api-docs.yml')]).policy({fresh:true}),{runner_group_id:3});
 for (const bad of [harness('pick-runner.yml'),harness('pages.yml'),harness('api-docs.yml').replace('refs/heads/main','refs/heads/other'),
   harness('nightly-verify.yml').replace('TauCetiProject/','someone/')])
  await assert.rejects(client([build,bad]).policy({fresh:true}),PolicyError);
 await assert.rejects(client([harness('api-docs.yml')]).policy({fresh:true}),PolicyError);
});

test('exact harness revisions are no longer admitted, even on TauCetiTools main',async()=>{
 const main='TauCetiProject/TauCetiTools/.github/workflows/pr-build.yml@refs/heads/main';
 for (const pinned of [main.replace('refs/heads/main','abf810bf5064bf549ba76fac568da9d1dbb32362'),
   main.replace('refs/heads/main','8902fa12a4faafc7bb512ab0baa9a452249b93aa')])
  await assert.rejects(client([build,main,pinned]).policy({fresh:true}),PolicyError);
});
