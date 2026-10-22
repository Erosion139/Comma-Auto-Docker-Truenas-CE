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

## Output

Each drive gets a folder named by its recording start time:

```
<output>/2026-10-22_14-03-11/2026-10-22_14-03-11__stacked.mp4
```

The video is H.264 at 1280×1080: the road camera across the top (1280×720) and the wide and
driver cameras side by side below (640×360 each). Mic audio is aligned to each one-minute segment.

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
| `FPS` | `20` | Frame rate of the recorded footage |
| `CLEAN_RAW` | `0` | `1` = delete the downloaded raw chunks after a drive is encoded |

## Troubleshooting

- **Repository not found when installing**: the image is private. Make the package public (Part 1, step 3).
- **"Comma rejected the key"**: the key is not on the GitHub account, or the username on the comma does not match.
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
