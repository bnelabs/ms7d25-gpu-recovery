import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch,Mock

ROOT=pathlib.Path(__file__).resolve().parents[1]
def load(name):
 spec=importlib.util.spec_from_file_location(name,ROOT/(name+'.py'))
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
r=load('recover');w=load('watch')
r.ACTIVE=pathlib.Path('/tmp')/('gpu-recovery-test-marker-'+str(__import__('os').getpid()))

class RecoveryTests(unittest.TestCase):
 def test_transfer_only_probe_does_not_resume_as_verified_cuda(self):
  with patch.object(r,'reg',return_value='10de'),patch.object(r,'unload'),patch.object(r,'pulse'),patch.object(r,'restore_pci'),patch.object(r,'modules',return_value=set()),patch.object(r,'log'),patch.object(r.time,'sleep'),patch.object(r,'run',side_effect=[(0,''),(0,''),(0,'Transfer correctness check: passed')]):
   with self.assertRaises(r.InitializationFailed):r.initialize_once({})
 def test_gateway_is_stopped_before_backends_and_restarted_as_owner(self):
  calls=[]
  with tempfile.TemporaryDirectory() as directory,patch.object(r,'STATE',pathlib.Path(directory)/'services.json'),patch.object(r,'run',return_value=(0,'')):
   def ctl(*args):
    calls.append(args)
    if '--property=ActiveState' in args:
     return (0,'active')
    return (0,'loaded') if args[0]=='show' else (0,'')
   with patch.object(r,'userctl',side_effect=ctl):
    r.hold_services()
    stops=[unit for call in calls if call[0]=='stop' for unit in call[1:]]
    self.assertEqual(stops[0],'inference-gateway.service')
    for unit in ['strata-server.service','strata-coder.service','maya-server.service','hyperqwen-server.service']:
     self.assertIn(unit,stops)
    r.resume_services()
    self.assertIn(('start','inference-gateway.service','deepseek-harness.service'),calls)
    self.assertFalse(r.STATE.exists())
 def test_absent_optional_unit_does_not_block_recovery(self):
  with tempfile.TemporaryDirectory() as directory,patch.object(r,'STATE',pathlib.Path(directory)/'services.json'),patch.object(r,'run',return_value=(0,'')),patch.object(r,'userctl') as ctl:
   ctl.side_effect=lambda *args: (0,'not-found') if args[0]=='show' else (3,'') if args[0]=='is-active' else (0,'')
   r.hold_services()
   self.assertFalse(any(c.args[0]=='stop' for c in ctl.call_args_list))
 def test_clean_retry_only_after_initialization_failure(self):
  with patch.object(r,'initialize_once',side_effect=[r.InitializationFailed('probe'),None]) as init,patch.object(r.time,'sleep'),patch.object(r,'log'):
   r.initialize_with_retry({});self.assertEqual(init.call_count,2)
 def test_second_initialization_failure_stops(self):
  with patch.object(r,'initialize_once',side_effect=r.InitializationFailed('probe')) as init,patch.object(r.time,'sleep'),patch.object(r,'log'):
   with self.assertRaises(r.InitializationFailed):r.initialize_with_retry({})
   self.assertEqual(init.call_count,2)
 def test_timeout_or_teardown_failure_never_retried(self):
  for error in (r.CommandTimeout('blocked'),RuntimeError('teardown')):
   with patch.object(r,'initialize_once',side_effect=error) as init:
    with self.assertRaises(type(error)):r.initialize_with_retry({})
    self.assertEqual(init.call_count,1)
 def test_saved_services_survive_failed_attempt(self):
  with tempfile.TemporaryDirectory() as directory,patch.object(r,'STATE',pathlib.Path(directory)/'services.json'),patch.object(r,'userctl') as ctl,patch.object(r,'run',return_value=(0,'')):
   ctl.side_effect=lambda *args:(0,'active') if '--property=ActiveState' in args and args[1]=='llama-server.service' else (0,'loaded')
   r.hold_services()
   ctl.side_effect=lambda *args:(0,'inactive') if '--property=ActiveState' in args else (0,'loaded')
   r.hold_services();self.assertIn('llama-server.service',r.STATE.read_text())
   r.resume_services();self.assertFalse(r.STATE.exists())
   self.assertIn(unittest.mock.call('start','llama-server.service'),ctl.call_args_list)
 def test_module_load_uses_all_flag(self):
  with patch.object(r,'reg',return_value='10de'),patch.object(r,'unload'),patch.object(r,'pulse'),patch.object(r,'restore_pci'),patch.object(r,'modules',return_value=set()),patch.object(r,'log'),patch.object(r.time,'sleep'),patch.object(r,'run',side_effect=[(0,''),(0,''),(0,'Transfer correctness check: passed\nKernel correctness check: passed')]) as run:
   r.initialize_once({});self.assertEqual(run.call_args_list[0].args[0],['/usr/sbin/modprobe','--all','nvidia','nvidia_uvm'])

