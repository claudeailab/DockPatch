from flask import Flask, render_template, jsonify, request
import docker
import threading
import schedule
import time
import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
VERSION = "0.1.1"

client = docker.from_env()

# The container name of dockwatch itself — never update this one
SELF_NAME = os.environ.get("SELF_NAME", "dockwatch")

# In-memory state
state = {
    "containers": {},       # name -> {image, current_digest, latest_digest, status, last_checked}
    "last_full_check": None,
    "check_schedule": int(os.environ.get("CHECK_SCHEDULE_MINUTES", 60)),
    "update_schedule": int(os.environ.get("UPDATE_SCHEDULE_MINUTES", 0)),  # minutes interval, 0 = disabled
    "updating": [],         # names currently being updated
    "checking": False,
}
state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def get_running_containers():
    """Return all running containers that have a registry image."""
    result = []
    for c in client.containers.list():
        image_tags = c.image.tags
        if not image_tags:
            continue  # locally built / untagged — skip
        result.append(c)
    return result


def pull_latest_digest(image_tag: str):
    """Pull the image from the registry and return its RepoDigest."""
    try:
        img = client.images.pull(image_tag)
        digests = img.attrs.get("RepoDigests", [])
        return digests[0] if digests else None
    except Exception as e:
        log.warning(f"Could not pull {image_tag}: {e}")
        return None


def current_digest(image_tag: str):
    """Return the RepoDigest of the locally running image."""
    try:
        img = client.images.get(image_tag)
        digests = img.attrs.get("RepoDigests", [])
        return digests[0] if digests else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Check logic
# ---------------------------------------------------------------------------

def check_container(name: str, image_tag: str):
    """Check a single container for updates. Updates state in place."""
    cur = current_digest(image_tag)
    lat = pull_latest_digest(image_tag)
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    if cur is None or lat is None:
        status = "unknown"
    elif cur == lat:
        status = "up-to-date"
    else:
        status = "update-available"

    with state_lock:
        state["containers"][name] = {
            "image": image_tag,
            "current_digest": cur,
            "latest_digest": lat,
            "status": status,
            "last_checked": now,
        }


