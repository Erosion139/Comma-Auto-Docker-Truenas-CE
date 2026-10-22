# Comma Footage Sync

Pulls dashcam footage off a comma device over your local network and turns each drive
into one stacked H.264 MP4 (road on top, wide and driver below, mic audio included).
Runs as a Docker container, for example on TrueNAS, with a web UI on a port you choose.

Based on the method in [sourylime/comma-sync](https://github.com/sourylime/comma-sync):
SSH as the `comma` user, footage in `/data/media/0/realdata/`, one folder per segment.

## Install on TrueNAS (Apps)

TrueNAS pulls the app's image from GitHub's container registry (GHCR), so you install it
from the Apps screen by pasting a compose file. No command line is needed.

### One-time: publish the image from your repository

1. Push this code to your GitHub repository's `main` branch.
2. GitHub builds and publishes the image automatically (**Actions** tab → *Build and publish image*).
3. Make the image public so TrueNAS can pull it without a login:
   your profile → **Packages** → `comma-sync` → **Package settings** → **Change visibility** → **Public**.

To release a version, tag it: `git tag v1.0.0 && git push origin v1.0.0`. This publishes
`ghcr.io/<you>/comma-sync:1.0.0`, `:1.0`, and `:latest`.

### Install in the TrueNAS Apps screen

1. Open the TrueNAS web UI → **Apps** → **Discover Apps** → **⋮** (top right) → **Install via YAML**.
2. Name the app `comma-sync`.
3. Open [`truenas/docker-compose.yaml`](truenas/docker-compose.yaml), copy it, and paste it into the **Custom Config** box.
4. Edit the lines marked `<-- EDIT`:
   - the image (your GitHub user, lowercase),
   - the two host paths under `volumes:` (create the datasets first, for example `apps/comma-sync/data` and `comma-footage`),
   - the host port on the left of `ports:` if 8080 is taken,
   - your time zone.
5. Click **Save**. Open the web UI at `http://<truenas-ip>:<host-port>`.
6. Continue with **Setup** below (steps 2–5).

### Updating

To update, change the image tag in the app's config (for example to `:1.1.0`) and save.
If you keep `:latest`, redeploy the app after a new release so it pulls the new image.

### Notes

- The GPU block in the compose file needs an NVIDIA GPU passed through to Apps. Delete that block if you don't have one.
- Keep the `data` dataset separate from the output dataset. `data` holds the SSH key and the sync record.
- This targets the Docker Compose-based Apps screen (the **Install via YAML** option). Older Kubernetes-based SCALE releases work differently and aren't covered here. Check your release's Apps screen if the menu doesn't match.

## How it works

```
scan the comma ─► downloader ─► (queue) ─► encoder ─► MP4 in your output folder
                 one drive at a time        one drive at a time
                 (the next drive downloads while the previous one encodes)
```

## Setup (about 5 minutes)

### 1. Start the container

```bash
cp .env.example .env        # edit the values you need (or leave the defaults)
docker compose up -d --build
```

Edit `docker-compose.yml` and point the output volume at your dataset, for example
`/mnt/tank/comma-footage:/output`. Open the web UI at `http://<truenas-ip>:<WEBUI_PORT>`
(default `8080`).

### 2. Create the SSH key (automatic)

The app creates its own key the first time it starts. It is stored in `data/ssh/`, separate
from any keys you use on your own computers. Open the **SSH key** panel and you will see the
public key.

### 3. Register the key on GitHub

1. Click **Copy public key**.
2. Go to <https://github.com/settings/keys> → **New SSH key** → paste → save.
3. The comma trusts keys from a GitHub account, so note the username you used.

### 4. Enable SSH on the comma

On the comma: **Settings → Developer → enable SSH**, then enter **that GitHub username**.

### 5. Connect and sync

1. In **Settings**, enter the comma's IP address and your GitHub username, then click **Save settings**.
2. Click **Test comma connection**. Everything in **Setup checks** should turn green.
3. Click **Sync now**. Drives appear in the list as they download and encode.

The app also re-scans on its own every `SCAN_INTERVAL_MIN` minutes (default 60).

## Managing the SSH key

| Task | How |
|---|---|
| See the key and its status | SSH key panel |
| Check it is still registered | Setup checks → *Key registered on GitHub* |
| Replace the key | **Replace key** (then re-register the new public key on GitHub) |
| Back it up | Copy `data/ssh/id_ed25519` somewhere safe (it is the private key) |

Keep the private key file private: it lives in `data/ssh/` with 600 permissions.

## Setup checks

| Check | What it verifies |
|---|---|
| Comma address | An IP or hostname and a GitHub username are set |
| Output storage | The output folder is writable and has enough free space |
| ffmpeg | ffmpeg and ffprobe are installed |
| SSH key | The key pair is valid and matches |
| Key registered on GitHub | The public key is listed on your GitHub account |
| Comma SSH connection | The comma is reachable and accepts the key |
| Footage on comma | The `realdata` folder exists and has drives |
| GPU | An NVIDIA GPU is visible to the container |
| Video encoder | A real NVENC test encode succeeds (or CPU fallback is noted) |

## Live dashboard

- **GPU**: name, utilization, VRAM used/total, and temperature (from `nvidia-smi`).
- **Transfer**: overall progress for the drive being downloaded, plus progress for the current file.
- **Transfer speed**: download speed from the comma, in MB/s.
- **Network in / out**: total network throughput of the container's interfaces, in Mb/s.
- **Encode queue**: drives waiting for or currently in the encoder.

## Output

Each drive gets a folder named by its recording start time:

```
<output>/2026-10-22_14-03-11/2026-10-22_14-03-11__stacked.mp4
```

The video is H.264 at 1280×1080: the road camera across the top (1280×720) and the wide and
driver cameras side by side below (640×360 each). Audio comes from the comma's microphone,
aligned to each one-minute segment.

## Settings

Set these in the web UI, or in `.env` / `docker-compose.yml` as defaults.

| Setting | Default | Meaning |
|---|---|---|
| `WEBUI_PORT` | `8080` | Port for the web UI (restart the container after changing it) |
| `COMMA_HOST` | *(empty)* | Comma IP or hostname |
| `GITHUB_USER` | *(empty)* | GitHub account whose keys the comma trusts |
| `COMMA_PORT` / `COMMA_USER` | `22` / `comma` | SSH details |
| `OUTPUT_DIR` | `/output` | Where finished videos go (mount your dataset here) |
| `MIN_FREE_GB` | `20` | Warn when the output dataset has less free space than this |
| `SCAN_INTERVAL_MIN` | `60` | Minutes between automatic scans |
| `MIN_AGE_SECS` | `120` | Skip drives whose latest segment is newer than this (still recording) |
| `ENCODER` | `auto` | `auto` (GPU if it works), `nvenc` (GPU only), or `cpu` |
| `FPS` | `20` | Frame rate of the recorded footage |
| `CLEAN_RAW` | `0` | `1` = delete the downloaded raw chunks after a drive is encoded |

## GPU encoding on TrueNAS

- NVIDIA GPU encoding needs the GPU passed through to the container (the `deploy` section in
  `docker-compose.yml`) and the NVIDIA container runtime on the host.
- If the GPU is missing, encoding falls back to the CPU (`libx264`). Everything still works, just slower.

## Files

```
app/main.py        web server and API
app/pipeline.py    downloader + encoder workers and the queue between them
app/comma.py       SSH/SFTP access to the comma
app/sshkeys.py     the app's SSH key: create, check, GitHub registration
app/checks.py      setup checks
app/encoder.py     ffmpeg: encoder test, concatenation, audio, stacked render
app/monitor.py     GPU and network stats
app/static/        the web UI
Dockerfile         image definition
truenas/           compose file to paste into TrueNAS Apps
.github/workflows/ builds and publishes the image to GHCR
```

## Troubleshooting

- **"Comma rejected the key"**: the key is not on the GitHub account, or the username on the comma does not match. Re-check steps 3 and 4.
- **"Nothing listening at …"**: wrong IP, the comma is off or not on the same network, or SSH is off on the comma.
- **"Could not reach GitHub"**: the container has no internet access. Registration can't be verified, but the comma connection still works.
- **Downloads keep dropping**: the comma may reboot on weak power. The app retries automatically. A high-current USB-C supply helps.
- **Drive stuck in "queued"**: the encoder is busy with the previous drive. Check the log for errors.
