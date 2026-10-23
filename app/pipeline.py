"""Download and encode as two separate workers joined by a queue.

  scheduler/manual -> scan device -> downloader thread --(encode_q)--> encoder thread
                       (one drive at a time)                            (one drive at a time)

The downloader moves on to the next drive as soon as one finishes, so a drive can be
encoding while the next one downloads. Each stage is sequential within itself.
"""
import collections
import datetime as dt
import json
import os
import queue
import shutil
import threading
import time

from . import comma, config, encoder, monitor, sshkeys

STATE_FILE = os.path.join(config.DATA_DIR, "state.json")
RAW_DIR = os.path.join(config.DATA_DIR, "raw")
WORK_DIR = os.path.join(config.DATA_DIR, "work")
ORDER = ["fcamera.hevc", "ecamera.hevc", "dcamera.hevc", "qcamera.ts"]


class Pipeline:
    def __init__(self):
        self.lock = threading.RLock()
        self.state = self._load_state()          # {"done": {route: {...}}, "failed": {route: err}}
        self.drives = {}                         # route -> display record
        self.log_lines = collections.deque(maxlen=300)
        self.encode_q = queue.Queue()
        self.trigger = threading.Event()
        self.stop_flag = threading.Event()
        self.cycle_running = False
        self.last_cycle = None
        self.last_error = ""
        self.encoder_label = ""
        self.encoding_route = None
        self.render = None                       # live state of the drive being encoded
        self.last_render = None                  # state of the most recent finished render
        self.batch = {"total": 0, "done": 0}     # drives queued for encoding in the current batch
        self.transfer = {"route": None, "file": "", "file_done": 0, "file_total": 0,
                         "drive_done": 0, "drive_total": 0}
        self._samples = collections.deque()      # (time, bytes) for speed
        self._client = None
        self._last_run = 0.0
        self._restore_from_state()

    # ----- state / logging -------------------------------------------------
    def _load_state(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    st = json.load(f)
                st.setdefault("done", {})
                st.setdefault("failed", {})
                return st
            except (OSError, ValueError):
                self.log("state file unreadable — starting a fresh ledger")
        return {"done": {}, "failed": {}}

    def _save_state(self):
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, STATE_FILE)

    def _restore_from_state(self):
        for route, info in self.state["done"].items():
            self.drives[route] = {"route": route, "stamp": info["stamp"], "state": "done",
                                  "error": "", "out": info.get("out", ""), "bytes": 0,
                                  "bytes_done": 0, "started_at": None}
        for route, err in self.state["failed"].items():
            if route in self.drives:
                continue
            self.drives[route] = {"route": route, "stamp": route, "state": "failed",
                                  "error": err, "out": "", "bytes": 0, "bytes_done": 0,
                                  "started_at": None}

    def log(self, msg):
        line = f"{dt.datetime.now().strftime('%H:%M:%S')}  {msg}"
        with self.lock:
            self.log_lines.append(line)
        print(line, flush=True)

    # ----- speed -----------------------------------------------------------
    def _add_bytes(self, n):
        now = time.time()
        with self.lock:
            self._samples.append((now, n))
            while self._samples and now - self._samples[0][0] > 5:
                self._samples.popleft()

    def _speed_bps(self):
        now = time.time()
        with self.lock:
            while self._samples and now - self._samples[0][0] > 5:
                self._samples.popleft()
            if not self._samples:
                return 0.0
            total = sum(n for _, n in self._samples)
            window = max(now - self._samples[0][0], 1.0)
            return total / window

    # ----- scheduling ------------------------------------------------------
    def request_sync(self):
        self.trigger.set()

    def stop(self):
        self.stop_flag.set()
        self.log("Stop requested — the current transfer will halt after the next chunk.")

    def scheduler_loop(self):
        while True:
            triggered = self.trigger.wait(timeout=30)
            if triggered:
                self.trigger.clear()
            interval = max(config.load().scan_interval_min, 1) * 60
            if triggered or time.time() - self._last_run >= interval:
                self._last_run = time.time()
                self.run_cycle()

    # ----- download side ---------------------------------------------------
    def run_cycle(self):
        if self.cycle_running:
            return
        self.stop_flag.clear()
        self.cycle_running = True
        self.last_error = ""
        try:
            pending = self._scan()
            for route, info in pending:
                if self.stop_flag.is_set():
                    self.log("Sync stopped by request.")
                    break
                self._download_route(route, info)
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            self.log(f"Sync cycle error: {e}")
        finally:
            self._close_client()
            self.cycle_running = False
            self.last_cycle = time.time()
            self.log("Download pass finished; encoder will finish any remaining queue.")

    def _scan(self):
        s = config.load()
        if not sshkeys.check_local()[0]:
            self.last_error = "SSH key not ready — fix it in the SSH key panel."
            self.log(self.last_error)
            return []
        self.log("Looking for the comma on the network…")
        try:
            client = comma.connect()
            try:
                drives = comma.list_drives(client)
            finally:
                client.close()
        except Exception as e:  # noqa: BLE001
            self.last_error = f"Scan failed: {e}"
            self.log(self.last_error)
            return []

        now = time.time()
        pending = []
        skipped_recording = 0
        for d in drives:
            route = d["route"]
            with self.lock:
                if route in self.state["done"]:
                    continue
            if now - d["mtime"] < s.min_age_secs:
                skipped_recording += 1
                continue
            start = d.get("start") or d["mtime"]
            stamp = dt.datetime.fromtimestamp(start).strftime("%Y-%m-%d_%H-%M-%S")
            with self.lock:
                self.drives[route] = {"route": route, "stamp": stamp, "state": "pending",
                                      "error": "", "out": "", "bytes": d["bytes"],
                                      "bytes_done": 0, "started_at": None}
            pending.append((route, d))
        if skipped_recording:
            self.log(f"Skipped {skipped_recording} drive(s) still being recorded.")
        self.log(f"{len(pending)} new drive(s) to download.")
        return pending

    def _connect(self):
        if self._client is None:
            self._client = comma.connect()
        return self._client

    def _close_client(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def _download_route(self, route, info):
        rec = self.drives[route]
        rec["state"] = "downloading"
        rec["started_at"] = time.time()
        rec["bytes"] = info["bytes"]
        with self.lock:
            self.transfer.update(route=route, file="", file_done=0, file_total=0,
                                 drive_done=0, drive_total=info["bytes"])
        self.log(f"Downloading {rec['stamp']} ({info['bytes'] / 1e6:.1f} MB)…")

        local_root = os.path.join(RAW_DIR, route)
        attempts = 0
        while True:
            attempts += 1
            try:
                self._fetch_segments(route, info, local_root, rec)
                break
            except comma.Stop:
                rec["state"] = "pending"
                self.log(f"Paused {rec['stamp']} — will resume on next sync.")
                return
            except Exception as e:  # noqa: BLE001 - reconnect on any transport error
                self._close_client()
                if attempts >= 4:
                    self._mark_failed(route, f"download failed after {attempts} attempts: {e}")
                    return
                self.log(f"Connection dropped ({e}); reconnecting ({attempts}/4)…")
                time.sleep(8)

        # download completed
        rec["state"] = "queued"
        self.log(f"Downloaded {rec['stamp']}; queued for encoding.")
        self._enqueue_encode(route)

    def _enqueue_encode(self, route):
        with self.lock:
            # start a fresh batch once the previous one has fully finished
            if self.batch["done"] >= self.batch["total"]:
                self.batch = {"total": 0, "done": 0}
            self.batch["total"] += 1
        self.encode_q.put(route)

    def _fetch_segments(self, route, info, local_root, rec):
        client = self._connect()
        sftp = client.open_sftp()
        try:
            for seg_no, segdir in info["segments"]:
                local_seg = os.path.join(local_root, segdir)
                os.makedirs(local_seg, exist_ok=True)
                files = info["file_map"].get(segdir, {})
                for fname in ORDER:
                    if fname not in files:
                        continue
                    remote = f"{comma.REALDATA}/{segdir}/{fname}"
                    size = files[fname]
                    final = os.path.join(local_seg, fname)
                    if os.path.exists(final) and os.path.getsize(final) == size:
                        self._progress_skip(rec, size)
                        continue
                    part = final + ".part"
                    with self.lock:
                        self.transfer.update(file=f"{segdir}/{fname}", file_done=0, file_total=size)
                    file_done = {"n": 0}

                    def progress(delta, _fd=file_done):
                        if self.stop_flag.is_set():
                            raise comma.Stop()
                        _fd["n"] += delta
                        rec["bytes_done"] += delta
                        self._add_bytes(delta)
                        with self.lock:
                            self.transfer["file_done"] = _fd["n"]
                            self.transfer["drive_done"] = rec["bytes_done"]
                    comma.download_file(sftp, remote, part, progress)
                    os.replace(part, final)
        finally:
            sftp.close()

    def _progress_skip(self, rec, size):
        rec["bytes_done"] += size
        with self.lock:
            self.transfer["drive_done"] = rec["bytes_done"]

    def _mark_failed(self, route, msg):
        rec = self.drives[route]
        rec["state"] = "failed"
        rec["error"] = msg
        with self.lock:
            self.state["failed"][route] = msg
            self._save_state()
        self.log(f"FAILED {rec['stamp']}: {msg}")

    # ----- encode side -----------------------------------------------------
    def encoder_loop(self):
        while True:
            route = self.encode_q.get()
            try:
                self._encode_route(route)
            except Exception as e:  # noqa: BLE001
                self._mark_failed(route, f"encode error: {e}")
            finally:
                with self.lock:
                    if self.render is not None:
                        self.render["ended"] = time.time()
                        self.render["finished"] = True
                        self.last_render = self.render
                        self.render = None
                    self.batch["done"] += 1
                self.encoding_route = None
                self.encode_q.task_done()

    def _encode_route(self, route):
        s = config.load()
        rec = self.drives[route]
        rec["state"] = "encoding"
        self.encoding_route = route
        stamp = rec["stamp"]
        local_root = os.path.join(RAW_DIR, route)
        work = os.path.join(WORK_DIR, route)
        os.makedirs(work, exist_ok=True)

        segdirs = sorted(
            (d for d in os.listdir(local_root) if d.startswith(route + "--")),
            key=lambda d: int(d.rsplit("--", 1)[1]),
        )
        if not segdirs:
            raise RuntimeError("no downloaded segments found")

        self.log(f"Encoding {stamp} ({len(segdirs)} segments)…")
        cams = {}
        for fname, label in (("fcamera.hevc", "road"), ("ecamera.hevc", "wide"), ("dcamera.hevc", "driver")):
            parts = [os.path.join(local_root, sd, fname) for sd in segdirs]
            parts = [p for p in parts if os.path.exists(p)]
            if not parts:
                cams[label] = None
                continue
            dst = os.path.join(work, f"{label}.hevc")
            encoder.concat_raw(parts, dst)
            cams[label] = dst

        audio = None
        qparts = [os.path.join(local_root, sd, "qcamera.ts") for sd in segdirs]
        audio_path = os.path.join(work, "audio.m4a")
        if encoder.build_audio(qparts, audio_path, kbps=max(64, min(int(s.audio_kbps), 320))):
            audio = audio_path
        else:
            self.log(f"No mic audio for {stamp}; rendering video only.")

        quality = encoder.Quality(
            video_quality=s.video_quality, preset=s.encode_preset,
            max_bitrate_mbps=s.max_bitrate_mbps, audio_kbps=s.audio_kbps,
        ).clamped()
        layout = encoder.plan_layout(cams, s.resolution_mode)
        label, enc_args = encoder.resolve_encoder(s.encoder, threads=s.encode_threads, quality=quality)
        use_nvdec, _ = encoder.resolve_decoder(s.decoder)
        self.encoder_label = label
        with self.lock:
            self.render = {
                "route": route, "stamp": stamp, "started": time.time(), "threads": max(1, s.encode_threads),
                "pct": 0.0, "out_sec": 0.0, "dur": 0.0, "fps": None, "speed": None, "eta": None,
                "cpu_pct": None, "cpu_sum": 0.0, "cpu_n": 0, "cpu_peak": 0.0,
                "encoder": label, "decoder": "nvdec" if use_nvdec else "cpu",
                "decode_fell_back": False, "layout": layout["desc"],
                "gpu_seen": False, "gpu_enc_now": None, "gpu_dec_now": None,
                "gpu_enc_peak": 0.0, "gpu_dec_peak": 0.0, "finished": False, "ended": None,
            }
        self.log(f"Layout: {layout['desc']}.")
        cap = f", cap {quality.max_bitrate_mbps:g} Mb/s" if quality.max_bitrate_mbps > 0 else ""
        self.log(f"Rendering with {label}, decode {'nvdec' if use_nvdec else 'cpu'}, "
                 f"quality {quality.video_quality} ({quality.preset}){cap}, "
                 f"{s.encode_threads} CPU thread(s), priority nice {s.encode_nice}.")

        out_dir = os.path.join(s.output_dir, stamp)
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, f"{stamp}__stacked.mp4")
        ok, msg, decode_used = encoder.render_stacked(
            cams, audio, out, s.fps, enc_args, layout,
            threads=s.encode_threads, hw_decode=use_nvdec, nice_level=s.encode_nice,
            on_progress=self._on_render_progress,
        )
        shutil.rmtree(work, ignore_errors=True)
        self._render_summary(stamp, ok)

        if not ok:
            self._mark_failed(route, f"render failed: {msg}")
            return
        if msg != "ok":
            self.log(f"Note for {stamp}: {msg}")
        self.log(f"Decoded with {decode_used}.")

        rec["state"] = "done"
        rec["out"] = out
        with self.lock:
            self.state["done"][route] = {"stamp": stamp, "out": out, "encoder": label}
            self.state["failed"].pop(route, None)
            self._save_state()
        self.log(f"Done {stamp} → {out} [{label}]")
        if s.delete_raw_after_encode:
            shutil.rmtree(local_root, ignore_errors=True)
            self.log(f"Removed raw chunks for {stamp}.")

    def _on_render_progress(self, snap):
        """Called by the encoder about once a second while ffmpeg runs."""
        gpu = monitor.gpu_snapshot()
        gpus = (gpu.get("gpus") or []) if gpu.get("available") else []
        enc = max((g["enc_pct"] for g in gpus if g.get("enc_pct") is not None), default=None)
        dec = max((g["dec_pct"] for g in gpus if g.get("dec_pct") is not None), default=None)
        with self.lock:
            r = self.render
            if r is None:
                return
            if "event" in snap:
                if snap["event"] == "decode_fallback":
                    r["decode_fell_back"] = True
                    r["decoder"] = "cpu"
                return
            r.update(pct=snap["pct"], out_sec=snap["out_sec"] or 0.0, dur=snap["dur"],
                     fps=snap["fps"], speed=snap["speed"], eta=snap["eta"])
            cpu = snap.get("cpu_pct")
            if cpu is not None:
                r["cpu_pct"] = cpu
                r["cpu_sum"] += cpu
                r["cpu_n"] += 1
                r["cpu_peak"] = max(r["cpu_peak"], cpu)
            if gpus:
                r["gpu_seen"] = True
                r["gpu_enc_now"] = enc
                r["gpu_dec_now"] = dec
                if enc is not None:
                    r["gpu_enc_peak"] = max(r["gpu_enc_peak"], enc)
                if dec is not None:
                    r["gpu_dec_peak"] = max(r["gpu_dec_peak"], dec)

    def _render_summary(self, stamp, ok):
        with self.lock:
            r = dict(self.render) if self.render else None
        if not r:
            return
        elapsed = max(time.time() - r["started"], 1e-6)
        parts = [f"encoder {r['encoder']}",
                 f"decoder {r['decoder']}" + (" (NVDEC failed, CPU fallback)" if r["decode_fell_back"] else "")]
        if r["gpu_seen"]:
            parts.append(f"GPU encoder engine peak {r['gpu_enc_peak']:.0f}%")
            parts.append(f"GPU decoder engine peak {r['gpu_dec_peak']:.0f}%")
        if r["cpu_n"]:
            parts.append(f"ffmpeg CPU avg {r['cpu_sum'] / r['cpu_n']:.0f}% (peak {r['cpu_peak']:.0f}%)")
        if r["dur"]:
            parts.append(f"{r['dur'] / elapsed:.2f}× realtime")
        self.log(f"Render {stamp} {'done' if ok else 'failed'} in {elapsed / 60:.1f} min: " + "; ".join(parts))

    # ----- snapshot for the UI --------------------------------------------
    def snapshot(self):
        with self.lock:
            drives = []
            for r in sorted(self.drives.values(), key=lambda x: x["stamp"], reverse=True):
                pct = (r["bytes_done"] / r["bytes"] * 100) if r["bytes"] else 0.0
                if r["state"] == "encoding" and self.render and self.render["route"] == r["route"]:
                    pct = self.render["pct"]  # encoding progress, not download progress
                drives.append({
                    "route": r["route"], "stamp": r["stamp"], "state": r["state"],
                    "error": r["error"], "bytes": r["bytes"], "bytes_done": r["bytes_done"],
                    "pct": round(min(pct, 100.0), 1), "out": r["out"],
                })
            t = dict(self.transfer)
            logs = list(self.log_lines)[-80:]
            render = dict(self.render) if self.render else None
            last = dict(self.last_render) if self.last_render else None
            batch = dict(self.batch)
        t["speed_bps"] = self._speed_bps()
        return {
            "cycle_running": self.cycle_running,
            "last_cycle": self.last_cycle,
            "last_error": self.last_error,
            "encoding_route": self.encoding_route,
            "encode_queue": self.encode_q.qsize(),
            "encoder": self.encoder_label,
            "transfer": t,
            "drives": drives,
            "log": logs,
            "render": _render_view(render),
            "last_render": _render_view(last),
            "batch": {"total": batch["total"], "done": batch["done"],
                      "waiting": max(self.encode_q.qsize(), 0)},
        }


