from flask import Flask, jsonify, request, render_template
import docker
import threading
import schedule
import time
import socket
import logging
import json
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
VERSION = "0.1.8"

client = docker.from_env()

SETTINGS_FILE = "/data/settings.json"

# ── state ─────────────────────────────────────────────────────────────────────
container_cache: dict[str, dict] = {}
cache_lock = threading.Lock()
checking_set: set[str] = set()
checking_all_flag = threading.Event()
updating_set: set[str] = set()

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
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)

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

# ── helpers ───────────────────────────────────────────────────────────────────
def is_pullable(image_str: str) -> bool:
    if not image_str:
        return False
    if image_str.startswith("sha256:"):
        return False
    if "@sha256:" in image_str:
        return False
    return True

def pull_check(name: str, image_str: str) -> tuple[str, str]:
    if not is_pullable(image_str):
        reason = "No pullable tag (local or digest-only image)"
        log.info("Check %s → skipped (%s)", name, reason)
        return "error", reason

    try:
        local_img = client.images.get(image_str)
        local_digest = (local_img.attrs.get("RepoDigests") or [""])[0]
    except docker.errors.ImageNotFound:
        return "error", "Local image not found"
    except Exception as e:
        return "error", f"Local image lookup failed: {e}"

    try:
        log.info("Pulling %s for update check…", image_str)
        remote_img = client.images.pull(image_str)
        remote_digest = (remote_img.attrs.get("RepoDigests") or [""])[0]
    except docker.errors.APIError as e:
        reason = str(e)
        if "unauthorized" in reason.lower() or "authentication" in reason.lower():
            reason = "Registry auth required"
        elif "not found" in reason.lower() or "manifest" in reason.lower():
            reason = "Image not found in registry"
        elif "timeout" in reason.lower() or "timed out" in reason.lower():
            reason = "Registry pull timed out"
        else:
            reason = f"Pull failed: {reason[:120]}"
        log.warning("pull_check %s: %s", name, reason)
        return "error", reason
    except Exception as e:
        log.warning("pull_check %s unexpected error: %s", name, e)
        return "error", f"Unexpected error: {str(e)[:120]}"

    if not remote_digest or not local_digest:
        if local_img.id == remote_img.id:
            return "up_to_date", ""
        return "update_available", ""

    if local_digest == remote_digest:
        return "up_to_date", ""
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
    checking_set.add(name)
    try:
        status, reason = pull_check(name, image_str)
        with cache_lock:
            container_cache[name] = {"update_status": status, "update_reason": reason}
        log.info("Check %s → %s%s", name, status, f" ({reason})" if reason else "")
    finally:
        checking_set.discard(name)

def check_one(name: str):
    try:
        c = client.containers.get(name)
    except docker.errors.NotFound:
        log.warning("check_one: container %r not found", name)
        return
    image_str = get_image_str(c)
    t = threading.Thread(target=_do_check_one, args=(name, image_str), daemon=True)
    t.start()

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
                if name == SELF_NAME:
                    continue
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
def do_update(name: str) -> tuple[bool, str]:
    if name == SELF_NAME:
        return False, "Cannot update self"
    updating_set.add(name)
    try:
        c = client.containers.get(name)
        image_str = get_image_str(c)
        if not image_str:
            return False, "No pullable image tag"

        log.info("Updating %s…", name)
        client.images.pull(image_str)

        config = c.attrs.get("HostConfig", {})
        net    = c.attrs.get("NetworkSettings", {}).get("Networks", {})
        c.stop()
        c.remove()

        new_c = client.containers.run(
            image_str,
            detach=True,
            name=name,
            hostname=c.attrs.get("Config", {}).get("Hostname", name),
            restart_policy=config.get("RestartPolicy", {"Name": "unless-stopped"}),
            ports=config.get("PortBindings") or {},
            volumes={
                m["Source"]: {"bind": m["Destination"], "mode": m["Mode"]}
                for m in (c.attrs.get("Mounts") or [])
                if m.get("Type") == "bind"
            },
            environment=c.attrs.get("Config", {}).get("Env") or [],
            network=list(net.keys())[0] if net else None,
        )
        with cache_lock:
            container_cache[name] = {"update_status": "up_to_date", "update_reason": ""}
        log.info("Updated %s → %s", name, new_c.id[:12])
        return True, ""
    except Exception as e:
        log.error("Update %s failed: %s", name, e)
        return False, str(e)
    finally:
        updating_set.discard(name)

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
                        threading.Thread(target=do_update, args=(name,), daemon=True).start()
            schedule.every(ui).minutes.do(auto_update_all)
            log.info("Scheduled auto-update every %d min", ui)

def scheduler_loop():
    while True:
        with scheduler_lock:
            schedule.run_pending()
        time.sleep(30)

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
    ok, err = do_update(name)
    return jsonify({"ok": ok, "error": err})

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(load_settings())

@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    s = request.get_json(force=True)
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