def check_all():
    """Check every running container for updates."""
    with state_lock:
        state["checking"] = True
    try:
        containers = get_running_containers()
        threads = []
        for c in containers:
            image_tag = c.image.tags[0]
            t = threading.Thread(
                target=check_container, args=(c.name, image_tag), daemon=True
            )
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        with state_lock:
            state["last_full_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
    finally:
        with state_lock:
            state["checking"] = False


# ---------------------------------------------------------------------------
# Update logic
# ---------------------------------------------------------------------------

def update_container(name: str):
    """Pull latest image, recreate the container."""
    if name == SELF_NAME:
        log.warning("Skipping self-update of %s", name)
        return {"ok": False, "error": "Cannot update dockwatch while it is running."}

    with state_lock:
        if name not in state["containers"]:
            return {"ok": False, "error": "Container not found in state."}
        state["updating"].append(name)

    try:
        containers = {c.name: c for c in client.containers.list()}
        c = containers.get(name)
        if not c:
            return {"ok": False, "error": "Container not running."}

        image_tag = c.image.tags[0]

        # Pull fresh image
        log.info("Pulling %s for %s", image_tag, name)
        client.images.pull(image_tag)

        # Grab config for recreate
        attrs = c.attrs
        host_config = attrs["HostConfig"]
        config = attrs["Config"]

        # Stop & remove old container
        log.info("Stopping %s", name)
        c.stop(timeout=30)
        c.remove()

        # Recreate
        log.info("Recreating %s", name)
        new_c = client.containers.run(
            image_tag,
            detach=True,
            name=name,
            hostname=attrs["Config"].get("Hostname", name),
            restart_policy=host_config.get("RestartPolicy", {"Name": "unless-stopped"}),
            environment=config.get("Env", []),
            ports=host_config.get("PortBindings", {}),
            binds=host_config.get("Binds"),
            network_mode=host_config.get("NetworkMode", "bridge"),
        )
        log.info("Started %s as %s", name, new_c.id[:12])

        # Re-check state
        check_container(name, image_tag)
        return {"ok": True}

    except Exception as e:
        log.error("Error updating %s: %s", name, e)
        return {"ok": False, "error": str(e)}
    finally:
        with state_lock:
            if name in state["updating"]:
                state["updating"].remove(name)


def update_all():
    """Update every container that has an available update (except self)."""
    with state_lock:
        targets = [
            name for name, info in state["containers"].items()
            if info["status"] == "update-available" and name != SELF_NAME
        ]
    results = {}
    for name in targets:
        results[name] = update_container(name)
    return results


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(30)


def setup_schedules():
    schedule.clear()

    check_interval = state["check_schedule"]
    schedule.every(check_interval).minutes.do(
        lambda: threading.Thread(target=check_all, daemon=True).start()
    )
    log.info("Check schedule: every %d minutes", check_interval)

    update_interval = state["update_schedule"]
    if update_interval and int(update_interval) > 0:
        schedule.every(int(update_interval)).minutes.do(
            lambda: threading.Thread(target=update_all, daemon=True).start()
        )
        log.info("Auto-update schedule: every %d minutes", int(update_interval))
    else:
        log.info("Auto-update schedule: disabled")


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", version=VERSION, self_name=SELF_NAME)


@app.route("/api/state")
def api_state():
    with state_lock:
        return jsonify({
            "containers": state["containers"],
            "last_full_check": state["last_full_check"],
            "check_schedule": state["check_schedule"],
            "update_schedule": state["update_schedule"],
            "checking": state["checking"],
            "updating": state["updating"],
            "self_name": SELF_NAME,
        })


@app.route("/api/check", methods=["POST"])
def api_check():
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if name:
        with state_lock:
            info = state["containers"].get(name)
        if not info:
            for c in get_running_containers():
                if c.name == name:
                    threading.Thread(
                        target=check_container, args=(name, c.image.tags[0]), daemon=True
                    ).start()
                    return jsonify({"ok": True, "message": f"Checking {name}"})
            return jsonify({"ok": False, "error": "Container not found"}), 404
        threading.Thread(
            target=check_container, args=(name, info["image"]), daemon=True
        ).start()
        return jsonify({"ok": True, "message": f"Checking {name}"})
    else:
        if state["checking"]:
            return jsonify({"ok": False, "error": "Check already in progress"}), 409
        threading.Thread(target=check_all, daemon=True).start()
        return jsonify({"ok": True, "message": "Checking all containers"})


@app.route("/api/update", methods=["POST"])
def api_update():
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if name:
        if name == SELF_NAME:
            return jsonify({"ok": False, "error": "Cannot update dockwatch while it is running."}), 403
        threading.Thread(target=update_container, args=(name,), daemon=True).start()
        return jsonify({"ok": True, "message": f"Updating {name}"})
    else:
        threading.Thread(target=update_all, daemon=True).start()
        return jsonify({"ok": True, "message": "Updating all eligible containers"})


@app.route("/api/schedule", methods=["POST"])
def api_schedule():
    data = request.get_json(silent=True) or {}
    check_mins = data.get("check_schedule")
    update_mins = data.get("update_schedule")

    if check_mins is not None:
        with state_lock:
            state["check_schedule"] = int(check_mins)
    if update_mins is not None:
        with state_lock:
            state["update_schedule"] = int(update_mins) if str(update_mins).strip() else 0

    setup_schedules()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    setup_schedules()
    threading.Thread(target=run_scheduler, daemon=True).start()
    # Initial check on startup
    threading.Thread(target=check_all, daemon=True).start()
    app.run(host="0.0.0.0", port=8093, debug=False)
