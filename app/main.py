from flask import Flask, jsonify, request, render_template
import docker
import threading
import schedule
import time
import datetime
import socket
import logging
import json
import os
import tempfile

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
VERSION = "0.6.0"

client     = docker.from_env()
api_client = docker.APIClient(base_url="unix://var/run/docker.sock")

SETTINGS_FILE     = "/data/settings.json"
CREDENTIALS_FILE  = "/data/credentials.json"
HISTORY_FILE      = "/data/update_history.json"

# ── state ─────────────────────────────────────────────────────────────────────
container_cache: dict[str, dict] = {}
cache_lock = threading.Lock()

# Guarded by cache_lock — never mutate without holding it
checking_set: set[str] = set()
updating_set: set[str] = set()
update_logs: dict[str, list[str]] = {}   # name -> list of log line strings
update_done: dict[str, bool] = {}        # name -> True when finished

# Limit how many completed log buffers we keep in memory to avoid unbounded growth
MAX_COMPLETED_LOG_BUFFERS = 50
_completed_log_order: list[str] = []    # insertion-ordered list of names whose update is done

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
    "check_interval_hours":  0,
    "update_interval_hours": 0,
    "check_on_startup":      True,
    "update_window_start":   "",
    "update_window_end":     "",
    "excluded_containers":   [],
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

def get_excluded() -> list[str]:
    return load_settings().get("excluded_containers", [])

def set_excluded(names: list[str]):
    s = load_settings()
    s["excluded_containers"] = sorted(set(names))
    save_settings(s)

def is_excluded(name: str) -> bool:
    return name in get_excluded()