class WatchTests(unittest.TestCase):
 def test_real_kernel_xid79_message_matches_only_the_expected_gpu_and_error(self):
  self.assertTrue(w.is_xid79('NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus.'))
  self.assertTrue(w.is_xid79('NVRM: Xid (PCI:0000:01:00.0): 79, GPU has fallen off the bus.'))
  for message in ['Xid (PCI:0000:02:00): 79, lost','Xid (PCI:0000:01:00): 119, timeout','Xid (PCI:0000:01:00.1): 79, lost']:
   self.assertFalse(w.is_xid79(message))
 def test_healthy_startup_does_not_reset(self):
  with patch.object(w,'healthy',return_value=True),patch.object(w.subprocess,'run') as run:
   w.Controller().check();run.assert_not_called()
 def test_health_failure_recovers_after_hold(self):
  with patch.object(w,'healthy',return_value=False),patch.object(w.subprocess,'run',return_value=Mock(returncode=0)) as run:
   controller=w.Controller();controller.check();self.assertFalse(controller.failed)
   self.assertEqual([c.args[0] for c in run.call_args_list],[[w.RECOVERY,'--hold-services'],[w.RECOVERY]])
 def test_failed_recovery_holds_on_later_checks_without_reset_loop(self):
  with patch.object(w,'healthy',return_value=False),patch.object(w.subprocess,'run',side_effect=[Mock(returncode=0),Mock(returncode=1),Mock(returncode=0)]) as run:
   controller=w.Controller();controller.check();controller.check();self.assertTrue(controller.failed)
   self.assertEqual(sum(c.args[0]==[w.RECOVERY] for c in run.call_args_list),1)
 def test_health_timeout_holds_without_reset(self):
  with patch.object(w,'healthy',side_effect=subprocess.TimeoutExpired('nvidia-smi',10)),patch.object(w.subprocess,'run',return_value=Mock(returncode=0)) as run:
   controller=w.Controller();controller.check();self.assertTrue(controller.failed)
   self.assertEqual(run.call_args.args[0],[w.RECOVERY,'--hold-services'])
 def test_old_event_after_success_does_not_reset_healthy_gpu(self):
  with patch.object(w,'healthy',return_value=True),patch.object(w.subprocess,'run') as run:
   controller=w.Controller();controller.last_recovery_end=100;controller.check(True,90);run.assert_not_called()

class DeferredRetryTests(unittest.TestCase):
 def test_link_not_ready_retries_after_cooldown_without_new_xid(self):
  clock=[100]
  with patch.object(w,'healthy',return_value=False),patch.object(w.time,'monotonic',side_effect=lambda:clock[0]),patch.object(w.subprocess,'run',return_value=Mock(returncode=0)) as run:
   # hold -> recover(tempfail); hold while waiting; hold -> recover(success)
   run.side_effect=[Mock(returncode=n) for n in (0,75,0,0,0)]
   c=w.Controller();c.check(True);self.assertTrue(c.pending_retry);self.assertFalse(c.failed)
   self.assertEqual(c.next_attempt,160)
   clock[0]=150;c.check();self.assertEqual(len(c.attempts),1)
   clock[0]=160;c.check();self.assertEqual(len(c.attempts),2)
   self.assertFalse(c.pending_retry);self.assertFalse(c.failed)
 def test_three_link_failures_cool_down_then_retry_automatically(self):
  clock=[100]
  with patch.object(w,'healthy',return_value=False),patch.object(w.time,'monotonic',side_effect=lambda:clock[0]),patch.object(w.subprocess,'run',side_effect=[Mock(returncode=n) for n in (0,75,0,75,0,75,0,0,0)]) as run:
   c=w.Controller();c.check();clock[0]=160;c.check();self.assertEqual(c.next_attempt,400)
   clock[0]=400;c.check();clock[0]=1000;c.check()
   self.assertFalse(c.failed);self.assertTrue(c.pending_retry)
   self.assertEqual(sum(call.args[0]==[w.RECOVERY] for call in run.call_args_list),3)
   clock[0]=3700;c.check()
   self.assertFalse(c.pending_retry);self.assertFalse(c.failed)
   self.assertEqual(sum(call.args[0]==[w.RECOVERY] for call in run.call_args_list),4)
 def test_pending_retry_survives_same_boot_watcher_restart(self):
  with tempfile.TemporaryDirectory() as directory,patch.object(w,'healthy',return_value=False),patch.object(w.time,'monotonic',return_value=100),patch.object(w.subprocess,'run',side_effect=[Mock(returncode=0),Mock(returncode=75)]):
   path=pathlib.Path(directory)/'watch.json';c=w.Controller(path);c.check()
   restarted=w.Controller(path);self.assertTrue(restarted.pending_retry)
   self.assertEqual(restarted.next_attempt,160);self.assertEqual(list(restarted.attempts),[100])
 def test_pending_retry_does_not_leave_held_services_off_if_nvml_returns(self):
  clock=[100]
  with patch.object(w,'healthy',side_effect=[False,True]),patch.object(w,'services_held',return_value=True),patch.object(w.time,'monotonic',side_effect=lambda:clock[0]),patch.object(w.subprocess,'run',side_effect=[Mock(returncode=n) for n in (0,75,0,0)]) as run:
   c=w.Controller();c.check();clock[0]=160;c.check()
   self.assertFalse(c.pending_retry);self.assertFalse(c.failed)
   self.assertEqual(sum(call.args[0]==[w.RECOVERY] for call in run.call_args_list),2)


class RestoreServicesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = pathlib.Path(directory.name)
        for name, path in [('STATE', root/'services.json'), ('ACTIVE', root/'active')]:
            override = patch.object(r, name, path)
            override.start()
            self.addCleanup(override.stop)
        r.STATE.write_text('{"units":["inference-gateway.service"],"persist":true,"power_timer":true}')
        r.ACTIVE.touch()

    def test_cdi_is_regenerated_before_persistence_and_gateway_start(self):
        calls = []

        def run(command, **kwargs):
            if command[:2] == ['/usr/bin/systemctl', 'start']:
                calls.append(command[-1])
                self.assertFalse(r.ACTIVE.exists())
            return (0, 'loaded')

        def userctl(action, unit, *options):
            if action == 'show':
                return (0, 'loaded')
            self.assertEqual(action, 'start')
            calls.append(unit)
            return (0, '')

        with patch.object(r, 'run', side_effect=run), patch.object(r, 'userctl', side_effect=userctl):
            r.resume_services()
        self.assertEqual(calls, [r.CDI_SERVICE, 'nvidia-persistenced.service', r.POWER_TIMER,
                                 'inference-gateway.service'])
        self.assertFalse(r.STATE.exists())

    def test_cdi_failure_reapplies_hold_and_keeps_desired_services(self):
        with patch.object(r, 'run', return_value=(0, '')), patch.object(r, 'userctl') as userctl, \
                patch.object(r, 'manage_cdi_service', side_effect=[RuntimeError('refresh failed'), None]):
            userctl.side_effect = lambda *args: (3, '') if args[0] == 'is-active' else (0, 'loaded')
            with self.assertRaisesRegex(RuntimeError, 'CUDA passed but service restoration failed'):
                r.resume_services()
        self.assertTrue(r.ACTIVE.exists())
        self.assertIn('inference-gateway.service', r.STATE.read_text())
        self.assertFalse(any(call.args[0] == 'start' for call in userctl.call_args_list))

    def test_absent_container_toolkit_is_optional(self):
        with patch.object(r, 'run', return_value=(0, 'not-found')) as run:
            r.manage_cdi_service('start')
        self.assertEqual(run.call_count, 1)
        self.assertIn('show', run.call_args.args[0])


class ManualRecoveryTests(unittest.TestCase):
    def test_completed_manual_recovery_cancels_pending_reset(self):
        with patch.object(w, 'healthy', return_value=True), patch.object(w, 'services_held', return_value=False), \
                patch.object(w.subprocess, 'run') as run:
            controller = w.Controller()
            controller.pending_retry = True
            controller.next_attempt = 800
            controller.attempts.append(100)
            controller.check()
        run.assert_not_called()
        self.assertFalse(controller.pending_retry)
        self.assertEqual(controller.next_attempt, 0)
        self.assertEqual(list(controller.attempts), [100])


class InferenceRegistryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = pathlib.Path(directory.name)
        for name, path in [('INFERENCE_CONFIG', root/'registry.json'),
                           ('STATE', root/'services.json'), ('ACTIVE', root/'active')]:
            override = patch.object(r, name, path)
            override.start()
            self.addCleanup(override.stop)

    def test_standalone_user_and_system_services_restore_alongside_gateway(self):
        r.INFERENCE_CONFIG.write_text(json.dumps({
            'gateway_backends': ['managed.service', 'lazy.service'],
            'user_services': ['worker@model.service', 'idle.service'],
            'system_services': ['inference.service', 'missing.service'],
        }))
        r.INFERENCE_CONFIG.chmod(0o644)
        calls = []

        def controller(scope, active):
            def call(action, *units):
                if action == 'show':
                    if '--property=ActiveState' in units:
                        return 0, active.get(units[0], 'inactive')
                    return 0, 'not-found' if units[0] == 'missing.service' else 'loaded'
                if action == 'is-active':
                    return 3, 'inactive'
                calls.append((scope, action, units))
                return 0, ''
            return call

        with patch.object(r, 'userctl', side_effect=controller('user', {
                'inference-gateway.service': 'active', 'managed.service': 'active',
                'worker@model.service': 'activating'})), patch.object(r, 'systemctl',
                side_effect=controller('system', {'inference.service': 'active'})), \
                patch.object(r, 'run', return_value=(0, 'loaded')):
            r.hold_services()
            saved = json.loads(r.STATE.read_text())
            self.assertEqual(saved['system_units'], ['inference.service'])
            self.assertEqual(saved['units'], ['inference-gateway.service', 'managed.service', 'worker@model.service'])
            self.assertEqual(calls[0], ('user', 'stop', ('inference-gateway.service',)))
            self.assertIn(('system', 'stop', ('inference.service',)), calls)
            self.assertIn(('user', 'stop', ('managed.service', 'lazy.service', 'worker@model.service', 'idle.service')), calls)
            r.resume_services()
        self.assertEqual([call for call in calls if call[1] == 'start'], [
            ('system', 'start', ('inference.service',)),
            ('user', 'start', ('inference-gateway.service', 'worker@model.service')),
        ])
        self.assertFalse(r.STATE.exists())

    def test_pre_registry_state_can_restore_a_backend_without_gateway(self):
        r.STATE.write_text('{"units":["llama-server.service"],"persist":false}')
        r.ACTIVE.touch()
        with patch.object(r, 'run', return_value=(0, 'loaded')), patch.object(r, 'userctl',
                return_value=(0, 'loaded')) as userctl, patch.object(r, 'systemctl') as systemctl:
            r.resume_services()
        userctl.assert_called_with('start', 'llama-server.service')
        systemctl.assert_not_called()

    def test_registry_rejects_flags_and_world_writable_files(self):
        r.INFERENCE_CONFIG.write_text('{"system_services":["--flag.service"]}')
        r.INFERENCE_CONFIG.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'Invalid service list'):
            r.inference_services()
        r.INFERENCE_CONFIG.write_text('{}')
        r.INFERENCE_CONFIG.chmod(0o666)
        with self.assertRaisesRegex(RuntimeError, 'writable only by its owner'):
            r.inference_services()


class LinkReadinessTests(unittest.TestCase):
 def test_only_completed_perst_readiness_failure_is_retryable(self):
  self.assertEqual(r.error_exit_status(r.LinkNotReady('not ready')),75)
  self.assertEqual(r.error_exit_status(r.InitializationFailed('firmware not ready')),75)
  for error in (r.CommandTimeout('blocked'),RuntimeError('GPIO mismatch')):
   self.assertEqual(r.error_exit_status(error),1)
 def test_pulse_wait_exhaustion_has_specific_status(self):
  with patch.object(r,'log'),patch.object(r,'run',return_value=(0,'')),patch.object(r,'reg',return_value='ffff'),patch.object(r.time,'monotonic',side_effect=[100,100,116]),patch.object(r.time,'sleep'):
   with self.assertRaises(r.LinkNotReady):r.pulse()
 def test_failed_gpio_operation_never_classified_as_link_readiness(self):
  with patch.object(r,'log'),patch.object(r,'run',side_effect=RuntimeError('GPIO mismatch')):
   with self.assertRaises(RuntimeError) as error:r.pulse()
   self.assertEqual(r.error_exit_status(error.exception),1)
 def test_lost_link_gets_one_extended_pulse_after_a_completed_reset(self):
  with patch.object(r,'pulse',side_effect=[r.LinkNotReady('link down'),None]) as pulse,patch.object(r,'reg',return_value='ffff'),patch.object(r.time,'sleep'),patch.object(r,'log'):
   r.recover_pci_link()
  self.assertEqual(pulse.call_args_list,[unittest.mock.call(),unittest.mock.call(hold_seconds=5)])
 def test_late_link_readiness_avoids_a_second_reset(self):
  with patch.object(r,'pulse',side_effect=r.LinkNotReady('temporary link loss')) as pulse,patch.object(r,'reg',return_value='10de'),patch.object(r.time,'sleep'),patch.object(r,'log'):
   r.recover_pci_link()
  pulse.assert_called_once_with()
 def test_gpio_or_config_read_failure_never_causes_an_extended_pulse(self):
  for error in (RuntimeError('GPIO mismatch'),r.CommandTimeout('setpci')):
   with patch.object(r,'pulse',side_effect=error) as pulse:
    with self.assertRaises(type(error)):r.recover_pci_link()
   pulse.assert_called_once_with()
