from flask import Flask, jsonify, request, render_template
import docker
import threading
import schedule
import time
import socket
import logging
import json
import os
import tempfile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
VERSION = "0.2.1"

client     = docker.from_env()
api_client = docker.APIClient(base_url="unix://var/run/docker.sock")

SETTINGS_FILE = "/data/settings.json"

# ── state ─────────────────────────────────────────────────────────────────────
container_cache: dict[str, dict] = {}
cache_lock = threading.Lock()

# Guarded by cache_lock — never mutate without holding it
checking_set: set[str] = set()
updating_set: set[str] = set()
update_logs: dict[str, list[str]] = {}   # name -> list of log line strings
update_done: dict[str, bool] = {}        # name -> True when finished

checking_all_flag = threading.Event()

# ── self-detection ────────────────────────────────────────────────────────────
def get_self_name() -> str:
    try:
        own_id = socket.gethostname()
        c = client.containers.get(own_id)
        return c.name.lstrip("/")
    except Exception:
        return ""

SELF_NAME = get_self_name()
log.info("Self-container name: %r", SELF_NAME)

# ── settings ──────────────────────────────────────────────────────────────────
DEFAULT_SETTINGS = {
    "check_interval_minutes":  0,
    "update_interval_minutes": 0,
    "check_on_startup":        True,
}

def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
        for k, v in DEFAULT_SETTINGS.items():
            s.setdefault(k, v)
        return s
    except Exception:
        return dict(DEFAULT_SETTINGS)

def save_settings(s: dict):
    """Atomically write settings so a crash never corrupts the file."""
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    dir_ = os.path.dirname(SETTINGS_FILE)
    # Write to a temp file in the same directory, then rename (atomic on POSIX)
    fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".settings_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

# ── image string helper ───────────────────────────────────────────────────────
def get_image_str(c) -> str:
    cfg_image = (c.attrs.get("Config") or {}).get("Image", "")
    if cfg_image and not cfg_image.startswith("sha256:"):
        return cfg_image
    try:
        tags = c.image.tags
        if tags:
            return tags[0]
    except Exception:
        pass
    return ""

def is_pullable(image_str: str) -> bool:
    if not image_str:
        return False
    if image_str.startswith("sha256:"):
        return False
    if "@sha256:" in image_str:
        return False
    return True

# ── digest-only check (no pull) ───────────────────────────────────────────────
def pull_check(name: str, image_str: str) -> tuple[str, str]:
    """Compare remote digest via registry API — never pulls the image down."""
    if not is_pullable(image_str):
        reason = "No pullable tag (local or digest-only image)"
        log.info("Check %s → skipped (%s)", name, reason)
        return "error", reason

    # get local digest
    try:
        local_img = client.images.get(image_str)
        local_digests = local_img.attrs.get("RepoDigests") or []
        local_digest = local_digests[0].split("@")[-1] if local_digests else ""
    except docker.errors.ImageNotFound:
        log.info("Check %s → update_available (image not local)", name)
        return "update_available", ""
    except Exception as e:
        return "error", f"Local image lookup failed: {e}"

    # get remote digest without downloading the image
    try:
        log.info("Checking remote digest for %s…", image_str)
        dist = api_client.inspect_distribution(image_str)
        remote_digest = (dist.get("Descriptor") or {}).get("digest", "")
    except docker.errors.APIError as e:
        reason = str(e)
        if "unauthorized" in reason.lower() or "authentication" in reason.lower():
            reason = "Registry auth required"
        elif "not found" in reason.lower() or "manifest" in reason.lower():
            reason = "Image not found in registry"
        elif "timeout" in reason.lower():
            reason = "Registry timed out"
        else:
            reason = f"Registry error: {reason[:120]}"
        log.warning("pull_check %s: %s", name, reason)
        return "error", reason
    except Exception as e:
        log.warning("pull_check %s unexpected: %s", name, e)
        return "error", f"Unexpected error: {str(e)[:120]}"

    if not remote_digest or not local_digest:
        log.info("Check %s → unknown (could not compare digests)", name)
        return "up_to_date", ""

    if local_digest == remote_digest:
        log.info("Check %s → up_to_date", name)
        return "up_to_date", ""

    log.info("Check %s → update_available (local=%s… remote=%s…)",
             name, local_digest[:16], remote_digest[:16])
    return "update_available", ""

