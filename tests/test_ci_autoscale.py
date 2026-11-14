import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tauceti_fleet import ci_autoscale as auto, host, hostos, store, vm

hostos.MACOS = False

GIB = 1024**3


def snapshot(now=1000, *, available=14, limit=3, idle=False):
    vms = [dict(slot=i, instance=str(i) * 16, runner_id=100+i, run_id=1000+i,
                busy=True, started_at=100+i, cpu_usec=100_000_000, memory_bytes=17*GIB)
           for i in range(limit)]
    if idle:
        vms[-1].update(busy=False, run_id=None)
    return dict(machine_id='machine', enabled=True, at=now, status_at=now, timing_at=now,
                maximum=limit, reserved=1, cpus=96, last_admission=now-200,
                pressure=dict(at=now, memory_total=112*GIB, memory_available=available*GIB, cpu_percent=.5),
                ci=dict(waiting_now=10, running_now=0), vms=vms)


def advance(value, seconds=30, cpu=.1):
    value = copy.deepcopy(value)
    for key in ('at', 'status_at', 'timing_at'):
        value[key] += seconds
    value['pressure']['at'] += seconds
    for guest in value['vms']:
        guest['cpu_usec'] += int(cpu * seconds * 1e6)
    return value


class CapacityPolicy(unittest.TestCase):
    def test_native_peak_does_not_inflate_vm_growth_forecast(self):
        c = auto.Controller(growth_delay=1)
        s = snapshot(available=50)
        s['vms'][2].update(runtime='native-disk', memory_bytes=65*GIB, memory_working_bytes=20*GIB)
        c.decide(s,1000)
        self.assertEqual(c.decide(advance(s),1030)['kind'],'limit')

    def test_native_growth_uses_working_memory_in_the_ordinary_forecast(self):
        for current, available, expected in ((0,50,'hold'), (8,50,'hold'), (8,80,'limit'), (20,50,'limit')):
            with self.subTest(current=current, available=available):
                c = auto.Controller(growth_delay=1)
                s = snapshot(available=available)
                s['vms'][2].update(runtime='native-disk', memory_bytes=40*GIB, memory_working_bytes=current*GIB)
                c.decide(s,1000)
                self.assertEqual(c.decide(advance(s),1030)['kind'],expected)

    def test_native_cleanup_does_not_hold_a_workspace_growth_forecast(self):
        c=auto.Controller(growth_delay=1)
        s=snapshot(available=50)
        s['vms'][2].update(runtime='native-disk', phase='cleanup', memory_bytes=None, memory_working_bytes=None)
        c.decide(s,1000)
        self.assertEqual(c.decide(advance(s),1030)['kind'],'limit')

    def test_unknown_native_working_memory_does_not_approve_growth(self):
        c = auto.Controller(growth_delay=1)
        s = snapshot(available=80)
        s['vms'][2].update(runtime='native-disk', memory_bytes=30*GIB, memory_working_bytes=None)
        c.decide(s,1000)
        self.assertEqual(c.decide(advance(s),1030)['kind'], 'hold')

    def test_idle_general_guest_is_trimmed_without_cancelling_work(self):
        c = auto.Controller()
        decision = c.decide(snapshot(limit=4, idle=True), 1000)
        self.assertEqual((decision['kind'], decision['target']), ('limit', 3))

    def test_idle_reserved_staging_guest_is_preserved(self):
        s = snapshot()
        s['vms'][0].update(busy=False)
        s['ci']['running_now'] = 1
        self.assertEqual(auto.Controller().decide(s,1000)['kind'], 'hold')

    def test_full_stall_needs_multiple_samples_and_excludes_reserved_slot(self):
        c = auto.Controller()
        s = snapshot()
        self.assertEqual(c.decide(s,1000)['kind'], 'hold')
        s = advance(s)
        decision = c.decide(s,1030)
        self.assertEqual(decision['kind'], 'recover')
        self.assertEqual(decision['target'], 2)
        self.assertEqual([v['slot'] for v in decision['candidates']], [2,1])

    def test_actual_compiler_or_native_cpu_work_prevents_false_stall_recovery(self):
        for kind in ('lean', 'native', 'guest'):
            with self.subTest(kind=kind):
                c = auto.Controller()
                s = snapshot()
                c.decide(s,1000)
                s = advance(s, cpu=1 if kind == 'guest' else .1)
                if kind == 'lean': s['ci']['running_now'] = 1
                if kind == 'native': s['pressure']['cpu_percent'] = 20
                self.assertNotEqual(c.decide(s,1030)['kind'], 'recover')

    def test_native_ci_cpu_activity_is_reported_without_claiming_lean_admissions(self):
        s=snapshot();s['pressure']['cpu_percent']=77
        result=auto.Controller().decide(s,1000)
        self.assertEqual(result['kind'],'hold')
        self.assertEqual(result['reason'],'Lean waiting; other CI work is using CPU')

    def test_stale_observation_and_disabled_ci_never_authorize_mutations(self):
        c = auto.Controller()
        for s in (snapshot(now=900),dict(snapshot(),enabled=False)):
            self.assertEqual(c.decide(s,1000)['kind'], 'hold')

    def test_growth_requires_sustained_headroom_and_honors_the_ceiling(self):
        c = auto.Controller(growth_delay=180)
        s = snapshot(available=70)
        self.assertEqual(c.decide(s,1000)['kind'],'hold')
        s = advance(s,180)
        decision = c.decide(s,1180)
        self.assertEqual((decision['kind'],decision['target']),('limit',4))
        s = snapshot(now=1200,available=90,limit=4)
        c.decide(s,1200)
        self.assertEqual(c.decide(advance(s,180),1380)['kind'],'hold')

    def test_idle_staging_reservation_and_warm_spare_do_not_block_pr_growth(self):
        for idle_spare in (False,True):
            c=auto.Controller(growth_delay=30)
            s=snapshot(available=80)
            s['vms'][0].update(busy=False)
            if idle_spare:s['vms'][2].update(busy=False)
            c.decide(s,1000)
            result=c.decide(advance(s),1030)
            self.assertEqual((result['kind'],result['target']),('limit',4))

    def test_reserved_only_pool_can_regain_a_general_slot(self):
        c=auto.Controller(growth_delay=30)
        s=snapshot(available=80,limit=1)
        s['vms'][0].update(busy=False)
        c.decide(s,1000)
        result=c.decide(advance(s),1030)
        self.assertEqual((result['kind'],result['target']),('limit',2))

    def test_low_ram_never_trims_the_last_general_capacity(self):
        s=snapshot(limit=2,idle=True)
        self.assertEqual(auto.Controller().decide(s,1000)['kind'],'hold')

    def test_unrealized_preparation_memory_and_unknown_measurements_block_growth(self):
        for missing in (False,True):
            c = auto.Controller(growth_delay=1)
            s = snapshot(available=60)
            for v in s['vms']: v['memory_bytes'] = None if missing else 4*GIB
            c.decide(s,1000)
            self.assertEqual(c.decide(advance(s),1030)['kind'],'hold')


class RecoveryTransactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.operator = auto.Operator('host','machine',root=Path(self.temp.name))
        self.pending = dict(phase='cancel',run_id=123,attempt=1,slot=2,instance='2'*16,runner_id=102)
        self.operator.controller.value['pending'] = self.pending
        self.operator.host = Mock(side_effect=lambda mode,**kw: snapshot() if mode=='snapshot' else {})

    def test_completed_rerun_after_a_crash_is_not_started_twice(self):
        for phase in ('cancel','rerun','verify'):
            with self.subTest(phase=phase):
                self.pending['phase'] = phase
                self.operator.controller.value['pending'] = self.pending
                self.operator.api = Mock(return_value=dict(run_attempt=2,status='in_progress'))
                self.operator.transaction()
                self.assertNotIn('pending',self.operator.controller.value)
                self.assertTrue(all(c.kwargs.get('method','GET') == 'GET' for c in self.operator.api.call_args_list))

    def test_write_quota_failure_still_releases_memory_and_keeps_durable_retry(self):
        def api(path, *, method='GET'):
            if method == 'POST': raise store.FleetError('quota')
            return dict(run_attempt=1,status='in_progress')
        self.operator.api = Mock(side_effect=api)
        with patch.object(auto.time,'time',return_value=1000):
            self.operator.tick()
        self.assertTrue(self.pending['retired'])
        self.assertEqual(self.pending['phase'],'cancel')
        self.assertGreater(self.pending['retry_at'],1000)
        self.assertIn('pending',store.read(self.operator.path))
        calls=self.operator.api.call_count
        with patch.object(auto.time,'time',return_value=1001): self.operator.tick()
        self.assertEqual(self.operator.api.call_count,calls)
        self.operator.host.assert_any_call('retire',slot=2,instance='2'*16,run_id=123)

    def test_guest_replacement_race_does_not_skip_durable_full_run_retry(self):
        self.pending['phase']='retire'
        self.operator.host=Mock(return_value=dict(retired=False,reason='guest changed'))
        self.operator.api=Mock(side_effect=[dict(run_attempt=1,status='completed',conclusion='cancelled'),{}])
        self.operator.transaction()
        self.operator.api.assert_any_call('actions/runs/123/rerun',method='POST')
        self.assertEqual(self.pending['phase'],'verify')

    def test_api_errors_during_candidate_lookup_do_not_repeat_every_tick(self):
        op=auto.Operator('host','machine',root=Path(self.temp.name)/'lookup')
        s=snapshot();op.controller.decide(s,1000);op.host=Mock(return_value=advance(s))
        op.api=Mock(side_effect=store.FleetError('quota'))
        with patch.object(auto.time,'time',return_value=1030):
            with self.assertRaises(store.FleetError):op.tick()
        self.assertEqual(store.read(op.path)['probe_retry_at'],1090)
        with patch.object(auto.time,'time',return_value=1045):op.tick()
        op.api.assert_called_once()

    def test_retired_runner_failure_still_gets_its_promised_full_rerun(self):
        self.pending.update(phase='cancel',retired=True)
        self.operator.api=Mock(side_effect=[dict(run_attempt=1,status='completed',conclusion='failure'),{}])
        self.operator.transaction()
        self.operator.api.assert_any_call('actions/runs/123/rerun',method='POST')
        self.assertEqual(self.pending['phase'],'verify')

    def test_cleanup_clears_stall_confirmation_before_another_cancellation(self):
        self.pending.update(phase='cleanup')
        self.operator.controller.value['stall_since']=500
        self.operator.api=Mock(return_value=dict(run_attempt=2,status='in_progress'))
        self.operator.transaction()
        self.assertNotIn('stall_since',self.operator.controller.value)

    def test_full_workflow_is_rerun_then_verified_before_cleanup(self):
        self.pending['phase']='rerun'
        self.operator.api = Mock(side_effect=[dict(run_attempt=1,status='completed',conclusion='cancelled'),{}])
        self.operator.transaction()
        self.operator.api.assert_any_call('actions/runs/123/rerun',method='POST')
        self.assertEqual(self.pending['phase'],'verify')
        self.operator.api=Mock(return_value=dict(run_attempt=2,status='queued'))
        self.operator.transaction()
        self.assertNotIn('pending',self.operator.controller.value)

    def test_verified_pr_recovery_saves_identity_before_lowering_the_cap(self):
        op=auto.Operator('host','machine',root=Path(self.temp.name)/'verified')
        s=snapshot();op.controller.decide(s,1000)
        def action(mode,**kwargs):
            if mode=='snapshot':return advance(s)
            if mode=='limit':
                pending=store.read(op.path)['pending']
                self.assertEqual(pending['run_id'],1002)
                self.assertEqual(pending['instance'],'2'*16)
            return {}
        op.host=Mock(side_effect=action)
        op.api=Mock(side_effect=[dict(event='pull_request_target',status='in_progress',id=1002,run_attempt=1),
                                dict(jobs=[dict(runner_id=102,status='in_progress')])])
        with patch.object(auto.time,'time',return_value=1030):op.tick()
        self.assertEqual(op.controller.value['pending']['phase'],'cancel')
        op.host.assert_any_call('limit',maximum=2)
        self.assertTrue(all(c.kwargs.get('method','GET')=='GET' for c in op.api.call_args_list))

    def test_missing_named_account_never_falls_back_to_ambient_credentials(self):
        op=auto.Operator('host','machine',root=Path(self.temp.name)/'auth',github_account='kim-em')
        for method in ('GET','POST'):
            with patch.object(auto.subprocess,'run',return_value=Mock(returncode=1,stdout='',stderr='missing')) as request:
                with self.assertRaises(store.FleetError):op.api('actions/runs/1',method=method)
                request.assert_called_once()
                self.assertEqual(request.call_args.args[0][:3],['gh','auth','token'])

    def test_staging_and_stale_runner_ownership_are_never_cancelled(self):
        for staging in (True,False):
            with self.subTest(staging=staging):
                op=auto.Operator('host','machine',root=Path(self.temp.name)/str(staging))
                s=snapshot()
                op.controller.decide(s,1000)
                op.host=Mock(return_value=advance(s))
                op.api=Mock(side_effect=lambda path,**kw: dict(event='repository_dispatch' if staging else 'pull_request_target',status='in_progress',id=1002,run_attempt=1) if '/jobs?' not in path else dict(jobs=[dict(runner_id=102 if staging else 999,status='in_progress')]))
                with patch.object(auto.time,'time',return_value=1030): op.tick()
                self.assertNotIn('pending',op.controller.value)
                self.assertTrue(op.api.called)
                self.assertTrue(all(c.kwargs.get('method','GET') == 'GET' for c in op.api.call_args_list))


