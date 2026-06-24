# 🐳 Dockpatch

A lightweight self-hosted web app that monitors your Docker containers for image updates and can apply them on demand or on a schedule.

![UI](https://img.shields.io/badge/UI-web--based-blue) ![Arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64-lightgrey) ![Registry](https://img.shields.io/badge/registry-ghcr.io-blue)

## Features

- 🔍 **Check for updates** on demand (per container or all at once) or on a configurable schedule — uses registry digest comparison, no image pull required
- ⬆️ **Apply updates** on demand or automatically on a configurable hours interval
- 🔒 **Self-update protection** — dockpatch auto-detects itself and never updates its own container while running
- ⚙️ **Settings panel** — configure check interval, auto-update interval, and startup scan from the UI
- 🕐 **Maintenance window** — restrict auto-updates to a specific time range (supports overnight ranges, e.g. 22:00–04:00)
- 🔑 **Registry credentials** — add username/token pairs for private registries (Docker Hub, ghcr.io, self-hosted, etc.)
- 🕘 **Update history** — each card shows when a container was last updated and whether it succeeded
- ✅ **Confirmation dialog** — bulk "Update all" prompts for confirmation before applying changes
- 📱 **Responsive UI** — works great on desktop and mobile

## Quick start

```yaml
dockpatch:
  image: ghcr.io/claudeailab/dockpatch
  container_name: dockpatch
  hostname: dockpatch
  restart: unless-stopped
  user: "0"
  environment:
    TZ: ${TZ}
  ports:
    - 8093:8093
  volumes:
    - ./config/dockpatch:/data
    - /var/run/docker.sock:/var/run/docker.sock
```

```bash
docker compose up -d dockpatch
```

Then open **http://your-server:8093**

> **Note:** The Docker socket (`/var/run/docker.sock`) must be mounted — this is how dockpatch talks to Docker.
> All schedule settings are configured from the UI Settings panel and persisted across restarts.

## Self-update protection

Dockpatch automatically detects its own container at startup by matching its hostname to the running container list. It will never update itself while running. To update dockpatch, use the commands below.

## Registry credentials

To monitor or update containers from private registries, open the **🔑 Auth** panel and add a registry entry:

| Field    | Example                        |
|----------|--------------------------------|
| Registry | `ghcr.io`                      |
| Username | your GitHub username           |
| Token    | a PAT with `read:packages` scope |

> **Security note:** Credentials are stored in plain text inside your `/data` volume. Ensure the host path is appropriately secured.

## Maintenance window

In **Settings**, set a start and end time to restrict when auto-updates run (uses the server's `TZ` environment variable). Overnight ranges are supported — e.g. 22:00 to 04:00 will allow updates from 10 PM through 4 AM.

## Updating

```bash
docker compose pull dockpatch && docker compose up -d dockpatch
```
