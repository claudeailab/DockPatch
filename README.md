# 🐳 Dockpatch

A lightweight self-hosted web app that monitors your Docker containers for image updates and can apply them on demand or on a schedule.

![UI](https://img.shields.io/badge/UI-web--based-blue) ![Arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64-lightgrey) ![Registry](https://img.shields.io/badge/registry-ghcr.io-blue)

## Features

- 🔍 **Check for updates** on demand (per container or all at once) or on a configurable schedule
- ⬆️ **Apply updates** on demand or automatically on a configurable minutes interval
- 🔒 **Self-update protection** — dockpatch auto-detects itself and never updates its own container while running
- ⚙️ **Settings panel** — configure check interval, auto-update interval, and startup scan from the UI
- 📱 Responsive UI — works great on desktop and mobile

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

## Updating

```bash
docker compose pull dockpatch && docker compose up -d dockpatch
```