# ----- plain-language markers for the encoding card ------------------------------
def _mk(label, value, status, detail):
    return {"label": label, "value": value, "status": status, "detail": detail}


def _fmt_clock(sec):
    if sec is None:
        return "—"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _engine_marker(label, name, peak, r, elapsed):
    """Whether a GPU engine (NVENC or NVDEC) has actually been busy during this render."""
    if peak > 0:
        return _mk(label, name, "ok", f"{label} engine active, peak {peak:.0f}% during this render.")
    if r["gpu_seen"] and elapsed > 15:
        return _mk(label, name, "warn", f"Selected, but the GPU {label.lower()} engine has shown no activity.")
    if not r["gpu_seen"] and elapsed > 15:
        return _mk(label, name, "info", "No GPU telemetry was reported, so activity can't be confirmed.")
    return _mk(label, name, "info", "Waiting for the first GPU reading…")


def _render_view(r):
    """Build the encoding card's state: progress, speed, CPU, and hardware markers."""
    if not r:
        return None
    finished = bool(r.get("finished"))
    now = r["ended"] if finished and r.get("ended") else time.time()
    elapsed = now - r["started"]
    enc, dec, fell = r["encoder"], r["decoder"], r["decode_fell_back"]
    markers = []

    # Encoder
    if enc.startswith("gpu"):
        markers.append(_engine_marker("Encoder", "NVENC (GPU)", r["gpu_enc_peak"], r, elapsed))
    else:
        markers.append(_mk("Encoder", "CPU (libx264)", "info",
                           "Video is encoded on the CPU, so that is where most of the load comes from."))

    # Decoder
    if fell:
        markers.append(_mk("Decoder", "CPU (NVDEC failed)", "warn",
                           "NVDEC could not start for this render, so the HEVC footage was decoded on the CPU."))
    elif dec == "nvdec":
        markers.append(_engine_marker("Decoder", "NVDEC (GPU)", r["gpu_dec_peak"], r, elapsed))
    else:
        markers.append(_mk("Decoder", "CPU", "info", "HEVC footage is decoded on the CPU."))

    # CPU: explain where the load is coming from
    cpu = r["cpu_pct"]
    if cpu is not None:
        threads = r["threads"]
        cap = threads * 100
        cpu_enc = not enc.startswith("gpu")
        cpu_dec = dec != "nvdec" or fell
        if cpu_enc and cpu_dec:
            why = "Both encoding and decoding run on the CPU, which accounts for the load."
        elif cpu_enc:
            why = "Encoding runs on the CPU; decoding is on the GPU."
        elif cpu_dec:
            why = "Decoding runs on the CPU; encoding is on the GPU."
        else:
            why = ("Encoding and decoding are both on the GPU, so the CPU should be mostly idle. "
                   "If it isn't, check the GPU markers above.")
        at_cap = cpu_enc and cpu >= 0.8 * cap
        unexpected = not cpu_enc and not cpu_dec and cpu > 100
        status = "warn" if (at_cap or unexpected) else "ok"
        cap_note = " At the thread limit; raise CPU threads for faster renders or lower the limit to keep the UI responsive." if at_cap else ""
        markers.append(_mk("ffmpeg CPU", f"{cpu:.0f}%", status,
                           f"About {cpu / 100:.1f} of {threads} allowed core(s). {why}{cap_note}"))

    # Speed
    if r["speed"] is not None:
        sp = r["speed"]
        detail = f"Footage {_fmt_clock(r['out_sec'])} of {_fmt_clock(r['dur'])}"
        if r["eta"] is not None and not finished:
            detail += f" · ETA {_fmt_clock(r['eta'])}"
        if r["fps"]:
            detail += f" · {r['fps']:.0f} fps"
        if sp < 1:
            detail += ". Slower than realtime, so the queue will fall behind."
        markers.append(_mk("Speed", f"{sp:.2f}× realtime", "ok" if sp >= 1 else "warn", detail))

    return {
        "stamp": r["stamp"], "route": r["route"], "finished": finished,
        "pct": r["pct"], "out_sec": r["out_sec"], "dur": r["dur"],
        "eta": None if finished else r["eta"], "speed": r["speed"], "fps": r["fps"],
        "elapsed": elapsed, "encoder": enc, "decoder": dec, "layout": r["layout"],
        "markers": markers,
    }


pipeline = Pipeline()