def snapshot_containers() -> list[dict]:
    try:
        containers = client.containers.list(all=True)
    except Exception as e:
        log.error("Failed to list containers: %s", e)
        return []

    result = []
    with cache_lock:
        for c in containers:
            name = c.name.lstrip("/")
            image_str = get_image_str(c)
            cached = container_cache.get(name, {})

            if name in checking_set:
                update_status = "checking"
                reason = ""
            elif name in updating_set:
                update_status = "updating"
                reason = ""
            else:
                update_status = cached.get("update_status", "unknown")
                reason = cached.get("update_reason", "")

            result.append({
                "name":          name,
                "image":         image_str,
                "status":        c.status,
                "is_self":       name == SELF_NAME,
                "update_status": update_status,
                "update_reason": reason,
            })

    result.sort(key=lambda x: x["name"].lower())
    return result

# ── check workers ─────────────────────────────────────────────────────────────
def _do_check_one(name: str, image_str: str):
    with cache_lock:
        checking_set.add(name)
    try:
        status, reason = pull_check(name, image_str)
        with cache_lock:
            container_cache[name] = {"update_status": status, "update_reason": reason}
        log.info("Check %s → %s%s", name, status, f" ({reason})" if reason else "")
    finally:
        with cache_lock:
            checking_set.discard(name)

def check_one(name: str):
    try:
        c = client.containers.get(name)
    except docker.errors.NotFound:
        log.warning("check_one: container %r not found", name)
        return
    image_str = get_image_str(c)
    threading.Thread(target=_do_check_one, args=(name, image_str), daemon=True).start()

