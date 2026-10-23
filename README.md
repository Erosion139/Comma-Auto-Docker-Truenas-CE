# Comma Footage Sync

Pulls dashcam footage off a comma device over your local network and turns each drive
into one stacked H.264 MP4 (road on top, wide and driver below, mic audio included).
Runs as a Docker container with a web UI on a port you choose. Built for TrueNAS Apps.

Based on the method in [sourylime/comma-sync](https://github.com/sourylime/comma-sync):
SSH as the `comma` user, footage in `/data/media/0/realdata/`, one folder per segment.

---

## Part 1 — Publish the image (one time, on GitHub)

TrueNAS installs apps from a Docker image. GitHub builds that image for you.

1. Push this code to your repository's `main` branch.
2. Open the **Actions** tab. The *Build and publish image* workflow runs and publishes the image to `ghcr.io/<your-user>/<repo>`.
3. Make the image public so TrueNAS can pull it without a login:
   your profile → **Packages** → select the package → **Package settings** → **Change visibility** → **Public**.
4. To make a versioned release, tag it: `git tag v1.0.0 && git push origin v1.0.0`.
   This publishes `1.0.0`, `1.0`, and `latest`.

Your image name is `ghcr.io/<your-user>/<repo>` in lowercase. Write it down for Part 2.

---

## Part 2 — Install in TrueNAS (guided Custom App wizard)

1. In the TrueNAS web UI, go to **Apps** → **Discover Apps** → **Custom App**.

2. **Application Name:** `comma-sync`

3. **Image Configuration**
   - **Repository:** `ghcr.io/<your-user>/<repo>`
   - **Tag:** `latest` (or a version such as `1.0.0`)
   - **Pull Policy:** *Pull if not present*

4. **Container Configuration**
   - **Timezone:** your time zone (for example `America/New_York`)
   - **Restart Policy:** *Unless Stopped*
   - **Environment Variables:** add each of these as a Name and Value pair:

     | Name | Value |
     |---|---|
     | `DATA_DIR` | `/data` |
     | `OUTPUT_DIR` | `/output` |
     | `WEBUI_PORT` | `8080` |
     | `ENCODER` | `auto` |
     | `SCAN_INTERVAL_MIN` | `60` |
     | `MIN_FREE_GB` | `20` |
     | `NVIDIA_VISIBLE_DEVICES` | `all` |
     | `NVIDIA_DRIVER_CAPABILITIES` | `compute,utility,video` |

   The comma address and GitHub username are not needed here. You enter them later in the app's Settings page, so the install stays simple.

5. **Network Configuration → Host Network: on.** This is required. The app finds the comma
   on your LAN, which needs the same network view as your NAS. With Host Network on, the
   port mapping is disabled and the web UI is served directly on port `8080` of the NAS.
   (If you can't use Host Network, see *Finding the comma without Host Network* below.)

6. **Portal Configuration** — click **Add** so the app shows an **Open** button in TrueNAS:
   - **Name:** `Comma Sync`
   - **Protocol:** HTTP
   - **Use Node IP:** on
   - **Port:** `8080` (must match `WEBUI_PORT`)
   - **Path:** `/`

7. **Storage Configuration** — add two volumes, each with **Type: Host Path**:

   | Host Path (create these datasets first) | Mount Path | Purpose |
   |---|---|---|
   | `/mnt/<pool>/apps/comma-sync/data` | `/data` | App settings, SSH key, sync record, raw chunks |
   | `/mnt/<pool>/comma-footage` | `/output` | Finished videos (the dataset you want the MP4s in) |

   Keep **Read Only** off for both. The container runs as root, so leave **Custom User** off.

8. **Resources Configuration → GPU Configuration** — if the GPU list appears, select your NVIDIA GPU and tick **Use this GPU**. If you have no NVIDIA GPU, skip this step and the app falls back to the CPU.

9. Click **Save**. Wait for the app to show **Running**, then click **Open** (or go to `http://<truenas-ip>:8080`).

10. Continue with **Part 3** to connect your comma.

### Updating

Edit the app and change **Tag** (for example from `1.0.0` to `1.1.0`), then save. If you use `latest`, edit the app and save or redeploy to pull the newest image.

### Settings you can change later

Your data stays in the app's data folder and the output dataset, so you can edit the app's configuration without losing anything. The comma address, GitHub username, encoder mode, and scan schedule are set in the web UI's **Settings** page.

---

## Part 3 — Connect your comma (in the web UI)

### a. Register the app's SSH key

The app creates its own key on first start. You don't need to make one.

1. In the web UI, open the **SSH key** panel and click **Copy public key**.
2. Go to <https://github.com/settings/keys> → **New SSH key** → paste → save.
3. Remember the GitHub username you used.

### b. Enable SSH on the comma

On the comma: **Settings → Developer → enable SSH**, then enter **that GitHub username**.

### c. Connect and sync

1. In **Settings**, enter your GitHub username and click **Save settings**. You don't need the comma's IP address.
2. Click **Test comma connection**. The app searches your network, finds the comma, and confirms the key works. Everything in **Setup checks** should turn green.
3. Click **Sync now**. Drives appear as they download and encode.

The comma gets a new IP address on most connects, so the app finds it each time:

1. It tries the address that worked last time.
2. If that fails, it looks for the comma over mDNS (the comma advertises its SSH service).
3. If that fails, it scans the local network for SSH hosts.

Each candidate must accept the app's key and have openpilot installed. A different SSH server on the network is never mistaken for the comma. The app searches again on every scan, so the comma is picked up when it reconnects on a new address. **Find comma now** in Settings runs the search immediately.

### Finding the comma without Host Network

Automatic discovery needs the app to see your LAN. If you must keep Host Network off:

- Set **Networks to search** in Settings to your home subnet, for example `192.168.1.0/24`.
- mDNS discovery won't work from a bridged network, so only the subnet scan is used. That still finds the comma, but it takes a few seconds longer.

The app scans on its own every `SCAN_INTERVAL_MIN` minutes (default 60).

---

## Managing the SSH key

| Task | How |
|---|---|
| See the key and its status | SSH key panel |
| Check it is still registered | Setup checks → *Key registered on GitHub* |
| Replace the key | **Replace key** (then add the new public key on GitHub) |
| Back it up | Copy `data/ssh/id_ed25519` somewhere safe. It is the private key. |

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
- **Encoding card**: a progress bar for the drive being encoded (footage done, speed as a multiple of
  realtime, and ETA), a queue bar for the current batch (encoded out of total), and hardware markers.
- **GPU card**: also shows the NVENC (encoder) and NVDEC (decoder) engine load when the driver reports it.

### Reading the hardware markers

Each marker is one plain-language check on the current render. It is shown live while a drive
encodes, and the last finished render stays visible until the next one starts.

| Marker | Green means | Amber means |
|---|---|---|
| **Encoder** | NVENC engine has been busy during this render | NVENC was selected, but the encoder engine stayed idle |
| **Decoder** | NVDEC engine has been busy during this render | NVDEC was selected but idle, or it failed and fell back to CPU |
| **ffmpeg CPU** | Load is within the thread limit | At 80% or more of the thread limit, or high CPU with both stages on the GPU |
| **Speed** | At least 1× realtime (the queue keeps up) | Slower than realtime, so the queue will fall behind |

A high **ffmpeg CPU** reading is expected when encoding or decoding runs on the CPU. The marker names
which stage that is. If the GPU markers are green and CPU load is still high, that needs a closer look.

The same numbers are written to the log after each render: encoder, decoder, GPU engine peaks, average
and peak CPU, and speed.

## Output

Each drive gets a folder named by its recording start time:

```
<output>/2026-10-22_14-03-11/2026-10-22_14-03-11__stacked.mp4
```

Each drive is written as one or more H.264 MP4 files, one per camera combination you tick (see
*Videos per drive*). By default you get a single file: the three-camera stack, with the road camera
across the top and the wide and driver cameras side by side below. The default layout keeps every
camera at its full sensor resolution (see *Output quality*). Mic audio is aligned to each one-minute
segment in every file.

### Videos per drive

The **Videos per drive** panel, to the right of Settings, lists every combination of the three
cameras: road, wide, and driver on their own; road + wide, road + driver, and wide + driver; and
all three stacked. Tick any of them. Changes save straight away.

Each ticked video is made for every drive, in the drive's folder, with the combination in the file name:

```
<output>/2026-10-22_14-03-11/2026-10-22_14-03-11__stacked.mp4
<output>/2026-10-22_14-03-11/2026-10-22_14-03-11__road.mp4
<output>/2026-10-22_14-03-11/2026-10-22_14-03-11__road-wide.mp4
```

Each video is a separate encode of the same footage, so encoding time adds up. The drive's progress
bar covers all of its videos in order. Pair a full-resolution archive stack with a smaller single-camera
video on the **Scaled to 1080p** layout if you want something quick to share.

## Output quality

Set these in **Settings → Output quality**, or as environment variables for defaults.

| Setting | Default | Meaning |
|---|---|---|
| **Layout** (`RESOLUTION_MODE`) | `native` | `native` keeps each camera at its own resolution with no scaling, so every pixel maps 1:1. With the current cameras the canvas is **3272×2416**. `1080` scales everything to fit a **1280×1080** frame (smaller and faster, but pixels are resampled). The three-camera stack keeps its fixed 1280×1080 layout, and the other combinations are scaled to fit the same frame and keep their shape. |
| **Video quality** (`VIDEO_QUALITY`) | `20` | CRF (CPU) or CQ (GPU) on a 10–35 scale. Lower is sharper and larger. About 18–22 is visually transparent for most footage. Each +6 roughly halves the file size. |
| **Encode preset** (`ENCODE_PRESET`) | `balanced` | `speed`, `balanced`, or `quality`. Slower presets give a smaller file at the same quality, but take more CPU or GPU time. |
| **Max bitrate** (`MAX_BITRATE_MBPS`) | `0` | `0` means no cap. Set a value such as `6` in Mb/s to keep file sizes predictable. |
| **Audio bitrate** (`AUDIO_KBPS`) | `128` | Bitrate of the mic audio, 64–320 kb/s. |

Trade-offs to keep in mind:

- **Native layout is much heavier.** A 3272×2416 frame has about 4.5× the pixels of the 1280×1080
  layout, so it needs more storage, more encode time, and more memory for the decoder. The file
  for one drive is several times larger. With NVENC and NVDEC working, native is comfortable. On
  CPU-only encoding, use `speed` or `1080` if renders take too long.
- Native mode keeps the original detail, so each camera stays as crisp as the recording. That is
  the right choice if you want to review footage closely or crop into it later.

## Settings reference

Set these in the web UI, or as environment variables for defaults.

| Setting | Default | Meaning |
|---|---|---|
| `WEBUI_PORT` | `8080` | Port the web UI listens on inside the container |
| `COMMA_HOST` | *(empty)* | Optional first guess at the comma's address. Leave blank to auto-find. |
| `SCAN_SUBNETS` | *(auto)* | Networks to search, for example `192.168.1.0/24`. Set this if Host Network is off. |
| `GITHUB_USER` | *(empty)* | GitHub account whose keys the comma trusts (set in the UI) |
| `COMMA_PORT` / `COMMA_USER` | `22` / `comma` | SSH details |
| `OUTPUT_DIR` | `/output` | Where finished videos go |
| `MIN_FREE_GB` | `20` | Warn when the output dataset has less free space than this |
| `SCAN_INTERVAL_MIN` | `60` | Minutes between automatic scans |
| `MIN_AGE_SECS` | `120` | Skip drives whose latest segment is newer than this (still recording) |
| `ENCODER` | `auto` | `auto` (GPU if it works), `nvenc` (GPU only), or `cpu` |
| `DECODER` | `auto` | `auto` (decode on the GPU with NVDEC if it works), `nvdec`, or `cpu` |
| `ENCODE_THREADS` | `2` | CPU threads the encoder may use. Lower this if the UI becomes slow. |
| `ENCODE_NICE` | `10` | Scheduling priority of encoding (0 = normal, up to 19). Higher keeps the UI responsive. |
| `FPS` | `20` | Frame rate of the recorded footage |
| `OUTPUTS` | `road+wide+driver` | Videos to make for each drive, as comma-separated camera combinations, for example `road+wide+driver,road`. Set in the **Videos per drive** panel. |
| `RESOLUTION_MODE` | `native` | `native` (1:1 camera pixels) or `1080` (scaled to fit 1280×1080). See *Output quality*. |
| `VIDEO_QUALITY` | `20` | CRF/CQ, 10–35. Lower is better and larger. |
| `ENCODE_PRESET` | `balanced` | `speed`, `balanced`, or `quality` |
| `MAX_BITRATE_MBPS` | `0` | Cap on video bitrate in Mb/s. `0` = no cap. |
| `AUDIO_KBPS` | `128` | Mic audio bitrate in kb/s (64–320) |
| `CLEAN_RAW` | `0` | `1` = delete the downloaded raw chunks after a drive is encoded |

## Performance on a modest CPU

Rendering is the heavy part. Decoding three HEVC cameras and encoding H.264 can use every core
the app is allowed to use, which makes the web UI slow to respond. Three settings control this:

1. **Decoder: auto (NVDEC).** With an NVIDIA GPU, the HEVC camera streams are decoded on the
   GPU. This removes most of the CPU load. It needs the GPU passed through with the **video**
   capability (the install steps already set `NVIDIA_DRIVER_CAPABILITIES` to `compute,utility,video`).
   The Setup checks show **Video decoder** as green when NVDEC works.
2. **CPU threads: 2 (default).** The encoder uses at most this many cores at a time. Set it to
   `1` on a slow CPU. Renders take longer, but the rest of the system stays responsive.
3. **Encoder priority: 10 (default).** Encoding runs at lower priority than the web UI, so the
   UI stays responsive even during a render.

You can also cap the whole container in TrueNAS: in the Custom App wizard, use
**Resources Configuration → Enable Resource Limits → CPUs** (for example `2`). That is a hard limit on
everything the app does, including downloads. Use it as a backstop if the settings above aren't enough.

With NVENC and NVDEC both working, most of the work moves to the GPU. Check the log after a render:
it shows which encoder and decoder were used, and says when NVDEC could not start and the app fell back to the CPU.

## Troubleshooting

- **Repository not found when installing**: the image is private. Make the package public (Part 1, step 3).
- **"Found SSH at … but it rejected the app's key"**: the comma is found, but it doesn't trust this key. Either add the app's public key to your GitHub account (Part 3a), or use **Use an existing key** in the SSH key panel to import the key Comma Sync already uses. After adding the key, re-enter your GitHub username on the comma so it reloads your keys.
- **"No comma found"**: the comma is off, on another network, or SSH is off on it. If Host Network is off, set **Networks to search**. Then click **Find comma now**.
- **"Could not reach GitHub"**: the container has no internet access. Registration can't be verified, but the comma connection still works.
- **Downloads keep dropping**: the comma may reboot on weak power. The app retries automatically. A high-current USB-C supply helps.
- **Drive stuck in "queued"**: the encoder is busy with the previous drive. Check the log for errors.

---

## Run locally (optional, for testing)

```bash
cp .env.example .env
docker compose up -d --build
```

Then open `http://localhost:8080`.

## Files

```
app/main.py          web server and API
app/pipeline.py      downloader and encoder workers, with the queue between them
app/comma.py         SSH/SFTP access to the comma
app/discovery.py     finds the comma on the network (last address, mDNS, subnet scan)
app/sshkeys.py       the app's SSH key: create, check, GitHub registration
app/checks.py        setup checks
app/encoder.py       ffmpeg: encoder test, concatenation, audio, stacked render
app/monitor.py       GPU and network stats
app/static/          the web UI
Dockerfile           image definition
.github/workflows/   builds and publishes the image to GHCR
```

## License

Add a `LICENSE` file for your repository. The upstream comma-sync project is MIT-licensed.
