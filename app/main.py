"""Web UI + API. Start with: python -m app.main  (port from WEBUI_PORT or Settings)."""
import os
import threading

from flask import Flask, jsonify, request, send_from_directory

from . import checks, comma, config, monitor, sshkeys
from .pipeline import pipeline

STATIC = os.path.join(os.path.dirname(__file__), "static")
app = Flask(__name__, static_folder=STATIC, static_url_path="")


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/api/status")
def api_status():
    snap = pipeline.snapshot()
    snap["gpu"] = monitor.gpu_snapshot()
    snap["net"] = monitor.net_snapshot()
    return jsonify(snap)


@app.get("/api/checks")
def api_checks():
    force = request.args.get("force") == "1"
    return jsonify(checks.run_all(force=force))


@app.get("/api/settings")
def api_settings_get():
    return jsonify(config.load().__dict__)


@app.post("/api/settings")
def api_settings_post():
    data = request.get_json(silent=True) or {}
    s = config.update(data)
    checks.run_all(force=True)
    return jsonify(s.__dict__)


@app.get("/api/key")
def api_key_get():
    ok, msg = sshkeys.check_local()
    return jsonify({
        "public_key": sshkeys.public_key(),
        "fingerprint": sshkeys.fingerprint(),
        "valid": ok,
        "message": msg,
    })


@app.post("/api/key/generate")
def api_key_generate():
    overwrite = bool((request.get_json(silent=True) or {}).get("overwrite"))
    try:
        sshkeys.generate(overwrite=overwrite)
    except RuntimeError as e:
        return jsonify({"ok": False, "message": str(e)}), 500
    checks.run_all(force=True)
    return api_key_get()


@app.post("/api/key/import")
def api_key_import():
    """Use an existing private key instead of the generated one."""
    text = (request.get_json(silent=True) or {}).get("private_key", "")
    try:
        sshkeys.import_private_key(text)
    except RuntimeError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    checks.run_all(force=True)
    return api_key_get()


@app.post("/api/test-connection")
def api_test_connection():
    ok, msg = comma.test_connection()
    checks.run_all(force=True)
    return jsonify({"ok": ok, "message": msg})


@app.post("/api/discover")
def api_discover():
    """Search the network for the comma now and save the address it answers on."""
    try:
        host = comma.resolve_host(force=True)
    except RuntimeError as e:
        checks.run_all(force=True)
        return jsonify({"ok": False, "message": str(e)})
    checks.run_all(force=True)
    s = config.load()
    return jsonify({"ok": True, "host": host,
                    "message": f"Found the comma at {host} ({s.comma_found_via})."})


@app.post("/api/sync")
def api_sync():
    pipeline.request_sync()
    return jsonify({"ok": True, "message": "Sync requested."})


@app.post("/api/stop")
def api_stop():
    pipeline.stop()
    return jsonify({"ok": True})


@app.get("/api/health")
def api_health():
    return jsonify({"ok": True})


def _bootstrap():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    s = config.load()
    os.makedirs(s.output_dir, exist_ok=True)
    try:
        sshkeys.generate()  # first run: create the app's own key
    except RuntimeError as e:
        pipeline.log(f"Could not create SSH key: {e}")
    monitor.start_background()
    threading.Thread(target=pipeline.encoder_loop, daemon=True, name="encoder").start()
    threading.Thread(target=pipeline.scheduler_loop, daemon=True, name="scheduler").start()


if __name__ == "__main__":
    _bootstrap()
    port = config.load().webui_port
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
