import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import ci_priority, github, lean_host, lean_pressure, store, vm

GIB=1024**3


class StagingPriority(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        environment=patch.dict(os.environ,TAUCETI_FLEET_STATE=str(self.root));environment.start();self.addCleanup(environment.stop)
        self.required=self.root/'required';self.required.touch()
        self.target=self.root/'pressure.json'
        for name,value in (('HOST_PRESSURE',self.target),('HOST_REQUIRED',self.required)):
            p=patch.object(lean_pressure,name,value);p.start();self.addCleanup(p.stop)
        self.observation=dict(version=1,at=100,cpu_percent=100,memory_total=112*GIB,memory_available=12*GIB)

    def capacity(self, *, staging, available=12, guest_available=30):
        value=ci_priority.frame(dict(self.observation,memory_available=available*GIB),staging=staging)
        self.target.write_text(json.dumps(dict(value,received_monotonic=100)))
        with patch.object(lean_pressure.time,'time',return_value=100), patch.object(lean_pressure.time,'monotonic',return_value=100), patch.object(lean_pressure,'memory',return_value=(112*GIB,guest_available*GIB)):
            return lean_pressure.capacity_available(),lean_pressure.host_pressure()

    def test_staging_ignores_cpu_pressure_and_uses_less_host_ram_headroom(self):
        allowed,measured=self.capacity(staging=True)
        self.assertTrue(allowed)
        self.assertEqual(measured,(100,112*GIB,12*GIB))
        self.assertFalse(self.capacity(staging=False)[0])
        self.assertLess(ci_priority.headroom(112*GIB,staging=True),12*GIB)
        self.assertGreater(ci_priority.headroom(112*GIB),12*GIB)

    def test_staging_still_rejects_actual_host_or_guest_exhaustion(self):
        self.assertFalse(self.capacity(staging=True,available=5)[0])
        self.assertFalse(self.capacity(staging=True,guest_available=2)[0])

    def test_live_legacy_wrappers_receive_equivalent_admission_policy(self):
        frame=ci_priority.frame(self.observation,staging=True)
        limit=lean_pressure.memory_limit(frame['memory_total'])
        self.assertEqual(frame['cpu_percent'],0)
        self.assertLessEqual(100*(1-frame['memory_available']/frame['memory_total']),limit)
        self.assertEqual(frame['observed_cpu_percent'],100)
        self.assertEqual(frame['observed_memory_available'],12*GIB)
        self.assertFalse(self.capacity(staging=True,available=5)[0])

    def publisher_frames(self, *, available, busy_cpu, stage_waiters=8):
        peers=[];receipts=[]
        for name,kind in (('a-pr',['pr','staging']),('z-staging',['staging'])):
            directory=self.root/'vms'/name;directory.mkdir(parents=True)
            listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.addCleanup(listener.close)
            listener.bind(str(directory/'pressure.sock'));listener.listen();listener.settimeout(1);peers.append(listener)
            receipts.append(dict(directory=str(directory),ci_kinds=kind))
        store.atomic(self.root/'status.json',dict(ci_vms=receipts))
        with patch.object(lean_pressure,'cpu_sample',side_effect=[(100,90),(200,90 if busy_cpu else 180),(300,90 if busy_cpu else 270)]), patch.object(lean_host,'worker_cpu',return_value=0), patch.object(lean_pressure,'memory',return_value=(112*GIB,available*GIB)):
            publisher=lean_host.Publisher(collect=False);self.addCleanup(publisher.close);publisher.step();clients=[]
            for i,listener in enumerate(peers):
                client,_=listener.accept();self.addCleanup(client.close);client.settimeout(1);client.recv(8192)
                client.sendall(('1:'+str(stage_waiters if i else 8)+'\n').encode());clients.append(client)
            publisher.step();frames=[json.loads(client.recv(8192)) for client in clients]
        return frames

    def test_host_permits_staging_at_full_cpu_and_lower_ram_but_not_pr(self):
        pr,stage=self.publisher_frames(available=12,busy_cpu=True)
        self.assertEqual(pr['permit'],0)
        self.assertEqual(stage['permit'],1)
        self.assertEqual(stage['starts'],1)
        self.assertEqual(stage['priority'],'staging')
        # Cached status retains measured pressure, not legacy admission fields.
        cached=store.read(self.root/'lean-host-pressure.json')
        self.assertEqual(cached['memory_available'],12*GIB)
        self.assertGreater(cached['cpu_percent'],90)
        self.assertFalse(cached['staging_priority']['cpu_sleeps'])

    def test_staging_takes_first_permits_without_double_spending_ram_budget(self):
        pr,stage=self.publisher_frames(available=50,busy_cpu=False)
        self.assertGreater(stage['starts'],0)
        self.assertEqual(stage['permit'],1)
        self.assertEqual(pr['permit'],0)
        budget=__import__('math').ceil((50*GIB-ci_priority.headroom(112*GIB,staging=True))/lean_pressure.WORST_FILE_RSS_BYTES)
        self.assertEqual(stage['starts'],budget)

    def test_reserved_staging_can_boot_with_less_headroom_and_keeps_boot_margin(self):
        with patch.object(vm,'start_memory_available',return_value=15*GIB):
            self.assertTrue(vm.start_memory_ok(dict(ci_kinds=['staging'])))
            self.assertFalse(vm.start_memory_ok(dict(ci_kinds=['pr','staging'])))
        with patch.object(vm,'start_memory_available',return_value=13*GIB):
            self.assertFalse(vm.start_memory_ok(dict(ci_kinds=['staging'])))

    def test_reserved_staging_needs_no_api_and_weight_is_applied_once(self):
        value=dict(ci_kinds=['staging'],unit='vm')
        with patch.object(github,'ci_read') as read,patch.object(ci_priority,'run') as weight:
            ci_priority.classify({},value,100)
            ci_priority.classify({},value,101)
            read.assert_not_called();weight.assert_called_once()
        self.assertEqual(value['cpu_weight'],10000)
        self.assertEqual(value['ci_job_kind'],'staging')

    def test_shared_guests_are_classified_from_trusted_run_metadata_and_failures_back_off(self):
        value=dict(ci_kinds=['pr','staging'],unit='vm',busy=True,run_id=42)
        with patch.object(github,'ci_read',side_effect=store.FleetError('quota')) as read,patch.object(ci_priority,'run'):
            ci_priority.classify({},value,100);ci_priority.classify({},value,101)
            read.assert_called_once();self.assertFalse(ci_priority.staging(value))
        with patch.object(github,'ci_read',return_value=dict(id=42,event='repository_dispatch',display_title='bors branch=main batch=1')) as read,patch.object(ci_priority,'run'):
            ci_priority.classify({},value,221);ci_priority.classify({},value,222)
            read.assert_called_once();self.assertTrue(ci_priority.staging(value))
            self.assertNotIn('ci_priority_error',value)

    def test_pr_cannot_gain_priority_from_title_alone(self):
        value=dict(ci_kinds=['pr','staging'],unit='vm',busy=True,run_id=42)
        with patch.object(github,'ci_read',return_value=dict(id=42,event='pull_request_target',display_title='bors branch=main batch=1')),patch.object(ci_priority,'run'):
            ci_priority.classify({},value,100)
        self.assertFalse(ci_priority.staging(value));self.assertEqual(value['cpu_weight'],100)
