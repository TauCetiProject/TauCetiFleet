import {test} from 'node:test';
import assert from 'node:assert/strict';
import {GitHub,PolicyError} from '../src/github.mjs';
import {DOCS_GROUP,INFRA_GROUP,PRIORITY_CLASS,hostKinds,runnerKinds,groupFor,hostGroups,labels} from '../src/capacity.mjs';
import {INFRA_REPOSITORIES} from '../src/github.mjs';
import {RegistrationRecovery,RECOVERY_DELAY} from '../src/recovery.mjs';

const workflow=(repo,name)=>'TauCetiProject/'+repo+'/.github/workflows/'+name+'@refs/heads/main';
const queue=[workflow('TauCeti','pick-runner.yml'),workflow('TauCeti','pr-build.yml'),workflow('TauCeti','publish-lake-cache.yml')];
const harness=[workflow('TauCetiTools','pr-build.yml'),workflow('TauCetiTools','publish-lake-cache.yml')];
const status=[workflow('TauCeti','pr-status.yml'),workflow('TauCeti','pr-labels.yml')];
const docs=[workflow('TauCeti','api-docs.yml'),workflow('TauCetiTools','api-docs.yml')];
const names={6:'TauCetiQueueControl',7:'TauCetiPRStatus',[INFRA_GROUP]:'TauCetiInfra',[DOCS_GROUP]:'TauCetiDocs'};
function client(group,{workflows,visibility='selected',restricted=true,repositories=['TauCetiProject/TauCeti'],name=names[group]},calls=[]) {
  const github=new GitHub({});
  github.request=async path=>{
    calls.push(path);
    if (path.startsWith(`orgs/TauCetiProject/actions/runner-groups/${group}/repositories`))
      return {total_count:repositories.length,repositories:repositories.map(full_name=>({full_name}))};
    assert.equal(path,`orgs/TauCetiProject/actions/runner-groups/${group}`);
    return {id:group,name,visibility,restricted_to_workflows:restricted,selected_workflows:workflows};
  };
  return github;
}

