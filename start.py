#!/usr/bin/env python3
"""WoT Bot launcher — email/password baked in, no typing needed."""
import subprocess, sys

EMAIL = "gravitya942" + "@" + "gmail.com"
PASSWORD = "Ismail2011ismail" + "@"

cmd = [
    sys.executable, "wot_grinder.py",
    "--email", EMAIL,
    "--password", PASSWORD,
    "--cycles", sys.argv[1] if len(sys.argv) > 1 else "1",
    "--speed", sys.argv[2] if len(sys.argv) > 2 else "fast",
]

print(f"Running: {' '.join(cmd[:6])} ... --cycles {cmd[6]} --speed {cmd[7]}")
subprocess.run(cmd)
