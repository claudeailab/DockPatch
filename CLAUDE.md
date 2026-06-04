# Dockpatch — Project Guidelines

## Docker build
- Always build the image for both **x86 (amd64)** and **arm64**
- Always host the image on the **GitHub Container Registry** (`ghcr.io/claudeailab/dockpatch`)
- Always merge branches/pull requests and run the GitHub Actions build workflow after changes

## Versioning
- Always display a discreet version number in the web UI
- Always bump the version with each push

## UI / UX
- The web app must always be functional and intuitive on **desktop and mobile**

## README — Updating section
The README must always include an **Updating** section with:
```bash
docker compose pull dockpatch && docker compose up -d dockpatch
```

## docker-compose.yml template
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
```
