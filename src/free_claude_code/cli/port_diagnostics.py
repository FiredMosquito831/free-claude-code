"""Bounded port-availability and ownership diagnostics for the server bind.

Used when a supervisor cannot bind its configured host:port: these helpers
answer two questions without ever disturbing a running process:

* ``probe_port_available`` -- can a socket bind here at all right now?
* ``diagnose_port_owner`` -- if not, which process holds the listener?

Both are read-only. They never send data to a peer and never kill or signal
the owner they discover; reporting is the entire job.
"""

import errno
import re
import socket
import subprocess
import time
from dataclasses import dataclass

_PORT_TOKEN = re.compile(r"(?<=[:\s])(\d+)(?=\s|$)")
_PROCESS_TOKEN = re.compile(r'users?:\(\("([^"]*)",pid=(\d+)')
_NETSTAT_PID = re.compile(r"(\d+)\s*$")


@dataclass(slots=True)
class PortOwner:
    """The process holding a listening socket, best effort."""

    pid: int | None
    name: str | None
    command: str | None


def _family_for(host: str) -> socket.AddressFamily:
    """Pick the socket family for ``host`` without resolving it."""
    if ":" in host:
        return socket.AF_INET6
    return socket.AF_INET


def probe_port_available(host: str, port: int, *, timeout: float = 2.0) -> bool:
    """Return True if a socket can bind ``(host, port)`` (it is free).

    A successful bind means nothing is listening yet, so the server can still
    rebind. This only probes availability; it sends no bytes and lasts at most
    ``timeout`` seconds.
    """

    sock = socket.socket(_family_for(host), socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def wait_for_port_free(
    host: str, port: int, *, timeout: float = 5.0, interval: float = 0.2
) -> bool:
    """Poll ``probe_port_available`` until the socket frees or ``timeout``.

    A previous server generation often holds the port for a beat after it
    stops, so a restart should wait a bounded moment before declaring a real
    conflict. Returns True once the port can be bound, False on timeout.
    """

    deadline = time.monotonic() + max(timeout, interval)
    while time.monotonic() < deadline:
        if probe_port_available(host, port):
            return True
        time.sleep(interval)
    return probe_port_available(host, port)


def _owner_from_line(line: str, port: int) -> PortOwner | None:
    """Extract the listener on ``port`` from one ``ss``/``netstat`` line."""

    if f":{port}" not in line:
        return None
    lowered = line.lower()
    if "listen" not in lowered and "listening" not in lowered:
        return None

    match = _PROCESS_TOKEN.search(line)
    if match:
        try:
            pid = int(match.group(2))
        except ValueError:
            pid = None
        return PortOwner(pid=pid, name=match.group(1) or None, command=None)

    # ``netstat`` puts the pid in the trailing column.
    pid_match = _NETSTAT_PID.search(line)
    if pid_match:
        try:
            pid = int(pid_match.group(1))
        except ValueError:
            pid = None
        else:
            return PortOwner(pid=pid, name=None, command=None)
    return None


def diagnose_port_owner(
    host: str, port: int, *, timeout: float = 2.0
) -> PortOwner | None:
    """Best-effort identify the process holding ``host:port``.

    Tries ``ss`` first, then ``netstat``. Either command may be missing, so
    failures are swallowed and the next probe is tried. Returns ``None`` when
    no owner can be determined; this never kills or signals the process.
    """

    for command in (("ss", "-ltnp"), ("netstat", "-ano")):
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except OSError, subprocess.SubprocessError:
            continue
        if completed.returncode != 0:
            continue
        for line in completed.stdout.splitlines():
            owner = _owner_from_line(line, port)
            if owner is not None:
                return owner
    return None


def is_address_in_use(exc: OSError) -> bool:
    """Whether ``exc`` is a bind failure because the address is taken."""

    if exc.errno == errno.EADDRINUSE:
        return True
    return "address already in use" in str(exc).lower()
