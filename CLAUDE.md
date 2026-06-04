# CLAUDE.md

## Project Guidelines

- The web app is built as a Docker container and must always be built for both **x86** and **ARM** architectures.
- The image is always hosted on the **GitHub Container Registry (GHCR)**.
- Always merge branches and pull requests and run the GitHub Actions workflow for the build.
- Always display a discreet version number on the web app and always bump it up with each push.
- The web app must always be functional and intuitive on both **desktop** and **mobile**.
- On the GitHub README file, add an **"Updating"** section with the following command:

```
docker compose pull ${project_name} && docker compose up -d ${project_name}
```

- Always use the following as a template for `docker-compose.yml`, replacing `${project_name}` with the actual project name:

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
