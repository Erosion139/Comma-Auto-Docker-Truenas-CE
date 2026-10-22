"""ffmpeg work: encoder detection, raw-HEVC concatenation, audio alignment, stacked render.

Output layout (per drive, H.264 MP4, 1280x1080):
    +-------------------------+
    |   road (fcamera)  1280x720  |
    +-------------+-----------+
    | wide 640x360 | driver 640x360 |
    +-------------+-----------+
"""
import os
import shutil
import subprocess

SEGMENT_SECS = 60  # openpilot segments are one minute each
OUT_W, OUT_H = 1280, 1080
TOP_H = 720
BOT_W, BOT_H = 640, 360


def _run(args, timeout=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


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


def resolve_encoder(setting: str) -> tuple[str, list[str]]:
    """Return (label, ffmpeg video-encoder args) for the chosen mode.

    auto  -> NVENC if the test encode works, otherwise CPU x264.
    nvenc -> NVENC only (caller should have checked it works).
    cpu   -> libx264.
    """
    cpu = ("cpu/libx264", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p"])
    nv = ("gpu/h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23",
                             "-b:v", "0", "-pix_fmt", "yuv420p"])
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


def build_audio(qcam_parts: list[str], dst: str) -> bool:
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
        "-c:a", "aac", "-b:a", "96k", dst,
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


def render_stacked(cams: dict, audio: str | None, out: str, fps: int, encoder_args: list[str]) -> tuple[bool, str]:
    """Stack road (top) over wide + driver (bottom) into one MP4.

    cams: {"road": path|None, "wide": path|None, "driver": path|None}
    Missing cameras become black panes so the layout never shifts.
    The output is cut to the shortest real camera so audio can't run past the video.
    """
    present = [p for p in cams.values() if p and os.path.exists(p)]
    if not present:
        return False, "no camera footage to render"
    dur = min(raw_duration(p, fps) for p in present)
    if dur <= 0:
        return False, "could not read footage length"

    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    labels = ["road", "wide", "driver"]
    idx = {}
    for i, lbl in enumerate(labels):
        p = cams.get(lbl)
        if p and os.path.exists(p):
            args += ["-framerate", str(fps), "-i", p]
        else:
            args += ["-f", "lavfi", "-i", f"color=c=black:s={OUT_W}x{TOP_H}:r={fps}"]
        idx[lbl] = i
    audio_idx = None
    if audio:
        args += ["-i", audio]
        audio_idx = len(labels)

    fc = (
        f"[0:v]scale={OUT_W}:{TOP_H}:force_original_aspect_ratio=decrease,"
        f"pad={OUT_W}:{TOP_H}:(ow-iw)/2:(oh-ih)/2,setsar=1[top];"
        f"[1:v]scale={BOT_W}:{BOT_H}:force_original_aspect_ratio=decrease,"
        f"pad={BOT_W}:{BOT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1[wl];"
        f"[2:v]scale={BOT_W}:{BOT_H}:force_original_aspect_ratio=decrease,"
        f"pad={BOT_W}:{BOT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1[wr];"
        f"[wl][wr]hstack=inputs=2[bot];[top][bot]vstack=inputs=2[v]"
    )
    args += ["-filter_complex", fc, "-map", "[v]"]
    if audio_idx is not None:
        args += ["-map", f"{audio_idx}:a:0", "-c:a", "copy"]
    args += encoder_args
    args += ["-t", f"{dur:.3f}", "-movflags", "+faststart", "-f", "mp4", out + ".part"]

    r = _run(args, timeout=60 * 60 * 6)
    part = out + ".part"
    if r.returncode != 0 or not os.path.exists(part) or os.path.getsize(part) == 0:
        if os.path.exists(part):
            os.remove(part)
        return False, (r.stderr.strip() or "ffmpeg failed")[-400:]
    os.replace(part, out)
    return True, "ok"


def probe_duration(path: str) -> float:
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "default=nk=1:nw=1", path])
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0
