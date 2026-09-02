"""Is there a user systemd we can actually drive? One definition, two callers.

`systemctl --user is-system-running` exits 1 both when the manager answers
"degraded" — a perfectly usable manager with one failed unit — and when there is
no user bus at all, where it prints to stderr:

    Failed to connect to user scope bus via local transport:
    $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined

Accepting the exit code alone therefore reads "available" inside a CI container,
and the two systemd tests FAIL there instead of skipping. Proven by running the
suite with the bus variables unset: 2 failed. So the discriminant is what the
manager PRINTS, not how it exits.

This lives in its own module rather than being copied into each test file: it
was copied once already, and both copies carried the same defect.
"""
from __future__ import annotations

import shutil
import subprocess

#: What `is-system-running` prints when a manager is there AND usable.
#:
#: "offline" and "unknown" are deliberately absent: systemd prints them when
#: there is no manager to talk to, which is the container case this predicate
#: exists to detect. Accepting them would put the two systemd tests back where
#: they were — failing in CI instead of skipping.
_STATES = {"running", "degraded", "maintenance", "initializing", "starting",
           "stopping"}


def user_systemd_available() -> bool:
    if shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        return False
    try:
        r = subprocess.run(["systemctl", "--user", "is-system-running"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.stdout.strip() in _STATES
