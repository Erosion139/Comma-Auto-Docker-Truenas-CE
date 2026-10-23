"""Setup checks: each check inspects one part of the setup and reports ok / warn / fail / skip.

Results carry a short detail and a fix hint so the UI can tell the user what to do next.
"""
import os
import shutil
import threading
import time

from . import comma, config, encoder, monitor, sshkeys

_cache_lock = threading.Lock()
_cache = {"at": 0.0, "results": None}
CACHE_SECS = 20


def _res(cid, label, status, detail, hint=""):
    return {"id": cid, "label": label, "status": status, "detail": detail, "hint": hint}


def check_storage():
    s = config.load()
    out = s.output_dir
    try:
        os.makedirs(out, exist_ok=True)
    except OSError as e:
        return _res("storage", "Output storage", "fail", f"Cannot create {out}: {e}",
                    "Mount a TrueNAS dataset to the output path in the Docker/app config.")
    probe = os.path.join(out, ".write_test")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        return _res("storage", "Output storage", "fail", f"Not writable: {e}",
                    "Check the dataset permissions (apps user needs write access).")
    free_gb = shutil.disk_usage(out).free / 1e9
    if free_gb < s.min_free_gb:
        return _res("storage", "Output storage", "warn",
                    f"Writable, but only {free_gb:.1f} GB free (minimum {s.min_free_gb:g} GB).",
                    "Free up space on the dataset or lower the minimum in Settings.")
    return _res("storage", "Output storage", "ok", f"Writable, {free_gb:.1f} GB free at {out}.")


def check_config():
    s = config.load()
    if not s.github_user:
        return _res("config", "Settings", "warn",
                    "No GitHub username set.",
                    "Enter your GitHub username in Settings to verify the SSH key.")
    if s.comma_host:
        return _res("config", "Settings", "ok",
                    f"GitHub user '{s.github_user}'. Last comma address: {s.comma_host}"
                    f" ({s.comma_found_via or 'saved'}). Auto-find is on.")
    return _res("config", "Settings", "ok",
                f"GitHub user '{s.github_user}'. The comma will be found automatically on the network.")


def check_ffmpeg():
    ok, msg = encoder.ffmpeg_status()
    if ok:
        return _res("ffmpeg", "ffmpeg", "ok", msg)
    return _res("ffmpeg", "ffmpeg", "fail", msg, "Rebuild the image — ffmpeg should be installed by the Dockerfile.")


def check_ssh_key_local():
    ok, msg = sshkeys.check_local()
    if ok:
        return _res("ssh_key", "SSH key", "ok", f"{msg} {sshkeys.fingerprint()}".strip())
    return _res("ssh_key", "SSH key", "fail", msg, "Open the SSH key panel and click Generate key.")


def check_github():
    s = config.load()
    ok, msg = sshkeys.github_registered(s.github_user)
    if ok is True:
        return _res("github_key", "Key registered on GitHub", "ok", msg)
    if ok is None:
        return _res("github_key", "Key registered on GitHub", "warn", msg,
                    "Check your internet connection, or verify manually on github.com.")
    return _res("github_key", "Key registered on GitHub", "fail", msg,
                "Copy the public key from the SSH panel to github.com/settings/keys.")


def check_comma_connection():
    ok, msg = comma.test_connection()
    if ok:
        return _res("comma", "Comma SSH connection", "ok", msg)
    return _res("comma", "Comma SSH connection", "fail", msg,
                "On the comma: Settings → Developer → enable SSH and enter your GitHub username.")


def check_footage_path():
    if not sshkeys.check_local()[0]:
        return _res("footage", "Footage on comma", "skip", "Skipped — SSH key not ready.")
    try:
        client = comma.connect()
    except Exception as e:  # noqa: BLE001 - surface any failure as a check result
        return _res("footage", "Footage on comma", "skip", f"Skipped — {e}")
    try:
        drives = comma.list_drives(client)
    finally:
        client.close()
    if not drives:
        return _res("footage", "Footage on comma", "warn", "No drives found in realdata yet.",
                    "Drive a trip with the comma powered on, then rescan.")
    return _res("footage", "Footage on comma", "ok", f"{len(drives)} drive(s) on the device.")


def check_gpu():
    snap = monitor.gpu_snapshot()
    if snap["available"] and snap["gpus"]:
        names = ", ".join(g["name"] for g in snap["gpus"])
        return _res("gpu", "GPU", "ok", f"Detected: {names}.")
    return _res("gpu", "GPU", "warn", snap["error"] or "No GPU detected — encoding will use the CPU.",
                "For NVIDIA on TrueNAS, enable GPU passthrough for the app and install the NVIDIA container runtime.")


def check_encoder():
    s = config.load()
    if s.encoder == "cpu":
        return _res("encoder", "Video encoder", "ok", "Set to CPU (libx264) in Settings.")
    ok, msg = encoder.nvenc_works()
    if ok:
        return _res("encoder", "Video encoder", "ok", msg)
    if s.encoder == "nvenc":
        return _res("encoder", "Video encoder", "fail", msg,
                    "GPU encoding is forced but unavailable — check GPU passthrough, or switch to auto/cpu.")
    return _res("encoder", "Video encoder", "warn", msg + " Falling back to CPU.",
                "Optional: enable GPU passthrough for faster encoding.")


def check_decoder():
    s = config.load()
    if s.decoder == "cpu":
        return _res("decoder", "Video decoder", "ok",
                    "Set to CPU decoding in Settings. This uses more CPU; NVDEC is faster if you have an NVIDIA GPU.")
    ok, msg = encoder.nvdec_works()
    if ok:
        return _res("decoder", "Video decoder", "ok", msg + " Decoding runs on the GPU.")
    if s.decoder == "nvdec":
        return _res("decoder", "Video decoder", "fail", msg,
                    "NVDEC is forced but unavailable — check GPU passthrough and the 'video' driver capability.")
    return _res("decoder", "Video decoder", "warn", msg + " Falling back to CPU decoding, which is slow.",
                "Optional: enable GPU passthrough with the video capability for NVDEC.")


CHECKS = [
    check_config, check_storage, check_ffmpeg, check_ssh_key_local,
    check_github, check_comma_connection, check_footage_path, check_gpu, check_encoder, check_decoder,
]


def run_all(force: bool = False) -> dict:
    with _cache_lock:
        if not force and _cache["results"] and time.time() - _cache["at"] < CACHE_SECS:
            return _cache["results"]
    results = []
    for fn in CHECKS:
        try:
            results.append(fn())
        except Exception as e:  # noqa: BLE001
            results.append(_res(fn.__name__, fn.__name__, "fail", f"Check crashed: {e}"))
    summary = "ok"
    if any(r["status"] == "fail" for r in results):
        summary = "fail"
    elif any(r["status"] == "warn" for r in results):
        summary = "warn"
    out = {"summary": summary, "checks": results, "at": time.time()}
    with _cache_lock:
        _cache.update(at=time.time(), results=out)
    return out
