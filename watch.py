#!/usr/bin/python3
"""Watch Xid79 and NVML health; never resume inference after failed recovery."""
import collections
import json
import pathlib
import os
import tempfile
import queue
import re
import subprocess
import threading
import time

RECOVERY='/usr/local/sbin/gpu-recover-ms7d25'
GPU='0000:01:00.0'
UUID='GPU-8a9c05b3-fea1-4d89-5113-b56acbb1eba9'

def healthy():
    result=subprocess.run(['/usr/bin/nvidia-smi','-i',GPU,'--query-gpu=uuid',
                           '--format=csv,noheader'],capture_output=True,text=True,timeout=10)
    return result.returncode==0 and result.stdout.strip()==UUID


class Controller:
    def __init__(self,state_path=None):
        self.attempts=collections.deque();self.failed=False
        self.last_recovery_end=0;self.pending_retry=False;self.next_attempt=0
        self.path=state_path;self.boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if self.path and self.path.exists():
            saved=json.loads(self.path.read_text())
            if saved['boot_id']==self.boot:
                self.attempts=collections.deque(saved['attempts'])
                self.pending_retry=saved['pending_retry'];self.next_attempt=saved['next_attempt']
    def save(self):
        if not self.path:return
        self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix='.watch-',dir=self.path.parent)
        try:
            with os.fdopen(fd,'w') as stream:
                json.dump({'boot_id':self.boot,'attempts':list(self.attempts),
                           'pending_retry':self.pending_retry,'next_attempt':self.next_attempt},stream)
                stream.flush();os.fsync(stream.fileno())
            os.replace(name,self.path)
        finally:
            if os.path.exists(name):os.unlink(name)
    def check(self,event=False,event_time=None):
        try:ok=healthy()
        except subprocess.TimeoutExpired:
            self.hold();self.failed=True;self.pending_retry=False;self.save()
            print('GPU health query blocked; services held, no reset attempted',flush=True);return
        if ok and not self.pending_retry and (not event or event_time is not None and event_time<=self.last_recovery_end):return
        if not self.hold():self.failed=True;return
        now=time.monotonic()
        while self.attempts and now-self.attempts[0]>=3600:self.attempts.popleft()
        if self.pending_retry and now<self.next_attempt:return
        if self.failed or len(self.attempts)>=3:
            print('GPU recovery suppressed; inference services remain stopped',flush=True);return
        self.attempts.append(now);self.pending_retry=False;self.save()
        print('Recovering GPU after '+('Xid79' if event else 'failed health check'),flush=True)
        # Adapter bounds individual hardware calls and owns the lock. Never kill
        # it mid-PERST/teardown with an outer timeout.
        rc=subprocess.run([RECOVERY]).returncode
        self.last_recovery_end=time.monotonic()
        if rc==75 and len(self.attempts)<3:
            delay=(60,240)[len(self.attempts)-1]
            self.pending_retry=True;self.next_attempt=self.last_recovery_end+delay
            print(f'Completed PERST but GPU link not ready; retry scheduled in {delay}s, inference remains stopped',flush=True)
        else:
            self.failed=rc!=0;self.next_attempt=0
            if self.failed:print('Recovery failed without an eligible retry; inference remains stopped',flush=True)
        self.save()
    def hold(self):
        try:
            result=subprocess.run([RECOVERY,'--hold-services'],timeout=120)
            if result.returncode:raise RuntimeError('service hold failed')
            return True
        except (subprocess.TimeoutExpired,RuntimeError) as exc:
            print('Cannot hold inference services: '+str(exc),flush=True);return False


def main():
    events=queue.Queue(maxsize=1024)
    with subprocess.Popen(['/usr/bin/journalctl','-k','-f','-n','0','-o','json'],
                          stdout=subprocess.PIPE,text=True) as journal:
        def read():
            for line in journal.stdout:events.put(line)
            events.put(None)
        threading.Thread(target=read,daemon=True).start()
        controller=Controller(pathlib.Path('/var/lib/gpu-recovery/watch.json'))
        print('Watching Xid79 and GPU health every 15s; failed recovery keeps inference stopped',flush=True)
        controller.check()
        next_check=time.monotonic()+15
        while True:
            if time.monotonic()>=next_check:
                controller.check();next_check=time.monotonic()+15
            try:line=events.get(timeout=max(.1,next_check-time.monotonic()))
            except queue.Empty:continue
            if line is None:raise RuntimeError('Kernel journal stream ended')
            try:entry=json.loads(line)
            except ValueError:continue
            if not re.search(r'Xid\s*\(PCI:0000:01:00(?:\.0)?\):\s*79\b',entry.get('MESSAGE',''),re.I):continue
            try:event_time=int(entry['__MONOTONIC_TIMESTAMP'])/1e6
            except (KeyError,ValueError,TypeError):event_time=None
            controller.check(True,event_time)


if __name__=='__main__':main()
