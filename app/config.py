"""Settings: environment defaults, overridden by a JSON file saved from the web UI."""
import json
import os
import threading
from dataclasses import dataclass, asdict, fields

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")


@dataclass
class Settings:
    comma_host: str = os.environ.get("COMMA_HOST", "")          # IP or hostname of the comma
    comma_port: int = int(os.environ.get("COMMA_PORT", "22"))
    comma_user: str = os.environ.get("COMMA_USER", "comma")
    github_user: str = os.environ.get("GITHUB_USER", "")        # account whose keys the comma trusts
    output_dir: str = os.environ.get("OUTPUT_DIR", "/output")   # TrueNAS dataset mount
    min_free_gb: float = float(os.environ.get("MIN_FREE_GB", "20"))
    scan_interval_min: int = int(os.environ.get("SCAN_INTERVAL_MIN", "60"))
    min_age_secs: int = int(os.environ.get("MIN_AGE_SECS", "120"))  # skip drives still recording
    encoder: str = os.environ.get("ENCODER", "auto")            # auto | nvenc | cpu
    fps: int = int(os.environ.get("FPS", "20"))
    delete_raw_after_encode: bool = os.environ.get("CLEAN_RAW", "0") == "1"
    webui_port: int = int(os.environ.get("WEBUI_PORT", "8080"))


_lock = threading.Lock()
_settings = None


def load() -> Settings:
    global _settings
    with _lock:
        if _settings is None:
            s = Settings()
            if os.path.exists(CONFIG_FILE):
                try:
                    with open(CONFIG_FILE) as f:
                        saved = json.load(f)
                    known = {f.name for f in fields(Settings)}
                    for k, v in saved.items():
                        if k in known:
                            setattr(s, k, v)
                except (OSError, ValueError):
                    pass  # corrupt file: fall back to env defaults
            _settings = s
        return _settings


def update(changes: dict) -> Settings:
    s = load()
    known = {f.name: f.type for f in fields(Settings)}
    with _lock:
        for k, v in changes.items():
            if k not in known:
                continue
            t = known[k]
            if t in ("int", int):
                v = int(v)
            elif t in ("float", float):
                v = float(v)
            elif t in ("bool", bool):
                v = bool(v)
            else:
                v = str(v).strip()
            setattr(s, k, v)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(asdict(s), f, indent=2)
    return s