test('runner classes are build classes together or exactly one special class',()=>{
  for (const kinds of [['pr'],['staging'],['pr','staging'],['queue-control'],['pr-status']]) assert.ok(runnerKinds(kinds));
  for (const kinds of [['pr','pr-status'],['queue-control','pr-status'],['staging','queue-control'],['control'],['roadmap'],[],['pr','pr'],null])
    assert.ok(!runnerKinds(kinds));
  assert.ok(hostKinds(['pr','staging','queue-control','pr-status']));
  for (const kinds of [['control'],['pr','pr'],[],'pr',['__proto__']]) assert.ok(!hostKinds(kinds));
  assert.deepEqual([['pr'],['staging','pr'],['queue-control'],['pr-status']].map(groupFor),[3,3,6,7]);
  assert.deepEqual(labels('a'.repeat(32),'fleet-x-'+'b'.repeat(16),['queue-control'],'Tauchonki2'),
    ['self-hosted','linux','x64','fleet-'+'a'.repeat(32),'fleet-instance-'+'b'.repeat(16),'fleet-host-tauchonki2','tauceti-fleet-queue-control']);
});
test('queue-control requires its main workflows, admits only the shared harness at main and only TauCeti',async()=>{
  const calls=[];
  assert.deepEqual(await client(6,{workflows:queue},calls).policy({group:6,fresh:true}),{runner_group_id:6});
  assert.deepEqual(calls,['orgs/TauCetiProject/actions/runner-groups/6','orgs/TauCetiProject/actions/runner-groups/6/repositories?per_page=100']);
  assert.deepEqual(await client(6,{workflows:[...queue,...harness]}).policy({group:6,fresh:true}),{runner_group_id:6});
  for (const settings of [{workflows:queue.slice(1)},{workflows:[...queue,workflow('TauCeti','pr-status.yml')]},
    {workflows:[...queue,workflow('TauCetiTools','pick-runner.yml')]},
    {workflows:[...queue,harness[0].replace('refs/heads/main','refs/heads/other')]},
    {workflows:[...queue,harness[0].replace('TauCetiProject/','someone/')]},
    {workflows:queue,visibility:'all'},{workflows:queue,restricted:false},{workflows:queue,name:'TauCetiFleet'},
    {workflows:queue,repositories:['TauCetiProject/TauCeti','TauCetiProject/TauCetiRoadmap']},
    {workflows:queue,repositories:['TauCetiProject/TauCetiRoadmap']},{workflows:queue,repositories:[]}])
    await assert.rejects(client(6,settings).policy({group:6,fresh:true}),PolicyError);
});
test('pr-status requires exactly its two main workflows on TauCeti',async()=>{
  assert.deepEqual(await client(7,{workflows:status}).policy({group:7,fresh:true}),{runner_group_id:7});
  for (const settings of [{workflows:status.slice(1)},{workflows:[...status,queue[1]]},{workflows:[...status,harness[0]]},
    {workflows:status.map(w=>w.replace('refs/heads/main','refs/pull/1/merge'))},
    {workflows:status,visibility:'all'},{workflows:status,restricted:false},
    {workflows:status,repositories:['TauCetiProject/TauCeti','someone/Other']}])
    await assert.rejects(client(7,settings).policy({group:7,fresh:true}),PolicyError);
});
test('docs requires exactly the API docs caller and its harness job at main, on TauCeti only',async()=>{
  assert.equal(groupFor(['docs']),DOCS_GROUP);
  assert.ok(runnerKinds(['docs']));
  assert.ok(hostKinds(['pr','staging','docs']));
  for (const kinds of [['pr','docs'],['docs','pr-status'],['staging','docs']]) assert.ok(!runnerKinds(kinds));
  assert.deepEqual(labels('a'.repeat(32),'fleet-x-'+'b'.repeat(16),['docs'],'Tauchonki2').at(-1),'tauceti-fleet-docs');
  assert.deepEqual(await client(DOCS_GROUP,{workflows:docs}).policy({group:DOCS_GROUP,fresh:true}),{runner_group_id:DOCS_GROUP});
  assert.deepEqual(await client(DOCS_GROUP,{workflows:[...docs].reverse()}).policy({group:DOCS_GROUP,fresh:true}),{runner_group_id:DOCS_GROUP});
  for (const settings of [{workflows:docs.slice(1)},{workflows:docs.slice(0,1)},{workflows:[...docs,queue[1]]},
    {workflows:[...docs,workflow('TauCetiTools','pr-build.yml')]},
    {workflows:docs.map(w=>w.replace('refs/heads/main','refs/heads/other'))},
    {workflows:docs,visibility:'all'},{workflows:docs,restricted:false},{workflows:docs,name:'TauCetiFleet'},
    {workflows:docs,repositories:['TauCetiProject/TauCeti','TauCetiProject/TauCetiTools']},
    {workflows:docs,repositories:['TauCetiProject/TauCetiTools']},{workflows:docs,repositories:[]}])
    await assert.rejects(client(DOCS_GROUP,settings).policy({group:DOCS_GROUP,fresh:true}),PolicyError);
});
test('infra admits any workflow of exactly the four infrastructure repositories',async()=>{
  const infra=INFRA_REPOSITORIES;
  assert.equal(infra.length,4);
  assert.deepEqual(groupFor(['infra']),INFRA_GROUP);
  assert.ok(runnerKinds(['infra']));
  for (const kinds of [['pr','infra'],['infra','pr-status']]) assert.ok(!runnerKinds(kinds));
  for (const settings of [{workflows:[],restricted:false,repositories:infra},{workflows:[],restricted:false,repositories:[...infra].reverse()},
    {workflows:[workflow('TauCetiWorker','ci.yml')],restricted:true,repositories:infra}])
    assert.deepEqual(await client(INFRA_GROUP,settings).policy({group:INFRA_GROUP,fresh:true}),{runner_group_id:INFRA_GROUP});
  for (const settings of [{workflows:[],restricted:false,repositories:[...infra,'TauCetiProject/TauCeti']},
    {workflows:[],restricted:false,repositories:infra.slice(1)},{workflows:[],restricted:false,repositories:['TauCetiProject/TauCeti']},
    {workflows:[],restricted:false,repositories:[...infra.slice(1),'TauCetiProject/TauCetiRoadmap']},
    {workflows:[],restricted:false,repositories:infra,visibility:'all'},{workflows:[],restricted:false,repositories:infra,name:'TauCetiFleet'}])
    await assert.rejects(client(INFRA_GROUP,settings).policy({group:INFRA_GROUP,fresh:true}),PolicyError);
});
test('policy is cached per group, and unknown groups are refused without a request',async()=>{
  const calls=[];
  const github=client(6,{workflows:queue},calls);
  await github.policy({group:6});await github.policy({group:6});
  assert.equal(calls.length,2);
  await github.policy({group:6,fresh:true});
  assert.equal(calls.length,4);
  for (const group of [1,4,5,'6']) await assert.rejects(github.policy({group,fresh:true}),PolicyError);
  assert.equal(calls.length,4);
});
test('inventory lists the broker groups and tags each runner with its group',async()=>{
  const github=new GitHub({});const paths=[];
  github.request=async path=>{
    paths.push(path);
    const group=Number(path.match(/runner-groups\/(\d+)\/runners/)[1]);
    return {total_count:1,runners:[{id:group*10,name:'runner-'+group}]};
  };
  assert.deepEqual(await github.runners([3,6,7]),[3,6,7].map(g=>({id:g*10,name:'runner-'+g,runner_group_id:g})));
  assert.deepEqual(paths,[3,6,7].map(g=>`orgs/TauCetiProject/actions/runner-groups/${g}/runners?per_page=100&page=1`));
  // Build-only callers read group 3 alone, as before.
  paths.length=0;
  assert.deepEqual(await github.runners(),[{id:30,name:'runner-3',runner_group_id:3}]);
  assert.equal(paths.length,1);
  await assert.rejects(github.runners([4]),/Incomplete runner inventory/);
  github.request=async()=>({total_count:1,runners:[{id:1,name:'same'}]});
  await assert.rejects(github.runners([3,6]),/Incomplete runner inventory/);
});
test('hosts inventory only the groups of their classes and registered slots',()=>{
  assert.deepEqual(hostGroups({kinds:['pr','staging'],slots:{}}),[3]);
  assert.deepEqual(hostGroups({slots:{}}),[3]);
  assert.deepEqual(hostGroups({kinds:['pr','staging','queue-control','pr-status'],slots:{}}),[3,6,7]);
  assert.deepEqual(hostGroups({kinds:['pr'],slots:{4:{runner_id:1,kinds:['pr-status']}}}),[3,7]);
  assert.deepEqual(hostGroups({kinds:['pr'],slots:{4:{pending:{name:'x',at:1,group:6}}}}),[3,6]);
  // Retired slots keep their last classes but no longer widen the inventory.
  assert.deepEqual(hostGroups({kinds:['pr'],slots:{4:{kinds:['queue-control'],runner_name:'old'}}}),[3]);
});
test('recovery rechecks the group recorded with the reservation, or group 3 for older ones',async()=>{
  for (const [pending,expected] of [[{group:6},6],[{group:7},7],[{},3]]) {
    const name='fleet-'+'1'.repeat(12)+'-'+'a'.repeat(16);
    const host={machine_id:'1'.repeat(32),slots:{0:{pending:{name,at:Date.now()-RECOVERY_DELAY-1000,...pending}}}};
    const records=new Map([['host:'+host.machine_id,host]]);
    const storage={get:async k=>records.get(k),put:async(k,v)=>records.set(k,v),list:async()=>records,
      getAlarm:async()=>null,setAlarm:async()=>{},deleteAlarm:async()=>{},transaction:async f=>f(storage)};
    const groups=[],deleted=[];
    // Same-named offline runners in other groups are not this reservation's registration.
    const inventory=[3,6,7].map(g=>({id:g,name,status:'offline',busy:false,runner_group_id:g}));
    const github={policy:async({group,fresh})=>{assert.equal(fresh,true);groups.push(group);},runners:async()=>inventory,
      request:async(path,{method}={})=>{const id=Number(path.split('/').at(-1));
        if (method==='DELETE') { deleted.push(id); return null; } return inventory.find(r=>r.id===id);}};
    assert.deepEqual(await new RegistrationRecovery(storage,github).reconcile(host,0),{reconciled:true,removed:[expected]});
    assert.deepEqual(groups,[expected]);assert.deepEqual(deleted,[expected]);
  }
});

