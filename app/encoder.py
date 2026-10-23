"""ffmpeg work: encoder/decoder detection, raw-HEVC concatenation, audio alignment, stacked render.

Two layouts (chosen by resolution_mode):

  native (default, 1:1 pixels)
      Every camera keeps its sensor resolution. Nothing is scaled. Road is centred on top,
      wide and driver sit side by side below. The canvas is sized to fit, e.g. 3272x2416
      for 1928x1208 + 1928x1208 + 1344x760 cameras.

  1080 (scaled)
      Fixed 1280x1080 canvas: road 1280x720 on top, wide and driver 640x360 below.
      Each camera is downscaled, so pixels are not 1:1.

Performance notes:
  * Decoding is the biggest CPU cost. With NVDEC (decoder=nvdec/auto) the HEVC streams are
    decoded on the GPU.
  * Encoding uses NVENC when available, otherwise libx264 with a fixed thread count.
  * ffmpeg runs under `nice`, so the web UI stays responsive while a render is busy.
"""
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

SEGMENT_SECS = 60  # openpilot segments are one minute each
OUT_W, OUT_H = 1280, 1080          # 1080 (scaled) layout
TOP_H = 720
BOT_W, BOT_H = 640, 360
LABELS = ("road", "wide", "driver")
# Used only to size a black placeholder when a camera file is missing.
DEFAULT_SIZES = {"road": (1928, 1208), "wide": (1928, 1208), "driver": (1344, 760)}

PRESET_X264 = {"speed": "veryfast", "balanced": "medium", "quality": "slow"}
PRESET_NVENC = {"speed": "p3", "balanced": "p5", "quality": "p7"}


@dataclass
class Quality:
    video_quality: int = 20        # CRF (CPU) / CQ (GPU): lower = better and larger
    preset: str = "balanced"       # speed | balanced | quality
    max_bitrate_mbps: float = 0.0  # 0 = no cap
    audio_kbps: int = 128

    def clamped(self) -> "Quality":
        preset = self.preset if self.preset in PRESET_X264 else "balanced"
        return Quality(
            video_quality=min(max(int(self.video_quality), 10), 35),
            preset=preset,
            max_bitrate_mbps=max(float(self.max_bitrate_mbps), 0.0),
            audio_kbps=min(max(int(self.audio_kbps), 64), 320),
        )


def _run(args, timeout=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _nice(args: list[str], level: int) -> list[str]:
    """Prefix a command with `nice` so encoding yields CPU to the web UI."""
    if level and level > 0 and shutil.which("nice"):
        return ["nice", "-n", str(level)] + args
    return args


def ffmpeg_status() -> tuple[bool, str]:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return False, "ffmpeg/ffprobe not found in the container."
    r = _run(["ffmpeg", "-hide_banner", "-version"])
    first = r.stdout.splitlines()[0] if r.stdout else "unknown version"
    return True, first


def nvenc_works() -> tuple[bool, str]:
    """Actually encode one frame on the GPU — presence of the codec name is not enough."""
    if not shutil.which("nvidia-smi"):
        return False, "No NVIDIA GPU runtime (nvidia-smi missing)."
    r = _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "testsrc=size=640x480:rate=20", "-frames:v", "5",
        "-c:v", "h264_nvenc", "-f", "null", "-",
    ], timeout=30)
    if r.returncode == 0:
        return True, "NVENC test encode succeeded on the GPU."
    return False, "NVENC test encode failed: " + (r.stderr.strip().splitlines() or ["unknown error"])[-1]


def nvdec_works() -> tuple[bool, str]:
    """Decode a real HEVC stream with NVDEC. Creates a tiny HEVC file on the GPU first."""
    if not shutil.which("nvidia-smi"):
        return False, "No NVIDIA GPU runtime (nvidia-smi missing)."
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "probe.hevc")
        make = _run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "testsrc=size=640x480:rate=20", "-frames:v", "10",
            "-c:v", "hevc_nvenc", "-f", "hevc", src,
        ], timeout=30)
        if make.returncode != 0 or not os.path.exists(src):
            return False, "Could not create a HEVC test stream on the GPU (hevc_nvenc unavailable)."
        dec = _run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-hwaccel", "cuda",
            "-framerate", "20", "-i", src, "-frames:v", "10", "-f", "null", "-",
        ], timeout=30)
        if dec.returncode == 0:
            return True, "NVDEC decode test succeeded on the GPU."
        return False, "NVDEC decode failed: " + (dec.stderr.strip().splitlines() or ["unknown error"])[-1]


def resolve_decoder(setting: str) -> tuple[bool, str]:
    """Return (use_nvdec, label). auto uses NVDEC when its test decode works."""
    if setting == "cpu":
        return False, "cpu"
    if setting == "nvdec":
        return True, "nvdec"
    ok, _ = nvdec_works()
    return (True, "nvdec") if ok else (False, "cpu")


