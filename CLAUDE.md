# CLAUDE.md — Project Guidelines

## Docker Build
- The web app is always built as a Docker container
- Always build for both **x86** and **ARM** architectures (multi-platform builds)
- Always host the image on the **GitHub Container Registry (ghcr.io)**

## GitHub Workflow
- Always merge branches and pull requests after changes
- Always trigger and run the GitHub Actions workflow for the build after merging

## Versioning
- Always display a discreet version number on the web app UI
- Always bump the version number with each push

## UI / UX
- The web app must always be fully functional and intuitive on both **desktop** and **mobile**

## README — Updating Section
Add the following section to the GitHub README:

### Updating
```bash
docker compose pull ${project_name} && docker compose up -d ${project_name}
```

## docker-compose.yml Template
Use the following as the template for `docker-compose.yml`, replacing `${project_name}` with the actual project name:

```yaml
  ${project_name}:
    image: ghcr.io/claudeailab/${project_name}
    container_name: ${project_name}
    hostname: ${project_name}
    restart: unless-stopped
    user: "0"
    environment:
      TZ: ${TZ}
    ports:
      - 8090:8090
    volumes:
      - ./config/${project_name}:/data
```