def check_all():
    if checking_all_flag.is_set():
        log.info("check_all already running, skipping")
        return
    checking_all_flag.set()

    def _worker():
        try:
            containers = client.containers.list(all=True)
            threads = []
            for c in containers:
                name = c.name.lstrip("/")
                image_str = get_image_str(c)
                t = threading.Thread(target=_do_check_one, args=(name, image_str), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
            log.info("check_all complete")
        except Exception as e:
            log.error("check_all error: %s", e)
        finally:
            checking_all_flag.clear()

    threading.Thread(target=_worker, daemon=True).start()

# ── update worker ─────────────────────────────────────────────────────────────
def _emit(name: str, line: str):
    """Append a log line to the update log buffer for a container."""
    log.info("[%s] %s", name, line)
    with cache_lock:
        update_logs.setdefault(name, []).append(line)


def _do_update(name: str) -> tuple[bool, str]:
    """Pull and recreate a container, preserving its full config."""
    if name == SELF_NAME:
        return False, "Cannot update self"

    with cache_lock:
        updating_set.add(name)
        update_logs[name] = []
        update_done[name] = False
    try:
        try:
            c = client.containers.get(name)
        except docker.errors.NotFound:
            return False, f"Container {name!r} not found"

        image_str = get_image_str(c)
        if not image_str:
            return False, "No pullable image tag"

        _emit(name, f"⬇️  Pulling image {image_str}…")
        for chunk in client.api.pull(image_str, stream=True, decode=True):
            status  = chunk.get("status", "")
            prog    = chunk.get("progressDetail", {})
            cid     = chunk.get("id", "")
            current = prog.get("current")
            total   = prog.get("total")
            if current and total and total > 0:
                pct = int(current * 100 / total)
                _emit(name, f"  {cid}  {status}  {pct}%")
            elif status:
                msg = f"  {cid}  {status}".strip() if cid else f"  {status}"
                _emit(name, msg)

        _emit(name, "✅  Pull complete")

        attrs      = c.attrs
        cfg        = attrs.get("Config") or {}
        host_cfg   = attrs.get("HostConfig") or {}
        net_cfg    = attrs.get("NetworkSettings", {}).get("Networks", {})

        # Reconstruct volume bindings — both bind-mounts and named volumes
        volumes: dict[str, dict] = {}
        for m in (attrs.get("Mounts") or []):
            if m.get("Type") in ("bind", "volume"):
                volumes[m["Source"]] = {
                    "bind": m["Destination"],
                    "mode": m.get("Mode", "rw"),
                }

        # Use the first network for containers.run(); attach extras afterwards
        net_names = list(net_cfg.keys())
        primary_net = net_names[0] if net_names else None

        _emit(name, "🛑  Stopping old container…")
        c.stop()
        _emit(name, "🗑️  Removing old container…")
        c.remove()
        _emit(name, "🚀  Starting new container…")

        new_c = client.containers.run(
            image_str,
            detach=True,
            name=name,
            hostname=cfg.get("Hostname", name),
            restart_policy=host_cfg.get("RestartPolicy") or {"Name": "unless-stopped"},
            ports=host_cfg.get("PortBindings") or {},
            volumes=volumes,
            environment=cfg.get("Env") or [],
            labels=cfg.get("Labels") or {},
            network=primary_net,
            cap_add=host_cfg.get("CapAdd") or [],
            devices=host_cfg.get("Devices") or [],
            sysctls=host_cfg.get("Sysctls") or {},
        )

        # Re-attach any additional networks
        for net_name in net_names[1:]:
            try:
                net_aliases = (net_cfg[net_name].get("Aliases") or [])
                network = client.networks.get(net_name)
                network.connect(new_c, aliases=net_aliases)
                _emit(name, f"🔗  Re-attached network {net_name}")
            except Exception as e:
                log.warning("Could not re-attach network %s to %s: %s", net_name, name, e)
                _emit(name, f"⚠️  Could not re-attach network {net_name}: {e}")

        with cache_lock:
            container_cache[name] = {"update_status": "up_to_date", "update_reason": ""}

        _emit(name, f"✅  {name} updated successfully → {new_c.id[:12]}")
        log.info("Updated %s → %s", name, new_c.id[:12])
        return True, ""
    except Exception as e:
        log.error("Update %s failed: %s", name, e)
        _emit(name, f"❌  Error: {e}")
        return False, str(e)
    finally:
        with cache_lock:
            updating_set.discard(name)
            update_done[name] = True

def do_update_async(name: str):
    """Fire-and-forget update; result is reflected in container_cache."""
    threading.Thread(target=_do_update, args=(name,), daemon=True).start()

# ── scheduler ─────────────────────────────────────────────────────────────────
scheduler_lock = threading.Lock()

def rebuild_schedule():
    with scheduler_lock:
        schedule.clear()
        s = load_settings()
        ci = s.get("check_interval_minutes", 0)
        ui = s.get("update_interval_minutes", 0)
        if ci and ci > 0:
            schedule.every(ci).minutes.do(check_all)
            log.info("Scheduled check every %d min", ci)
        if ui and ui > 0:
            def auto_update_all():
                for name, info in list(container_cache.items()):
                    if info.get("update_status") == "update_available" and name != SELF_NAME:
                        threading.Thread(target=_do_update, args=(name,), daemon=True).start()
            schedule.every(ui).minutes.do(auto_update_all)
            log.info("Scheduled auto-update every %d min", ui)

def scheduler_loop():
    while True:
        time.sleep(30)
        with scheduler_lock:
            schedule.run_pending()

# ── API ───────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", version=VERSION)

@app.route("/api/containers")
def api_containers():
    return jsonify({
        "containers":   snapshot_containers(),
        "checking_all": checking_all_flag.is_set(),
    })

@app.route("/api/check/all", methods=["POST"])
def api_check_all():
    check_all()
    return jsonify({"ok": True})

@app.route("/api/check/<name>", methods=["POST"])
def api_check_one(name):
    check_one(name)
    return jsonify({"ok": True})

@app.route("/api/update/<name>", methods=["POST"])
def api_update(name):
    if name == SELF_NAME:
        return jsonify({"ok": False, "error": "Cannot update self"})
    # Run in background; UI polls for status changes
    do_update_async(name)
    return jsonify({"ok": True})

@app.route("/api/update-log/<name>")
def api_update_log(name):
    """SSE stream of update log lines for a container."""
    def generate():
        sent = 0
        while True:
            with cache_lock:
                lines = update_logs.get(name, [])
                done  = update_done.get(name, False)
            while sent < len(lines):
                yield f"data: {lines[sent]}\n\n"
                sent += 1
            if done and sent >= len(lines):
                yield "data: __DONE__\n\n"
                return
            time.sleep(0.4)
    return app.response_class(generate(), mimetype="text/event-stream",
                               headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(load_settings())

@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    data = request.get_json(force=True) or {}

    # Validate and sanitise — reject obviously bad values
    def _int(val, default: int) -> int:
        try:
            v = int(val)
            return max(0, v)
        except (TypeError, ValueError):
            return default

    s = {
        "check_interval_minutes":  _int(data.get("check_interval_minutes"),  0),
        "update_interval_minutes": _int(data.get("update_interval_minutes"), 0),
        "check_on_startup":        bool(data.get("check_on_startup", True)),
    }
    save_settings(s)
    rebuild_schedule()
    return jsonify({"ok": True})

# ── startup ───────────────────────────────────────────────────────────────────
def on_startup():
    rebuild_schedule()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    s = load_settings()
    if s.get("check_on_startup", True):
        log.info("check_on_startup=true — running initial check")
        check_all()

on_startup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8093, debug=False)