def resolve_encoder(setting: str, threads: int = 2, quality: Quality | None = None) -> tuple[str, list[str]]:
    """Return (label, ffmpeg video-encoder args) for the chosen mode and quality.

    auto  -> NVENC if the test encode works, otherwise CPU x264.
    nvenc -> NVENC only (caller should have checked it works).
    cpu   -> libx264 with `threads` threads, so the CPU is left for the rest of the system.
    """
    q = (quality or Quality()).clamped()
    t = str(max(1, int(threads)))
    cap = []
    if q.max_bitrate_mbps > 0:
        m = f"{q.max_bitrate_mbps:g}"
        cap = ["-maxrate", f"{m}M", "-bufsize", f"{float(m) * 2:g}M"]

    cpu = ("cpu/libx264", ["-c:v", "libx264", "-preset", PRESET_X264[q.preset],
                           "-crf", str(q.video_quality), "-pix_fmt", "yuv420p",
                           "-threads", t] + cap)
    nv = ("gpu/h264_nvenc", ["-c:v", "h264_nvenc", "-preset", PRESET_NVENC[q.preset],
                             "-rc", "vbr", "-cq", str(q.video_quality), "-b:v", "0",
                             "-pix_fmt", "yuv420p"] + cap)
    if setting == "cpu":
        return cpu
    if setting == "nvenc":
        return nv
    ok, _ = nvenc_works()
    return nv if ok else cpu


def concat_raw(parts: list[str], dst: str) -> None:
    """Byte-concatenate raw HEVC segments (the same approach comma-sync uses)."""
    with open(dst, "wb") as out:
        for p in parts:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out, length=8 * 1024 * 1024)


def build_audio(qcam_parts: list[str], dst: str, kbps: int = 128) -> bool:
    """Mic audio: each qcamera.ts is clipped/padded to exactly one segment, then joined.

    Returns False when no segment has audio. Keeps audio length == video length.
    """
    pcm_all = dst + ".pcm"
    wrote = False
    with open(pcm_all, "wb") as out:
        for p in qcam_parts:
            tmp = p + ".pcm"
            ok = False
            if os.path.exists(p):
                r = _run([
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", p,
                    "-map", "0:a:0", "-af", "aresample=async=1:first_pts=0,apad",
                    "-t", str(SEGMENT_SECS), "-ar", "16000", "-ac", "1", "-f", "s16le", tmp,
                ])
                ok = r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0
            if ok:
                with open(tmp, "rb") as f:
                    shutil.copyfileobj(f, out)
                wrote = True
                os.remove(tmp)
            else:
                # silence keeps later segments aligned to the video
                out.write(b"\x00" * (16000 * 2 * SEGMENT_SECS))
    if not wrote:
        os.remove(pcm_all)
        return False
    r = _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", pcm_all,
        "-c:a", "aac", "-b:a", f"{int(kbps)}k", dst,
    ])
    os.remove(pcm_all)
    return r.returncode == 0 and os.path.exists(dst)


