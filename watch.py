#!/usr/bin/python3
"""Watch Xid79 and NVML health; never resume inference after failed recovery."""
import collections
import json
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
    def __init__(self):
        self.attempts=collections.deque();self.failed=False
        self.last_recovery_end=0
    def check(self,event=False,event_time=None):
        try:ok=healthy()
        except subprocess.TimeoutExpired:
            self.hold();self.failed=True
            print('GPU health query blocked; services held, no reset attempted',flush=True);return
        if ok and (not event or event_time is not None and event_time<=self.last_recovery_end):return
        if not self.hold():self.failed=True;return
        now=time.monotonic()
        while self.attempts and now-self.attempts[0]>=3600:self.attempts.popleft()
        if self.failed or len(self.attempts)>=3:
            print('GPU recovery suppressed; inference services remain stopped',flush=True);return
        self.attempts.append(now)
        print('Recovering GPU after '+('Xid79' if event else 'failed health check'),flush=True)
        # Adapter bounds individual hardware calls and owns the lock. Never kill
        # it mid-PERST/teardown with an outer timeout.
        self.failed=subprocess.run([RECOVERY]).returncode!=0
        self.last_recovery_end=time.monotonic()
    def hold(self):
        try:
            result=subprocess.run([RECOVERY,'--hold-services'],timeout=120)
            if result.returncode:raise RuntimeError('service hold failed')
            return True
        except (subprocess.TimeoutExpired,RuntimeError) as exc:
            print('Cannot hold inference services: '+str(exc),flush=True);return False


def main():
    events=queue.Queue()
    with subprocess.Popen(['/usr/bin/journalctl','-k','-f','-n','0','-o','json'],
                          stdout=subprocess.PIPE,text=True) as journal:
        def read():
            for line in journal.stdout:events.put(line)
            events.put(None)
        threading.Thread(target=read,daemon=True).start()
        controller=Controller()
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