test('every PR runner offers the priority class; a priority-only runner offers nothing else',()=>{
  const id='a'.repeat(32),name='fleet-x-'+'b'.repeat(16),base=['self-hosted','linux','x64','fleet-'+id,'fleet-instance-'+'b'.repeat(16),'fleet-host-c5'];
  assert.equal(PRIORITY_CLASS,'tauceti-fleet-pr-priority');
  assert.deepEqual(labels(id,name,['pr'],'C5'),[...base,'tauceti-fleet-pr',PRIORITY_CLASS]);
  assert.deepEqual(labels(id,name,['pr','staging'],'C5'),
    [...base.slice(0,5),'tauceti-fleet','fleet-host-c5','tauceti-fleet-pr','tauceti-fleet-staging',PRIORITY_CLASS]);
  // Staging and special runners never take priority PR builds.
  assert.deepEqual(labels(id,name,['staging'],'C5'),[...base.slice(0,5),'tauceti-fleet','fleet-host-c5','tauceti-fleet-staging']);
  assert.ok(!labels(id,name,['queue-control'],'C5').includes(PRIORITY_CLASS));
  for (const kinds of [['pr'],['pr','staging']])
    assert.deepEqual(labels(id,name,kinds,'C5',true),[...base,PRIORITY_CLASS]);
});
