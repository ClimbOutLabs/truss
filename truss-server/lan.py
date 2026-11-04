#!/usr/bin/env python3
"""
The LAN address the server binds and prints, on any computer.

  1. TRUSS_HOST, when it is set (an address of this computer, or
     0.0.0.0 to listen on every interface).
  2. On macOS, the Wi-Fi (en0) address from `ipconfig getifaddr en0`,
     as before.
  3. Elsewhere (Windows, Linux), the address this computer would send
     from to reach the wider network. A UDP socket is "connected" to a
     public address; no packet is sent, the system only picks the
     outgoing interface.

None when nothing is found; callers choose their own fallback.

Stdlib only.
"""

import os
import socket
import subprocess
import sys


def _mac_en0():
    try:
        out = subprocess.run(
            ["ipconfig", "getifaddr", "en0"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.SubprocessError):
        return None
    return out or None


def _routed():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1, never actually reached
        addr = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    if not addr or addr.startswith("127.") or addr == "0.0.0.0":
        return None
    return addr


def lan_address(platform=None):
    """This computer's LAN address, or None. See the module notes."""
    wanted = os.environ.get("TRUSS_HOST", "").strip()
    if wanted:
        return wanted
    if (platform or sys.platform) == "darwin":
        return _mac_en0()
    return _routed()
