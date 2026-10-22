"""Talk to the comma device over SSH/SFTP using the app's key.

Footage layout on the device (from the comma-sync project):
  /data/media/0/realdata/<route>--<segment>/{fcamera.hevc, ecamera.hevc, dcamera.hevc, qcamera.ts}
  road = fcamera, wide = ecamera, driver = dcamera, mic audio = qcamera.ts
"""
import re
import socket
from collections import defaultdict

import paramiko

from . import config, sshkeys

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


def connect(timeout: float = 10) -> paramiko.SSHClient:
    """Open an SSH session with the app key. Raises on failure."""
    s = config.load()
    if not s.comma_host:
        raise RuntimeError("No comma address set. Enter it in Settings.")
    if not sshkeys.check_local()[0]:
        raise RuntimeError("SSH key is missing or invalid.")
    client = paramiko.SSHClient()
    # Comma host keys change on reflash, so we don't pin them (same approach as comma-sync).
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        s.comma_host, port=s.comma_port, username=s.comma_user,
        key_filename=sshkeys.PRIV, timeout=timeout, banner_timeout=timeout,
        auth_timeout=timeout, look_for_keys=False, allow_agent=False,
    )
    client.get_transport().set_keepalive(10)
    return client


def test_connection() -> tuple[bool, str]:
    """Full check: reachable, key accepted, and the device looks like an openpilot comma."""
    s = config.load()
    if not s.comma_host:
        return False, "No comma address set."
    if not port_open(s.comma_host, s.comma_port):
        return False, f"Nothing listening at {s.comma_host}:{s.comma_port}. Is the comma on the same network with SSH enabled?"
    try:
        client = connect()
    except paramiko.AuthenticationException:
        return False, "Comma rejected the key. Check GitHub registration and the GitHub username on the comma."
    except (paramiko.SSHException, OSError) as e:
        return False, f"SSH failed: {e}"
    try:
        _, out, _ = client.exec_command("test -d /data/openpilot && echo ok || echo no")
        if out.read().decode().strip() != "ok":
            return False, "Connected, but /data/openpilot not found — is this a comma device?"
        return True, "Connected to the comma and authenticated."
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
    """Download one file. progress(bytes_done_this_call_delta) is called as data arrives.

    Raising Stop from progress aborts the transfer.
    """
    last = {"v": 0}

    def cb(done, total):
        delta = done - last["v"]
        last["v"] = done
        if delta:
            progress(delta)

    sftp.get(remote, local, callback=cb)
