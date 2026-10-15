import importlib.util
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

class RecoveryTests(unittest.TestCase):
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
   ctl.side_effect=lambda *args:(0,'') if args[0]!='is-active' or args[-1]=='llama-server.service' else (3,'')
   r.hold_services()
   ctl.side_effect=lambda *args:(3,'') if args[0]=='is-active' else (0,'')
   r.hold_services();self.assertIn('llama-server.service',r.STATE.read_text())
   r.resume_services();self.assertFalse(r.STATE.exists())
   self.assertIn(unittest.mock.call('start','llama-server.service'),ctl.call_args_list)
 def test_module_load_uses_all_flag(self):
  with patch.object(r,'reg',return_value='10de'),patch.object(r,'unload'),patch.object(r,'pulse'),patch.object(r,'restore_pci'),patch.object(r,'modules',return_value=set()),patch.object(r,'log'),patch.object(r,'run',side_effect=[(0,''),(0,'Transfer correctness check: passed')]) as run:
   r.initialize_once({});self.assertEqual(run.call_args_list[0].args[0],['/usr/sbin/modprobe','--all','nvidia','nvidia_uvm'])

class WatchTests(unittest.TestCase):
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
 def test_three_link_failures_exhaust_budget(self):
  clock=[100]
  with patch.object(w,'healthy',return_value=False),patch.object(w.time,'monotonic',side_effect=lambda:clock[0]),patch.object(w.subprocess,'run',side_effect=[Mock(returncode=n) for n in (0,75,0,75,0,75,0)]) as run:
   c=w.Controller();c.check();clock[0]=160;c.check();self.assertEqual(c.next_attempt,400)
   clock[0]=400;c.check();clock[0]=1000;c.check()
   self.assertTrue(c.failed);self.assertFalse(c.pending_retry)
   self.assertEqual(sum(call.args[0]==[w.RECOVERY] for call in run.call_args_list),3)
 def test_pending_retry_survives_same_boot_watcher_restart(self):
  with tempfile.TemporaryDirectory() as directory,patch.object(w,'healthy',return_value=False),patch.object(w.time,'monotonic',return_value=100),patch.object(w.subprocess,'run',side_effect=[Mock(returncode=0),Mock(returncode=75)]):
   path=pathlib.Path(directory)/'watch.json';c=w.Controller(path);c.check()
   restarted=w.Controller(path);self.assertTrue(restarted.pending_retry)
   self.assertEqual(restarted.next_attempt,160);self.assertEqual(list(restarted.attempts),[100])
 def test_pending_retry_does_not_leave_held_services_off_if_nvml_returns(self):
  clock=[100]
  with patch.object(w,'healthy',side_effect=[False,True]),patch.object(w.time,'monotonic',side_effect=lambda:clock[0]),patch.object(w.subprocess,'run',side_effect=[Mock(returncode=n) for n in (0,75,0,0)]) as run:
   c=w.Controller();c.check();clock[0]=160;c.check()
   self.assertFalse(c.pending_retry);self.assertFalse(c.failed)
   self.assertEqual(sum(call.args[0]==[w.RECOVERY] for call in run.call_args_list),2)

class LinkReadinessTests(unittest.TestCase):
 def test_only_completed_perst_readiness_failure_is_retryable(self):
  self.assertEqual(r.error_exit_status(r.LinkNotReady('not ready')),75)
  for error in (r.CommandTimeout('blocked'),RuntimeError('GPIO mismatch'),r.InitializationFailed('still failed')):
   self.assertEqual(r.error_exit_status(error),1)
 def test_pulse_wait_exhaustion_has_specific_status(self):
  with patch.object(r,'log'),patch.object(r,'run',return_value=(0,'')),patch.object(r,'reg',return_value='ffff'),patch.object(r.time,'monotonic',side_effect=[100,100,116]),patch.object(r.time,'sleep'):
   with self.assertRaises(r.LinkNotReady):r.pulse()
 def test_failed_gpio_operation_never_classified_as_link_readiness(self):
  with patch.object(r,'log'),patch.object(r,'run',side_effect=RuntimeError('GPIO mismatch')):
   with self.assertRaises(RuntimeError) as error:r.pulse()
   self.assertEqual(r.error_exit_status(error.exception),1)