def raw_duration(path: str, fps: int) -> float:
    """Length of a raw HEVC file in seconds, from its frame count (no decode)."""
    r = _run(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
              "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path])
    try:
        return int(r.stdout.strip()) / fps
    except ValueError:
        return 0.0


def probe_size(path: str) -> tuple[int, int] | None:
    """Width and height of a raw HEVC stream, read from its headers (no decode)."""
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", path])
    try:
        w, h = r.stdout.strip().split("x")
        return int(w), int(h)
    except ValueError:
        return None


def _even(n: int) -> int:
    return n - (n % 2)


def plan_layout(cams: dict, resolution_mode: str) -> dict:
    """Work out the canvas and where each camera goes.

    native: every camera at its own resolution, positions on even pixel boundaries.
    1080:   the fixed scaled layout.
    """
    sizes = {}
    for lbl in LABELS:
        p = cams.get(lbl)
        sizes[lbl] = probe_size(p) if p and os.path.exists(p) else None
    for lbl in LABELS:
        if sizes[lbl] is None:
            sizes[lbl] = DEFAULT_SIZES[lbl]  # placeholder black pane at a sensible size

    if resolution_mode != "native":
        return {"mode": "1080", "canvas": (OUT_W, OUT_H), "sizes": sizes, "pos": None,
                "desc": f"scaled {OUT_W}x{OUT_H} (pixels resampled)"}

    rw, rh = sizes["road"]
    ww, wh = sizes["wide"]
    dw, dh = sizes["driver"]
    bottom_w = ww + dw
    bottom_h = max(wh, dh)
    W = _even(max(rw, bottom_w))
    H = _even(rh + bottom_h)
    pos = {
        "road": (_even((W - rw) // 2), 0),
        "wide": (_even((W - bottom_w) // 2), rh),
    }
    pos["driver"] = (pos["wide"][0] + ww, rh)
    return {"mode": "native", "canvas": (W, H), "sizes": sizes, "pos": pos,
            "desc": f"native {W}x{H} (1:1 pixels, no scaling)"}


def _build_render_args(cams, audio, out, fps, encoder_args, threads, hw, nice_level, dur, layout) -> list[str]:
    args = _nice(["ffmpeg"], nice_level)
    # warnings are shown only when NVDEC is requested, so a silent CPU fallback can be detected
    args += ["-y", "-hide_banner", "-loglevel", "warning" if hw else "error",
             "-filter_threads", str(max(1, threads)), "-threads", str(max(1, threads))]

    native = layout["mode"] == "native"
    for lbl in LABELS:
        p = cams.get(lbl)
        if p and os.path.exists(p):
            if hw:
                args += ["-hwaccel", "cuda"]          # NVDEC: decode HEVC on the GPU
            args += ["-framerate", str(fps), "-i", p]
        else:
            w, h = layout["sizes"][lbl]
            size = f"{w}x{h}" if native else f"{OUT_W}x{TOP_H}"
            args += ["-f", "lavfi", "-i", f"color=c=black:s={size}:r={fps}"]

    next_idx = 3
    if native:
        W, H = layout["canvas"]
        args += ["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={fps}"]
        bg = next_idx
        next_idx += 1
        pos = layout["pos"]
        # chain: canvas -> overlay road -> overlay wide -> overlay driver. No scaling anywhere.
        steps = [f"[{bg}:v]format=yuv420p[c0]"]
        for n, lbl in enumerate(LABELS):
            px, py = pos[lbl]
            out_label = "[v]" if n == len(LABELS) - 1 else f"[c{n + 1}]"
            steps.append(f"[c{n}][{n}:v]overlay={px}:{py}{out_label}")
        fc = ";".join(steps)
    else:
        fc = (
            f"[0:v]scale={OUT_W}:{TOP_H}:force_original_aspect_ratio=decrease,"
            f"pad={OUT_W}:{TOP_H}:(ow-iw)/2:(oh-ih)/2,setsar=1[top];"
            f"[1:v]scale={BOT_W}:{BOT_H}:force_original_aspect_ratio=decrease,"
            f"pad={BOT_W}:{BOT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1[wl];"
            f"[2:v]scale={BOT_W}:{BOT_H}:force_original_aspect_ratio=decrease,"
            f"pad={BOT_W}:{BOT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1[wr];"
            f"[wl][wr]hstack=inputs=2[bot];[top][bot]vstack=inputs=2[v]"
        )

    audio_idx = None
    if audio:
        args += ["-i", audio]
        audio_idx = next_idx

    args += ["-filter_complex", fc, "-map", "[v]"]
    if audio_idx is not None:
        args += ["-map", f"{audio_idx}:a:0", "-c:a", "copy"]
    args += encoder_args
    args += ["-t", f"{dur:.3f}", "-movflags", "+faststart", "-f", "mp4", out + ".part"]
    return args


def render_stacked(cams: dict, audio: str | None, out: str, fps: int, encoder_args: list[str],
                   layout: dict, threads: int = 2, hw_decode: bool = False, nice_level: int = 10
                   ) -> tuple[bool, str, str]:
    """Stack the three cameras into one MP4 using a layout from plan_layout().

    cams: {"road": path|None, "wide": path|None, "driver": path|None}
    Missing cameras become black panes so the layout never shifts.
    The output is cut to the shortest real camera so audio can't run past the video.

    Returns (ok, message, decode_used). If NVDEC decoding fails, the render is retried
    once with CPU decoding so a bad GPU setup never stops the sync.
    """
    present = [p for p in cams.values() if p and os.path.exists(p)]
    if not present:
        return False, "no camera footage to render", "none"
    dur = min(raw_duration(p, fps) for p in present)
    if dur <= 0:
        return False, "could not read footage length", "none"

    part = out + ".part"

    def attempt(hw: bool):
        args = _build_render_args(cams, audio, out, fps, encoder_args, threads, hw, nice_level, dur, layout)
        r = _run(args, timeout=60 * 60 * 6)
        if r.returncode == 0 and os.path.exists(part) and os.path.getsize(part) > 0:
            os.replace(part, out)
            # ffmpeg quietly decodes on the CPU when the GPU decoder can't start
            fell_back = hw and "cuda" in r.stderr.lower() and "fail" in r.stderr.lower()
            return True, "ok", fell_back
        if os.path.exists(part):
            os.remove(part)
        lines = [ln.strip() for ln in r.stderr.strip().splitlines() if ln.strip()]
        gpu = [ln for ln in lines if any(k in ln.lower() for k in ("cuda", "device", "hwaccel", "nvdec"))]
        pick = (gpu or lines or ["ffmpeg failed"])[0] if hw else (lines or ["ffmpeg failed"])[-1]
        return False, pick[:240], False

    ok, msg, fell_back = attempt(hw_decode)
    if ok and fell_back:
        return True, "NVDEC could not start for this render, so it decoded on the CPU.", "cpu"
    if ok:
        return True, msg, ("nvdec" if hw_decode else "cpu")
    if hw_decode:
        ok2, msg2, _ = attempt(False)
        if ok2:
            return True, "NVDEC failed (" + msg[:160] + "), decoded on the CPU instead.", "cpu"
        return False, msg2, "cpu"
    return False, msg, "cpu"


def probe_duration(path: str) -> float:
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "default=nk=1:nw=1", path])
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0