class HostActions(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        env=patch.dict(os.environ,{"TAUCETI_FLEET_"+k:str(self.root/k) for k in ('STATE','CONFIG','DATA')});env.start();self.addCleanup(env.stop)
        store.update_host(ci_max_jobs=3,ci_kinds=['pr','staging'],ci_staging_slots=1,ci_enabled=True,workers_enabled=True,max_workers=40)

    def test_live_limit_changes_preserve_worker_and_ci_admissions(self):
        auto.host_action(dict(mode='limit',maximum=2))
        config=store.host_config()
        self.assertEqual(config['ci_max_jobs'],2)
        self.assertTrue(config['ci_enabled']);self.assertTrue(config['workers_enabled']);self.assertEqual(config['max_workers'],40)
        with self.assertRaises(store.FleetError):auto.host_action(dict(mode='limit',maximum=0))

    def test_reserved_and_replaced_instances_cannot_be_retired(self):
        with patch.object(vm,'retire') as retire:
            with self.assertRaises(store.FleetError):auto.host_action(dict(mode='retire',slot=0,instance='a',run_id=1))
            result=auto.host_action(dict(mode='retire',slot=1,instance='a',run_id=1))
            self.assertFalse(result['retired']);retire.assert_not_called()

    def test_runner_cleanup_requires_persisted_host_ownership(self):
        from tauceti_fleet import github
        with patch.object(github,'retire_runner') as retire:
            with self.assertRaises(store.FleetError):auto.host_action(dict(mode='cleanup',runner_id=99))
            retire.assert_not_called()
            store.atomic(store.state_dir()/'ci-slots/9/ci-last-retired.json',dict(runner_id=99))
            self.assertTrue(auto.host_action(dict(mode='cleanup',runner_id=99))['removed'])
            retire.assert_called_once()

    def test_recovery_ownership_survives_slot_reuse_and_collected_result(self):
        from tauceti_fleet import github
        value=dict(slot=1,instance='a'*16,runner_id=99,run_id=77,unit='vm.service')
        store.atomic(vm.slot_file(dict(ci_slot=1),'vm.json'),value)
        with patch.object(vm,'_retire') as retire:
            self.assertTrue(auto.host_action(dict(mode='retire',slot=1,instance=value['instance'],run_id=77))['retired'])
            retire.assert_called_once()
        store.atomic(store.state_dir()/'pending-ci.json',[])
        store.atomic(store.state_dir()/'ci-slots/1/ci-last-retired.json',dict(runner_id=100))
        with patch.object(github,'retire_runner') as remove:
            self.assertTrue(auto.host_action(dict(mode='cleanup',runner_id=99))['removed'])
            remove.assert_called_once()

    def test_snapshot_reads_actual_ci_cgroups_when_workers_are_paused(self):
        store.update_host(max_workers=0)
        store.atomic(store.state_dir()/'status.json',dict(worker_memory=None,ci_vms=[
            dict(slot=0,unit='first.service'),dict(slot=1,unit='second.service')]))
        root=self.root/'cgroups'
        for name,memory,cpu in [('first',17*GIB,12345),('second',3*GIB,67890)]:
            group=root/'custom.slice'/name;group.mkdir(parents=True)
            (group/'memory.current').write_text(str(memory))
            (group/'cpu.stat').write_text('usage_usec '+str(cpu)+'\n')
        reply=Mock(returncode=0,stdout='Id=first.service\nControlGroup=/custom.slice/first\n\nId=second.service\nControlGroup=/custom.slice/second\n')
        with patch.object(auto,'CGROUP_ROOT',root,create=True),patch.object(vm,'run',return_value=reply),patch.object(auto.remote,'call',side_effect=AssertionError('no remote calls')):
            result=auto.host_action({})
        self.assertEqual([v['memory_bytes'] for v in result['vms']],[17*GIB,3*GIB])
        self.assertEqual([v['cpu_usec'] for v in result['vms']],[12345,67890])
        self.assertEqual(store.host_config()['max_workers'],0)
        self.assertTrue(store.host_config()['ci_enabled'])

    def test_snapshot_preserves_native_raw_charge_and_reports_reclaimable_cache(self):
        store.atomic(store.state_dir()/'status.json',dict(ci_vms=[dict(unit='native.service',runtime='native-disk')]))
        root=self.root/'cgroups'
        group=root/'native'; group.mkdir(parents=True)
        (group/'memory.current').write_text(str(30*GIB))
        (group/'memory.stat').write_text(f'file {28*GIB}\nshmem 0\nfile_dirty {GIB}\nfile_writeback 0\n')
        (group/'cpu.stat').write_text('usage_usec 123\n')
        reply=Mock(returncode=0,stdout='Id=native.service\nControlGroup=/native\n')
        with patch.object(auto,'CGROUP_ROOT',root),patch.object(vm,'run',return_value=reply):
            value=auto.host_action({})['vms'][0]
        self.assertEqual(value['memory_bytes'],30*GIB)
        self.assertEqual(value['memory_clean_file_bytes'],27*GIB)
        self.assertEqual(value['memory_working_bytes'],3*GIB)

    def test_missing_ci_unit_never_reads_root_or_loses_a_healthy_sibling(self):
        store.atomic(store.state_dir()/'status.json',dict(ci_vms=[dict(unit='good.service'),dict(unit='gone.service')]))
        root=self.root/'cgroups';root.mkdir()
        # Root counters must never be attributed to a guest with no cgroup.
        (root/'memory.current').write_text(str(99*GIB));(root/'cpu.stat').write_text('usage_usec 999\n')
        good=root/'actual-ci'/ 'good';good.mkdir(parents=True)
        (good/'memory.current').write_text(str(5*GIB));(good/'cpu.stat').write_text('usage_usec 123\n')
        for missing in ['', '/', '/actual-ci/missing', '/actual-ci/../good']:
            with self.subTest(missing=missing):
                reply=Mock(returncode=1,stdout='Id=good.service\nControlGroup=/actual-ci/good\n\nId=gone.service\nControlGroup='+missing+'\n')
                with patch.object(auto,'CGROUP_ROOT',root,create=True),patch.object(vm,'run',return_value=reply):
                    result=auto.host_action({})
                self.assertEqual(result['vms'][0]['memory_bytes'],5*GIB)
                self.assertEqual(result['vms'][0]['cpu_usec'],123)
                self.assertIsNone(result['vms'][1]['memory_bytes'])
                self.assertIsNone(result['vms'][1]['cpu_usec'])

    def test_snapshot_keeps_ci_measurements_unknown_when_systemd_is_unavailable(self):
        store.atomic(store.state_dir()/'status.json',dict(worker_memory=None,ci_vms=[dict(unit='ci.service')]))
        with patch.object(vm,'run',side_effect=store.FleetError('systemctl unavailable')):
            result=auto.host_action({})
        self.assertIsNone(result['vms'][0]['memory_bytes'])
        self.assertIsNone(result['vms'][0]['cpu_usec'])

    def test_snapshot_is_cache_only_and_handles_no_worker_measurement(self):
        with patch.object(auto.remote,'call',side_effect=AssertionError('no remote calls')):
            result=auto.host_action({})
        self.assertEqual(result['maximum'],3)
        self.assertEqual(result['vms'],[])
