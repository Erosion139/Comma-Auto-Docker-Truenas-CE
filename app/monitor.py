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

BASE_QUERY = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu"
# Encoder/decoder engine load is appended, so the base field order never changes.
QUERY = BASE_QUERY + ",utilization.encoder,utilization.decoder"


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _nvidia_smi(query: str):
    return subprocess.run(
        ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=5,
    )


def poll_gpu_once() -> None:
    if not shutil.which("nvidia-smi"):
        _set_gpu(False, [], "nvidia-smi not found — no NVIDIA GPU runtime in this container.")
        return
    try:
        r = _nvidia_smi(QUERY)
        engines = True
        if r.returncode != 0:
            # Older drivers may not expose the engine fields; keep the basic card working anyway.
            r = _nvidia_smi(BASE_QUERY)
            engines = False
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
        gpus.append({
            "index": int(parts[0]),
            "name": parts[1],
            "util_pct": _num(parts[2]),
            "mem_used_mb": _num(parts[3]),
            "mem_total_mb": _num(parts[4]),
            "temp_c": _num(parts[5]),
            # NVENC / NVDEC engine busy percentage (None when the driver doesn't report it)
            "enc_pct": _num(parts[6]) if engines and len(parts) > 6 else None,
            "dec_pct": _num(parts[7]) if engines and len(parts) > 7 else None,
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
