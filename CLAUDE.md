# CLAUDE.md — Project Rules & Guidelines

## Project Description
**Dockpatch** is a lightweight self-hosted web app that monitors your Docker containers for image updates and can apply them on demand or on a schedule. It connects directly to the Docker socket, checks registries for newer image digests, and lets you update individual containers or all of them at once — all from a clean, responsive web UI. It also protects itself from accidental self-updates while running.

## Hosting
- Always host the image on the GitHub Container Registry (ghcr.io/claudeailab/${project_name})

## Branches & CI
- **Only `main` exists — commit and push directly to `main`, always**
- Never create feature branches or pull requests
- Always wait for the GitHub Actions build to complete after pushing to main

## Web App Quality
- The web app must always be functional and intuitive on both desktop and mobile
- Test layouts and interactions for both screen sizes before pushing

## Docker Build
- The project is always packaged as a Docker container
- Always build for both **x86** (amd64) and **ARM** (arm64/v8) platforms

## Versioning
- Always display a discreet version number somewhere on the web app (e.g. footer)
- Always bump the version number with every push

## README Requirements
- Maintain a **Features** section in README.md with bullet points and a short description of each feature
- Include an **Updating** section in README.md with the following command (replacing `${project_name}` with the actual project name):

```bash
docker compose pull ${project_name} && docker compose up -d ${project_name}
```

## docker-compose.yml Template
Always use the following template for `docker-compose.yml`, replacing `${project_name}` with the actual project name and `${port}` with the port specified for the project:

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
      - ${port}:${port}
    volumes:
      - ./config/${project_name}:/data
```
