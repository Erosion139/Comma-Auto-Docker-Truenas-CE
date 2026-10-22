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


def find(verify, port: int, last_known: str = "", scan_subnets: str = "") -> tuple[str, str]:
    """Return (host, method) for the first candidate that passes verify(host).

    Raises DiscoveryError when nothing matches.
    """
    with _lock:  # one search at a time (scheduler and UI can both ask)
        tried: set[str] = set()

        def attempt(host: str, method: str):
            if not host or host in tried:
                return None
            tried.add(host)
            try:
                ok = verify(host)
            except Exception:  # noqa: BLE001 - a failing candidate is simply skipped
                ok = False
            return (host, method) if ok else None

        if last_known:
            r = attempt(last_known, "last known address")
            if r:
                return r

        for host in mdns_hosts():
            r = attempt(host, "mDNS")
            if r:
                return r

        nets = local_networks(scan_subnets)
        if not nets:
            raise DiscoveryError("Could not determine the local network to search. "
                                 "Set 'Networks to search' in Settings (for example 192.168.1.0/24).")
        for host in sweep(nets, port):
            r = attempt(host, "subnet scan")
            if r:
                return r

        where = ", ".join(str(n) for n in nets)
        raise DiscoveryError(f"No comma found (searched mDNS and {where}). "
                             "Check that it is powered on, on the same network, and has SSH enabled.")
