"""Talk to the comma device over SSH/SFTP using the app's key.

The comma's IP changes on most connects, so the address is resolved automatically
(see discovery.py). A fixed address in Settings is only used as a first guess.

Footage layout on the device (from the comma-sync project):
  /data/media/0/realdata/<route>--<segment>/{fcamera.hevc, ecamera.hevc, dcamera.hevc, qcamera.ts}
  road = fcamera, wide = ecamera, driver = dcamera, mic audio = qcamera.ts
"""
import logging
import re
import socket
import time
from collections import defaultdict

import paramiko

# Sweeping the LAN makes paramiko log a traceback for every non-SSH host it touches.
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

from . import config, discovery, sshkeys

REALDATA = "/data/media/0/realdata"
CAMERAS = {"fcamera.hevc": "road", "ecamera.hevc": "wide", "dcamera.hevc": "driver"}
SEG_RE = re.compile(r"^(?P<route>.+)--(?P<seg>\d+)$")


class Stop(Exception):
    """Raised inside a progress callback to abort a transfer cleanly."""


def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _make_client(host: str, timeout: float = 10) -> paramiko.SSHClient:
    s = config.load()
    client = paramiko.SSHClient()
    # Comma host keys change on reflash, so we don't pin them (same approach as comma-sync).
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host, port=s.comma_port, username=s.comma_user,
        key_filename=sshkeys.PRIV, timeout=timeout, banner_timeout=timeout,
        auth_timeout=timeout, look_for_keys=False, allow_agent=False,
    )
    client.get_transport().set_keepalive(10)
    return client


def _probe(host: str) -> tuple[bool, str]:
    """One attempt: connect with the app key and check for openpilot."""
    try:
        client = _make_client(host, timeout=5)
    except paramiko.AuthenticationException:
        return False, "SSH answered but rejected the app's key"
    except (paramiko.SSHException, OSError) as e:
        return False, f"no usable SSH service ({e.__class__.__name__})"
    try:
        _, out, _ = client.exec_command("test -d /data/openpilot && echo ok || echo no", timeout=10)
        if out.read().decode().strip() == "ok":
            return True, "comma (openpilot found)"
        return False, "SSH works but /data/openpilot is missing (not a comma)"
    except Exception as e:  # noqa: BLE001
        return False, f"command failed ({e.__class__.__name__})"
    finally:
        client.close()


def _is_comma(host: str) -> tuple[bool, str]:
    """Accepts our key and has the openpilot install. Returns (ok, reason).

    A rejected key is final. Other failures get one retry, since a device that just
    woke up or is under load can drop a single command.
    """
    ok, reason = _probe(host)
    if ok or "rejected" in reason:
        return ok, reason
    time.sleep(1)
    return _probe(host)


def resolve_host(force: bool = False) -> str:
    """Return the comma's current address, searching the network when needed.

    The stored address is used while it still answers on the SSH port. Otherwise the
    network is searched and the result is saved for next time.
    """
    s = config.load()
    if not force and s.comma_host and port_open(s.comma_host, s.comma_port):
        return s.comma_host
    if not sshkeys.check_local()[0]:
        raise RuntimeError("SSH key is missing or invalid — fix it in the SSH key panel first.")
    try:
        host, method = discovery.find(
            _is_comma, s.comma_port,
            last_known=s.comma_host, scan_subnets=s.scan_subnets,
        )
    except discovery.DiscoveryError as e:
        raise RuntimeError(str(e)) from e
    config.update({"comma_host": host, "comma_found_via": method})
    return host


def connect(timeout: float = 10) -> paramiko.SSHClient:
    """Open an SSH session with the app key to the comma, found automatically."""
    host = resolve_host()
    return _make_client(host, timeout=timeout)


def test_connection() -> tuple[bool, str]:
    """Reachable, key accepted, and the device looks like an openpilot comma."""
    try:
        host = resolve_host(force=True)
    except RuntimeError as e:
        return False, str(e)
    s = config.load()
    try:
        client = _make_client(host)
    except paramiko.AuthenticationException:
        return False, (f"Found a device at {host}, but it rejected the key. Check the GitHub "
                       "registration and the GitHub username on the comma.")
    except (paramiko.SSHException, OSError) as e:
        return False, f"SSH to {host}:{s.comma_port} failed: {e}"
    try:
        _, out, _ = client.exec_command("test -d /data/openpilot && echo ok || echo no", timeout=10)
        if out.read().decode().strip() != "ok":
            return False, "Connected, but /data/openpilot not found — is this a comma device?"
        via = s.comma_found_via or "found"
        return True, f"Connected to the comma at {host} ({via})."
    finally:
        client.close()


def list_drives(client: paramiko.SSHClient) -> list[dict]:
    """Group every segment on the device by route. Only segments with footage count."""
    sftp = client.open_sftp()
    try:
        try:
            names = sftp.listdir(REALDATA)
        except FileNotFoundError:
            return []
        routes = defaultdict(lambda: {"segments": {}, "files": {}, "mtime": 0, "start": None, "bytes": 0})
        for name in names:
            m = SEG_RE.match(name)
            if not m or name.startswith("boot"):
                continue
            route, seg = m["route"], int(m["seg"])
            try:
                entries = sftp.listdir_attr(f"{REALDATA}/{name}")
            except OSError:
                continue
            files = {}
            for e in entries:
                if e.filename in CAMERAS or e.filename == "qcamera.ts":
                    files[e.filename] = e.st_size or 0
                    routes[route]["bytes"] += e.st_size or 0
                    mt = e.st_mtime or 0
                    routes[route]["mtime"] = max(routes[route]["mtime"], mt)
                    cur = routes[route]["start"]
                    routes[route]["start"] = mt if cur is None else min(cur, mt)
            if files:
                routes[route]["segments"][seg] = name
                routes[route]["files"][name] = files
        out = []
        for route, info in routes.items():
            out.append({
                "route": route,
                "segments": sorted(info["segments"].items()),  # [(seg_no, dirname), ...]
                "file_map": info["files"],
                "bytes": info["bytes"],
                "mtime": info["mtime"],
                "start": info["start"] or info["mtime"],
            })
        out.sort(key=lambda r: r["mtime"])
        return out
    finally:
        sftp.close()


def download_file(sftp, remote: str, local: str, progress) -> None:
    """Download one file. progress(bytes_delta) is called as data arrives.

    Raising Stop from progress aborts the transfer.
    """
    last = {"v": 0}

    def cb(done, total):
        delta = done - last["v"]
        last["v"] = done
        if delta:
            progress(delta)

    sftp.get(remote, local, callback=cb)
