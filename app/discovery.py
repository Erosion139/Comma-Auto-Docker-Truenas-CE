"""Find the comma on the local network without a fixed IP.

The comma gets a new DHCP address on most connects, so the app never relies on one.
Search order:
  1. the last address that worked (instant when the device hasn't moved)
  2. mDNS / Bonjour: the comma advertises an SSH service with "comma" in its name
  3. a subnet sweep for hosts with port 22 open

Every candidate is verified by the caller (it must accept the app's key and have
/data/openpilot), so a random SSH server on the LAN is never mistaken for the comma.
"""
import ipaddress
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

MAX_SWEEP_HOSTS = 4096          # guards against huge networks (e.g. a /8 scope)
PROBE_TIMEOUT = 0.7             # seconds per TCP probe
MDNS_WAIT = 3.0                 # seconds to listen for mDNS answers

_lock = threading.Lock()


class DiscoveryError(RuntimeError):
    pass


def _primary_ipv4() -> str | None:
    """The address of the interface used for LAN traffic (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))   # TEST-NET address, routing lookup only
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def local_networks(override: str = "") -> list[ipaddress.IPv4Network]:
    """Subnets to sweep: the user's override list, or the /24 of the primary interface."""
    if override.strip():
        nets = []
        for part in override.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                continue
        return [n for n in nets if isinstance(n, ipaddress.IPv4Network)]
    ip = _primary_ipv4()
    if not ip:
        return []
    return [ipaddress.ip_network(f"{ip}/24", strict=False)]


def mdns_hosts(wait: float = MDNS_WAIT) -> list[str]:
    """IPv4 addresses of SSH services whose name mentions 'comma'. Empty if mDNS is unavailable."""
    try:
        from zeroconf import ServiceBrowser, ServiceStateChange, Zeroconf
    except ImportError:
        return []
    found: list[str] = []
    lock = threading.Lock()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change is not ServiceStateChange.Added or "comma" not in name.lower():
            return
        try:
            info = zeroconf.get_service_info(service_type, name, timeout=2000)
        except Exception:  # noqa: BLE001 - mDNS is best effort
            return
        if info is None:
            return
        with lock:
            for addr in info.parsed_addresses():
                try:
                    if isinstance(ipaddress.ip_address(addr), ipaddress.IPv4Address):
                        found.append(addr)
                except ValueError:
                    pass

    try:
        zc = Zeroconf()
    except OSError:
        return []
    try:
        ServiceBrowser(zc, "_ssh._tcp.local.", handlers=[on_change])
        threading.Event().wait(wait)
    finally:
        zc.close()
    seen, out = set(), []
    for a in found:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def sweep(nets: list[ipaddress.IPv4Network], port: int) -> list[str]:
    """Hosts in the given networks that accept a TCP connection on port."""
    hosts: list[str] = []
    for net in nets:
        for h in net.hosts():
            hosts.append(str(h))
            if len(hosts) >= MAX_SWEEP_HOSTS:
                break
        if len(hosts) >= MAX_SWEEP_HOSTS:
            break
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=128) as ex:
        results = list(ex.map(lambda h: h if _port_open(h, port) else None, hosts))
    return [h for h in results if h]


last_report: list[dict] = []   # what the most recent search tried, and why each failed


def find(verify, port: int, last_known: str = "", scan_subnets: str = "") -> tuple[str, str]:
    """Return (host, method) for the first candidate that passes verify(host).

    verify(host) returns (ok: bool, reason: str). The reasons are kept in last_report
    so a failure can say exactly what was wrong (for example, the key was rejected).
    Raises DiscoveryError when nothing matches.
    """
    with _lock:  # one search at a time (scheduler and UI can both ask)
        tried: set[str] = set()
        report: list[dict] = []

        def attempt(host: str, method: str):
            if not host or host in tried:
                return None
            tried.add(host)
            try:
                ok, reason = verify(host)
            except Exception as e:  # noqa: BLE001 - a failing candidate is skipped
                ok, reason = False, f"error: {e}"
            report.append({"host": host, "method": method, "ok": ok, "reason": reason})
            return (host, method) if ok else None

        def finish(err: str):
            last_report[:] = report
            return DiscoveryError(err)

        if last_known:
            r = attempt(last_known, "last known address")
            if r:
                last_report[:] = report
                return r

        mdns = mdns_hosts()
        for host in mdns:
            r = attempt(host, "mDNS")
            if r:
                last_report[:] = report
                return r

        nets = local_networks(scan_subnets)
        if not nets:
            raise finish("Could not determine the local network to search. "
                         "Set 'Networks to search' in Settings (for example 192.168.1.0/24).")
        for host in sweep(nets, port):
            r = attempt(host, "subnet scan")
            if r:
                last_report[:] = report
                return r

        where = ", ".join(str(n) for n in nets)
        rejected = [x for x in report if not x["ok"] and "rejected" in x["reason"]]
        if rejected:
            hosts = ", ".join(x["host"] for x in rejected)
            raise finish(
                f"Found SSH at {hosts}, but it rejected the app's key. The comma only accepts keys "
                "on your GitHub account. Add the app's public key to GitHub (or import the key "
                "Comma Sync uses), then re-enter the GitHub username on the comma.")
        if mdns:
            raise finish("A comma was advertised on mDNS but did not respond to SSH. "
                         "Check that SSH is enabled on the comma.")
        raise finish(f"No comma found (searched mDNS and {where}). "
                     "Check that it is powered on, on the same network, and has SSH enabled.")