# ── update history ────────────────────────────────────────────────────────────
def load_history() -> dict:
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_history(history: dict):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    dir_ = os.path.dirname(HISTORY_FILE)
    fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".history_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(history, f, indent=2)
        os.replace(tmp, HISTORY_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def record_update(name: str, success: bool, image: str = ""):
    history = load_history()
    history[name] = {
        "last_updated":     datetime.datetime.utcnow().isoformat() + "Z",
        "last_update_ok":   success,
        "last_update_image": image,
    }
    save_history(history)

# ── registry credentials ──────────────────────────────────────────────────────
def load_credentials() -> dict:
    try:
        with open(CREDENTIALS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"registries": {}}

def save_credentials(creds: dict):
    os.makedirs(os.path.dirname(CREDENTIALS_FILE), exist_ok=True)
    dir_ = os.path.dirname(CREDENTIALS_FILE)
    fd, tmp = tempfile.mkstemp(dir=dir_, prefix=".creds_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(creds, f, indent=2)
        # Restrict file permissions: owner read/write only (credentials are sensitive)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CREDENTIALS_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def get_registry(image_str: str) -> str:
    """Return the registry hostname for an image name."""
    parts = image_str.split("/")
    if len(parts) == 1:
        return "registry-1.docker.io"
    first = parts[0]
    if "." in first or ":" in first or first == "localhost":
        return first
    return "registry-1.docker.io"

def get_auth_config(image_str: str) -> "dict | None":
    """Return a docker auth_config dict for this image's registry, or None."""
    reg = get_registry(image_str)
    info = load_credentials().get("registries", {}).get(reg)
    if info and info.get("username") and info.get("password"):
        return {"username": info["username"], "password": info["password"]}
    return None

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
        reason = "Custom image (digest-only or local)"
        log.info("Check %s → skipped (%s)", name, reason)
        return "custom", reason

    # get local digest
    image_exists_locally = False
    try:
        local_img = client.images.get(image_str)
        image_exists_locally = True
        local_digests = local_img.attrs.get("RepoDigests") or []
        if not local_digests:
            log.info("Check %s → custom (locally built, no registry digest)", name)
            return "custom", ""
        local_digest = local_digests[0].split("@")[-1]
    except docker.errors.ImageNotFound:
        log.info("Check %s → update_available (image not local)", name)
        return "update_available", ""
    except Exception:
        log.info("Check %s → custom (local image lookup error)", name)
        return "custom", ""

    # get remote digest without downloading the image
    auth = get_auth_config(image_str)
    try:
        log.info("Checking remote digest for %s…", image_str)
        dist = api_client.inspect_distribution(image_str, auth_config=auth)
        remote_digest = (dist.get("Descriptor") or {}).get("digest", "")
    except docker.errors.APIError as e:
        reason = str(e)
        if "unauthorized" in reason.lower() or "authentication" in reason.lower():
            if auth:
                log.warning("pull_check %s: auth failed with configured credentials", name)
                return "error", "Registry auth failed — check credentials in settings"
            if image_exists_locally:
                log.info("Check %s → custom (registry auth challenge, image is local)", name)
                return "custom", ""
            return "error", "Registry auth required — add credentials in settings"
        elif "timeout" in reason.lower():
            reason = "Registry timed out"
            log.warning("pull_check %s: %s", name, reason)
            return "error", reason
        else:
            if image_exists_locally:
                log.info("Check %s → custom (registry unavailable/not found)", name)
                return "custom", "Custom image (not in registry)"
            reason = str(e)[:120]
            log.warning("pull_check %s: %s", name, reason)
            return "error", reason
    except Exception as e:
        if image_exists_locally:
            log.info("Check %s → custom (registry check failed)", name)
            return "custom", "Custom image (registry check failed)"
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

    history  = load_history()
    excluded = get_excluded()
    result   = []
    with cache_lock:
        for c in containers:
            name = c.name.lstrip("/")
            image_str = get_image_str(c)
            cached = container_cache.get(name, {})
            hist   = history.get(name, {})

            if name in checking_set:
                update_status = "checking"
                reason = ""
            elif name in updating_set:
                update_status = "updating"
                reason = ""
            else:
                update_status = cached.get("update_status", "unknown")
                reason = cached.get("update_reason", "")

            started_at = c.attrs.get("State", {}).get("StartedAt", "")

            result.append({
                "name":              name,
                "image":             image_str,
                "status":            c.status,
                "started_at":        started_at,
                "is_self":           name == SELF_NAME,
                "is_excluded":       name in excluded,
                "update_status":     update_status,
                "update_reason":     reason,
                "last_updated":      hist.get("last_updated", ""),
                "last_update_ok":    hist.get("last_update_ok", None),
                "last_update_image": hist.get("last_update_image", ""),
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

def _evict_log_buffer(name: str):
    """Track completed log buffers and evict oldest ones beyond the cap."""
    global _completed_log_order
    if name not in _completed_log_order:
        _completed_log_order.append(name)
    while len(_completed_log_order) > MAX_COMPLETED_LOG_BUFFERS:
        oldest = _completed_log_order.pop(0)
        update_logs.pop(oldest, None)
        update_done.pop(oldest, None)
        log.debug("Evicted update log buffer for %r", oldest)


def _do_update(name: str) -> tuple[bool, str]:
    """Pull and recreate a container, preserving its full config."""
    if name == SELF_NAME:
        return False, "Cannot update self"

    if is_excluded(name):
        return False, f"{name!r} is excluded from updates"

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
        for chunk in client.api.pull(image_str, stream=True, decode=True,
                                     auth_config=get_auth_config(image_str)):
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

        record_update(name, True, image_str)
        _emit(name, f"✅  {name} updated successfully → {new_c.id[:12]}")
        log.info("Updated %s → %s", name, new_c.id[:12])
        return True, ""
    except Exception as e:
        log.error("Update %s failed: %s", name, e)
        _emit(name, f"❌  Error: {e}")
        record_update(name, False)
        return False, str(e)
    finally:
        with cache_lock:
            updating_set.discard(name)
            update_done[name] = True
            _evict_log_buffer(name)

def do_update_async(name: str):
    """Fire-and-forget update; result is reflected in container_cache."""
    threading.Thread(target=_do_update, args=(name,), daemon=True).start()

# ── scheduler ─────────────────────────────────────────────────────────────────
scheduler_lock = threading.Lock()

def _within_update_window() -> bool:
    """Return True if the current server time falls within the configured
    maintenance window. If no start time is set, always returns True.
    If only a start time is set, allows updates from start time onward.
    If both start and end are set, allows updates only between them."""
    s = load_settings()
    start_str = (s.get("update_window_start") or "").strip()
    end_str   = (s.get("update_window_end")   or "").strip()

    if not start_str:
        return True  # no window configured — always allowed

    try:
        sh, sm = start_str.split(":")
        t_start = datetime.time(int(sh), int(sm))
    except Exception:
        return True  # malformed start — don't block updates

    now = datetime.datetime.now().time()

    if not end_str:
        return now >= t_start  # only start configured

    try:
        eh, em = end_str.split(":")
        t_end = datetime.time(int(eh), int(em))
    except Exception:
        return now >= t_start  # malformed end — treat as start-only

    if t_start <= t_end:
        # same-day window e.g. 02:00 – 05:00
        return t_start <= now <= t_end
    else:
        # overnight window e.g. 22:00 – 04:00
        return now >= t_start or now <= t_end

def rebuild_schedule():
    with scheduler_lock:
        schedule.clear()
        s = load_settings()
        ci = s.get("check_interval_hours", 0)
        ui = s.get("update_interval_hours", 0)
        if ci and ci > 0:
            schedule.every(ci).hours.do(check_all)
            log.info("Scheduled check every %d hr", ci)
        if ui and ui > 0:
            def auto_update_all():
                if not _within_update_window():
                    log.info("Auto-update skipped: outside maintenance window")
                    return
                excluded = get_excluded()
                for name, info in list(container_cache.items()):
                    if name == SELF_NAME:
                        continue
                    if name in excluded:
                        log.info("Auto-update skipped for %r: excluded", name)
                        continue
                    if info.get("update_status") == "update_available":
                        threading.Thread(target=_do_update, args=(name,), daemon=True).start()
            schedule.every(ui).hours.do(auto_update_all)
            log.info("Scheduled auto-update every %d hr", ui)

def scheduler_loop():
    while True:
        time.sleep(30)
        with scheduler_lock:
            schedule.run_pending()

# ── API ───────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", version=VERSION)

@app.route("/settings")
def settings_page():
    return render_template("settings.html", version=VERSION)

@app.route("/api/containers")
def api_containers():
    containers = snapshot_containers()
    checking_count = 0
    with cache_lock:
        checking_count = len(checking_set)
    return jsonify({
        "containers":    containers,
        "checking_all":  checking_all_flag.is_set(),
        "checking_count": checking_count,
    })

@app.route("/api/stats")
def api_stats():
    containers = snapshot_containers()
    total    = len(containers)
    updates  = sum(1 for c in containers if c["update_status"] == "update_available")
    uptodate = sum(1 for c in containers if c["update_status"] == "up_to_date")
    custom   = sum(1 for c in containers if c["update_status"] == "custom")
    errors   = sum(1 for c in containers if c["update_status"] == "error")
    unknown  = sum(1 for c in containers if c["update_status"] == "unknown")
    return jsonify({
        "total": total, "updates": updates, "up_to_date": uptodate,
        "custom": custom, "errors": errors, "unknown": unknown,
    })

@app.route("/api/check/all", methods=["POST"])
def api_check_all():
    check_all()
    return jsonify({"ok": True})

@app.route("/api/check/<name>", methods=["POST"])
def api_check_one(name):
    check_one(name)
    return jsonify({"ok": True})

@app.route("/api/update/all", methods=["POST"])
def api_update_all():
    """Concurrently kick off updates for all containers with available updates,
    skipping excluded containers."""
    excluded = get_excluded()
    started  = []
    skipped  = []
    with cache_lock:
        names = list(container_cache.keys())
    for name in names:
        if name == SELF_NAME:
            skipped.append(name)
            continue
        if name in excluded:
            skipped.append(name)
            continue
        with cache_lock:
            info = container_cache.get(name, {})
        if info.get("update_status") == "update_available":
            do_update_async(name)
            started.append(name)
    return jsonify({"ok": True, "started": started, "skipped": skipped})

@app.route("/api/update/<name>", methods=["POST"])
def api_update(name):
    if name == SELF_NAME:
        return jsonify({"ok": False, "error": "Cannot update self"})
    if is_excluded(name):
        return jsonify({"ok": False, "error": f"{name!r} is excluded from updates"})
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

@app.route("/api/history")
def api_history():
    return jsonify(load_history())

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(load_settings())

@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    data = request.get_json(force=True) or {}

    def _int(val, default: int) -> int:
        try:
            v = int(val)
            return max(0, v)
        except (TypeError, ValueError):
            return default

    def _time(val):
        v = (val or "").strip()
        try:
            h, m = v.split(":")
            datetime.time(int(h), int(m))
            return f"{int(h):02d}:{int(m):02d}"
        except Exception:
            return ""

    s = load_settings()
    s.update({
        "check_interval_hours":  _int(data.get("check_interval_hours"),  0),
        "update_interval_hours": _int(data.get("update_interval_hours"), 0),
        "check_on_startup":      bool(data.get("check_on_startup", True)),
        "update_window_start":   _time(data.get("update_window_start")),
        "update_window_end":     _time(data.get("update_window_end")),
        # excluded_containers is managed separately — don't overwrite here
    })
    save_settings(s)
    rebuild_schedule()
    return jsonify({"ok": True})

# ── exclusion API ─────────────────────────────────────────────────────────────
@app.route("/api/excluded", methods=["GET"])
def api_excluded_get():
    return jsonify({"excluded": get_excluded()})

@app.route("/api/excluded/<name>", methods=["POST"])
def api_excluded_add(name):
    excluded = get_excluded()
    if name not in excluded:
        excluded.append(name)
        set_excluded(excluded)
        log.info("Container %r added to exclusion list", name)
    return jsonify({"ok": True, "excluded": get_excluded()})

@app.route("/api/excluded/<name>", methods=["DELETE"])
def api_excluded_remove(name):
    excluded = get_excluded()
    if name in excluded:
        excluded.remove(name)
        set_excluded(excluded)
        log.info("Container %r removed from exclusion list", name)
    return jsonify({"ok": True, "excluded": get_excluded()})

# ── credentials API ───────────────────────────────────────────────────────────
@app.route("/api/credentials", methods=["GET"])
def api_credentials_get():
    creds = load_credentials()
    safe = {reg: {"username": info.get("username", ""), "has_token": bool(info.get("password"))}
            for reg, info in creds.get("registries", {}).items()}
    return jsonify({"registries": safe})

@app.route("/api/credentials", methods=["POST"])
def api_credentials_post():
    data = request.get_json(force=True) or {}
    registry = (data.get("registry") or "").strip().rstrip("/")
    username  = (data.get("username")  or "").strip()
    password  = (data.get("password")  or "").strip()
    if not registry or not username or not password:
        return jsonify({"ok": False, "error": "All fields are required"})
    creds = load_credentials()
    creds.setdefault("registries", {})[registry] = {"username": username, "password": password}
    save_credentials(creds)
    return jsonify({"ok": True})

@app.route("/api/credentials/<path:registry>", methods=["DELETE"])
def api_credentials_delete(registry):
    creds = load_credentials()
    creds.get("registries", {}).pop(registry, None)
    save_credentials(creds)
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
