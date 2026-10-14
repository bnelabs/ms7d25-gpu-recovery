#!/usr/bin/python3
"""Watch new kernel events only; invoke the verified server-specific recovery."""
import collections, json, pathlib, re, subprocess, time

attempts = collections.deque()
failed = False
print('Watching GPU 0000:01:00 Xid79; GPIO PERST recovery; never reboot',flush=True)
with subprocess.Popen(['/usr/bin/journalctl','-k','-f','-n','0','-o','json'],
                      stdout=subprocess.PIPE,text=True) as journal:
    for line in journal.stdout:
        try:
            message = json.loads(line).get('MESSAGE','')
        except ValueError: continue
        if not re.search(r'Xid\s*\(PCI:0000:01:00\):\s*79\b',message): continue
        now = time.time()
        while attempts and now-attempts[0] > 3600: attempts.popleft()
        if failed or len(attempts) >= 3:
            print('Recovery suppressed after failure or three attempts/hour; no reboot',flush=True)
            continue
        attempts.append(now)
        print(message,flush=True)
        result = subprocess.run(['/usr/local/sbin/gpu-recover-ms7d25'])
        failed = result.returncode != 0
