# 🐳 Dockwatch

A lightweight self-hosted web app that monitors your Docker containers for image updates and can apply them on demand or on a schedule.

![UI](https://img.shields.io/badge/UI-web--based-blue) ![Arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64-lightgrey) ![Registry](https://img.shields.io/badge/registry-ghcr.io-blue)

## Features

- 🔍 **Check for updates** on demand (per container or all at once) or on a configurable schedule
- ⬆️ **Apply updates** on demand or automatically at a set daily time
- 🔒 **Self-update protection** — dockwatch never updates itself while running
- 📱 Responsive UI — works great on desktop and mobile
- 🔔 Toast notifications for all actions

## Quick start

```yaml
# docker-compose.yml
services:
  dockwatch:
    image: ghcr.io/claudeailab/dockwatch
    container_name: dockwatch
    hostname: dockwatch
    restart: unless-stopped
    user: "0"
    environment:
      TZ: ${TZ}
      SELF_NAME: dockwatch
      CHECK_SCHEDULE_MINUTES: 60
      UPDATE_SCHEDULE: ""           # e.g. "03:00" for daily 3 AM updates
    ports:
      - 8090:8090
    volumes:
      - ./config/dockwatch:/data
      - /var/run/docker.sock:/var/run/docker.sock
```

```bash
docker compose up -d dockwatch
```

Then open **http://your-server:8090**

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `TZ` | UTC | Timezone |
| `SELF_NAME` | `dockwatch` | Container name of this app (prevents self-update) |
| `CHECK_SCHEDULE_MINUTES` | `60` | How often to auto-check for updates |
| `UPDATE_SCHEDULE` | *(blank)* | Daily time to auto-apply updates, e.g. `03:00`. Leave blank to disable. |

> **Note:** The Docker socket (`/var/run/docker.sock`) must be mounted — this is how dockwatch talks to Docker.

## Self-update

Dockwatch intentionally **cannot update itself** while running. To update dockwatch, run the commands below — it will pull the new image and recreate the container without any data loss.

## Updating

```bash
docker compose pull dockwatch && docker compose up -d dockwatch
```
