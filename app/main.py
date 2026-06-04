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
VERSION = "0.1.4"

client = docker.from_env()

SETTINGS_FILE = "/data/settings.json"

DEFAULT_SETTINGS = {
    "check_interval_minutes": 0,
    "update_interval_minutes": 0,
    "check_on_startup": True,
}

settings = dict(DEFAULT_SETTINGS)

def load_settings():
    global settings
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                stored = json.load(f)
                settings = {**DEFAULT_SETTINGS, **stored}
                log.info("Settings loaded: %s", settings)
        except Exception as e:
            log.warning("Could not load settings: %s", e)
    else:
        settings = dict(DEFAULT_SETTINGS)

def save_settings():
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        log.warning("Could not save settings: %s", e)

def detect_self_name():
    """Detect our own container name by matching hostname (container ID) to running containers."""
    try:
        hostname = socket.gethostname()
        for c in client.containers.list():
            if c.id.startswith(hostname) or hostname.startswith(c.short_id):
                name = c.name
                log.info("Self-detected as container: %s", name)
                return name
    except Exception as e:
        log.warning("Could not detect self: %s", e)
    return None

SELF_NAME = None

# --- state ---
container_cache: dict = {}
cache_lock = threading.Lock()
checking_all = False
updating_set: set = set()

def pull_check(container) -> dict:
    """Check if a container has an update available. Returns status dict."""
    try:
        current_img = container.image
        repo_tags = current_img.tags
        if not repo_tags:
            return {"status": "unknown", "reason": "no tag"}
        tag = repo_tags[0]
        pulled = client.images.pull(tag)
        if pulled.id != current_img.id:
            return {"status": "update_available"}
        return {"status": "up_to_date"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}

def get_containers():
    """Return list of all containers with cached update status."""
    try:
        containers = client.containers.list(all=True)
    except Exception as e:
        log.error("Docker error: %s", e)
        return []
    result = []
    with cache_lock:
        for c in containers:
            is_self = (c.name == SELF_NAME)
            cached = container_cache.get(c.name, {})
            result.append({
                "id": c.short_id,
                "name": c.name,
                "image": c.image.tags[0] if c.image.tags else c.image.short_id,
                "status": c.status,
                "update_status": cached.get("status", "unknown"),
                "update_reason": cached.get("reason", ""),
                "is_self": is_self,
                "checking": c.name in updating_set,
            })
    return result

def check_all():
    global checking_all
    if checking_all:
        return
    checking_all = True
    log.info("Checking all containers for updates...")
    try:
        containers = client.containers.list(all=True)
        for c in containers:
            result = pull_check(c)
            with cache_lock:
                container_cache[c.name] = result
    except Exception as e:
        log.error("check_all error: %s", e)
    finally:
        checking_all = False
    log.info("Check complete.")

def check_one(name):
    try:
        c = client.containers.get(name)
        result = pull_check(c)
        with cache_lock:
            container_cache[name] = result
    except Exception as e:
        with cache_lock:
            container_cache[name] = {"status": "error", "reason": str(e)}

def update_one(name):
    if name == SELF_NAME:
        log.warning("Refusing to update self (%s)", name)
        return {"ok": False, "error": "Cannot update self while running."}
    try:
        c = client.containers.get(name)
        img_tag = c.image.tags[0] if c.image.tags else None
        if not img_tag:
            return {"ok": False, "error": "No image tag found."}
        client.images.pull(img_tag)
        c.stop()
        c.remove()
        # Recreate with same config
        attrs = c.attrs["HostConfig"]
        client.containers.run(
            img_tag,
            detach=True,
            name=name,
            restart_policy=attrs.get("RestartPolicy"),
        )
        with cache_lock:
            container_cache[name] = {"status": "up_to_date"}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---- scheduler ----
scheduler_thread_stop = threading.Event()

def run_scheduler():
    while not scheduler_thread_stop.is_set():
        schedule.run_pending()
        time.sleep(10)

check_job = None
update_job = None

def apply_schedules():
    global check_job, update_job
    schedule.clear()
    check_job = None
    update_job = None
    ci = settings.get("check_interval_minutes", 0)
    ui = settings.get("update_interval_minutes", 0)
    if ci and ci > 0:
        check_job = schedule.every(ci).minutes.do(lambda: threading.Thread(target=check_all, daemon=True).start())
        log.info("Check schedule: every %s minutes", ci)
    if ui and ui > 0:
        def auto_update():
            containers = client.containers.list()
            for c in containers:
                if c.name == SELF_NAME:
                    continue
                with cache_lock:
                    s = container_cache.get(c.name, {}).get("status")
                if s == "update_available":
                    log.info("Auto-updating %s", c.name)
                    update_one(c.name)
        update_job = schedule.every(ui).minutes.do(lambda: threading.Thread(target=auto_update, daemon=True).start())
        log.info("Auto-update schedule: every %s minutes", ui)

# ---- routes ----

@app.route("/")
def index():
    return render_template("index.html", version=VERSION)

@app.route("/api/containers")
def api_containers():
    return jsonify({"containers": get_containers(), "checking_all": checking_all})

@app.route("/api/check/all", methods=["POST"])
def api_check_all():
    threading.Thread(target=check_all, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/check/<name>", methods=["POST"])
def api_check_one(name):
    threading.Thread(target=check_one, args=(name,), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/update/<name>", methods=["POST"])
def api_update_one(name):
    if name == SELF_NAME:
        return jsonify({"ok": False, "error": "Cannot update self while running."}), 403
    result = update_one(name)
    return jsonify(result)

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    return jsonify(settings)

@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    global settings
    data = request.get_json(force=True)
    settings["check_interval_minutes"] = int(data.get("check_interval_minutes") or 0)
    settings["update_interval_minutes"] = int(data.get("update_interval_minutes") or 0)
    settings["check_on_startup"] = bool(data.get("check_on_startup", True))
    save_settings()
    apply_schedules()
    return jsonify({"ok": True, "settings": settings})

# ---- startup ----
def startup():
    global SELF_NAME
    load_settings()
    SELF_NAME = detect_self_name()
    apply_schedules()
    threading.Thread(target=run_scheduler, daemon=True).start()
    if settings.get("check_on_startup", True):
        log.info("check_on_startup enabled — scanning now...")
        threading.Thread(target=check_all, daemon=True).start()

startup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8093, debug=False)
