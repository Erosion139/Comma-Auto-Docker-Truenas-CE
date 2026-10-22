"""Live GPU stats (nvidia-smi) and network throughput (/proc/net/dev)."""
import shutil
import subprocess
import threading
import time

_gpu_lock = threading.Lock()
_gpu_state = {"available": False, "gpus": [], "error": "Not polled yet", "updated": 0}

_net_lock = threading.Lock()
_net_prev = {"t": 0.0, "rx": 0, "tx": 0}
_net_state = {"rx_mbps": 0.0, "tx_mbps": 0.0}

QUERY = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu"


def poll_gpu_once() -> None:
    if not shutil.which("nvidia-smi"):
        _set_gpu(False, [], "nvidia-smi not found — no NVIDIA GPU runtime in this container.")
        return
    try:
        r = subprocess.run(
            ["nvidia-smi", f"--query-gpu={QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _set_gpu(False, [], f"nvidia-smi failed: {e}")
        return
    if r.returncode != 0 or not r.stdout.strip():
        _set_gpu(False, [], (r.stderr or "no GPU reported").strip()[:300])
        return
    gpus = []
    for line in r.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue

        def num(x):
            try:
                return float(x)
            except ValueError:
                return None
        gpus.append({
            "index": int(parts[0]),
            "name": parts[1],
            "util_pct": num(parts[2]),
            "mem_used_mb": num(parts[3]),
            "mem_total_mb": num(parts[4]),
            "temp_c": num(parts[5]),
        })
    _set_gpu(True, gpus, "")


def _set_gpu(ok, gpus, err):
    with _gpu_lock:
        _gpu_state.update(available=ok, gpus=gpus, error=err, updated=time.time())


def gpu_snapshot() -> dict:
    with _gpu_lock:
        return dict(_gpu_state)


def poll_net_once() -> None:
    """Throughput across non-loopback interfaces, in megabits/s."""
    rx = tx = 0
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                iface, data = line.split(":", 1)
                if iface.strip() == "lo":
                    continue
                cols = data.split()
                rx += int(cols[0])
                tx += int(cols[8])
    except (OSError, ValueError, IndexError):
        return
    now = time.time()
    with _net_lock:
        dt = now - _net_prev["t"]
        if _net_prev["t"] and dt > 0:
            _net_state["rx_mbps"] = (rx - _net_prev["rx"]) * 8 / dt / 1e6
            _net_state["tx_mbps"] = (tx - _net_prev["tx"]) * 8 / dt / 1e6
        _net_prev.update(t=now, rx=rx, tx=tx)


def net_snapshot() -> dict:
    with _net_lock:
        return dict(_net_state)


def start_background(interval: float = 2.0) -> None:
    def loop():
        while True:
            poll_gpu_once()
            poll_net_once()
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True, name="monitor").start()
