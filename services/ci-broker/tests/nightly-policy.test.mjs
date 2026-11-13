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
