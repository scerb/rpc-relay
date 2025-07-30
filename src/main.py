# main.py

import json
import logging
import shutil
import time
import threading
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests
from requests.adapters import HTTPAdapter
import yaml
from flask import Flask, jsonify, request
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel

from health_monitor import RPCStatusMonitor

# -------------------------------
# 1) LOAD CONFIG.YAML (Shared by both Flask + Dashboard)
# -------------------------------
CONFIG_PATH = Path(__file__).resolve().parent.parent / 'config.yaml'
with open(CONFIG_PATH, 'r') as f:
    config: Dict[str, Any] = yaml.safe_load(f)

# -------------------------------
# 2) CONFIGURE OUTGOING HTTP POOL
# -------------------------------
session = requests.Session()
adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100)
session.mount('http://', adapter)
session.mount('https://', adapter)

# -------------------------------
# 3) INSTANTIATE HEALTH MONITOR
# -------------------------------
monitor = RPCStatusMonitor(config)
_last_status_time = 0.0

# -------------------------------
# 4) BUILD WEIGHTS FOR RPC URLs
# -------------------------------
def build_url_weights(cfg: Dict[str, Any]) -> Dict[str, int]:
    """
    Build a flat map of {rpc_url: weight_int}, where weight_int
    comes from config['rpc_endpoints'] and defaults to 1 if not specified.
    """
    url_weights: Dict[str, int] = {}
    prim = cfg.get('rpc_endpoints', {}).get('primary', [])
    sec  = cfg.get('rpc_endpoints', {}).get('secondary', [])
    for ep in prim + sec:
        url = ep.get('url')
        w   = ep.get('weight', 1)
        if url:
            url_weights[url] = w
    return url_weights

url_weights = build_url_weights(config)

# -------------------------------
# 5) TTL CACHE FOR JSON-RPC RESPONSES
# -------------------------------
# Format: { (method, json.dumps(params)): { "time": timestamp, "response": full_dict } }
_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
cache_ttl: Dict[str, float] = config.get("cache_ttl", {})

# -------------------------------
# 6) FLASK SETUP
# -------------------------------
app = Flask(__name__)

# Keep a copy of the last-loaded config dict for comparison
_last_config_snapshot   = config.copy()
_last_config_file_check = 0.0

def reload_config_if_changed() -> None:
    """
    If config.yaml on disk has changed since last read, reload into `config`,
    rebuild weights, inform `monitor` of new endpoints/thresholds,
    and pick up any cache_ttl changes. Throttled to once per 30 s.
    """
    global config, _last_config_file_check, _last_config_snapshot, url_weights, cache_ttl

    now = time.time()
    if now - _last_config_file_check < 30:
        return
    _last_config_file_check = now

    try:
        new_cfg = yaml.safe_load(open(CONFIG_PATH, 'r'))
    except Exception:
        return

    if new_cfg != _last_config_snapshot:
        # swap in the new config
        config               = new_cfg
        _last_config_snapshot = new_cfg.copy()

        # rebuild URL weights
        url_weights = build_url_weights(config)

        # inform the monitor of updated config
        monitor.config = config

        # pick up any cache-TTL changes
        cache_ttl = config.get("cache_ttl", {})
        if not cache_ttl:
            _cache.clear()

# -------------------------------
# 7) DASHBOARD (Runs in separate thread)
# -------------------------------
def terminal_dashboard():
    """
    Continuously update a single “live” region showing:
      1) Banner: “Total calls | Cache hits | Hit rate”
      2) The Rich table of endpoint status, with a footer row for totals.
    """
    interval = config.get("relay", {}).get("monitor_interval", 5)
    console  = Console(
        width=shutil.get_terminal_size(fallback=(140, 80)).columns,
        force_terminal=True,
        record=False,
    )
    layout = Layout()
    layout.split_column(
        Layout(name="banner", size=3),
        Layout(name="table", ratio=1),
    )

    while True:
        reload_config_if_changed()
        now = time.time()
        monitor.update_statuses()

        # Banner
        total = monitor.total_calls
        hits  = monitor.cached_calls
        rate  = (hits / total * 100) if total > 0 else 0
        banner = Panel(
            f" Total calls: {total} | Cache hits: {hits} | Hit rate: {rate:.1f}% ",
            style="bold white on blue",
            expand=True,
        )
        layout["banner"].update(banner)

        # Status table
        tbl = monitor._generate_table()

        # ── compute totals safely from raw data ──
        total_tps = 0
        total_tpm = 0
        for rpc in monitor.rpcs:
            try:
                timestamps = list(rpc['timestamps'])
            except RuntimeError:
                timestamps = []
            total_tps += sum(1 for ts in timestamps if ts >= now - 1)
            total_tpm += sum(1 for ts in timestamps if ts >= now - 60)
        total_calls = sum(rpc['call_count'] for rpc in monitor.rpcs)
        # ───────────────────────────────────────────

        # ── locate our columns by header name ──
        headers = [col.header for col in tbl.columns]
        try:
            idx_tps   = headers.index("TPS")
            idx_tpm   = headers.index("TPM")
            idx_calls = headers.index("Calls")
            tbl.show_footer = True
            tbl.columns[idx_tps].footer   = str(total_tps)
            tbl.columns[idx_tpm].footer   = str(total_tpm)
            tbl.columns[idx_calls].footer = str(total_calls)
        except ValueError:
            # if headers don’t match, skip footer
            pass
        # ───────────────────────────────────────────

        layout["table"].update(tbl)
        console.clear()
        console.print(layout)
        time.sleep(interval)

# start the dashboard thread
threading.Thread(target=terminal_dashboard, daemon=True).start()

# -------------------------------
# 8) FLASK ROUTES
# -------------------------------
@app.route("/status", methods=["GET"])
def status():
    """
    Returns JSON with the raw list of RPC endpoint statuses.
    Before returning, reload config if changed (throttled).
    """
    reload_config_if_changed()
    return jsonify({"rpcs": monitor.rpcs})


@app.route("/", methods=["GET"])
def relay_health_check():
    """
    Simple health-check for the relay service.
    """
    return jsonify({"status": "ok"}), 200


@app.route("/", methods=["POST"])
def relay():
    """
    Main JSON-RPC relay endpoint.  
    – Forces all eth_getTransactionCount calls to use "pending".  
    – Pre-checks and corrects nonces on eth_sendTransaction / personal_sendTransaction.  
    – Handles TTL cache, rate-limits, weighted LB, latency-based filtering, dashboard metrics.
    """
    global _last_status_time

    reload_config_if_changed()

    # Throttle update_statuses() to once every `monitor_interval` seconds
    now = time.time()
    monitor_interval = config.get("relay", {}).get("monitor_interval", 5)
    if now - _last_status_time >= monitor_interval:
        try:
            monitor.update_statuses()
        except Exception:
            logging.exception("Failed to update RPC statuses")
        _last_status_time = now

    data   = request.get_json(force=True)
    method = data.get("method", "")
    params = data.get("params", [])

    # ── NEW: Force pending for nonce queries ──
    if method == "eth_getTransactionCount" and isinstance(params, list) and len(params) >= 1:
        params = [params[0], "pending"]

    # ── TTL cache lookup ──
    if method in cache_ttl:
        key   = (method, json.dumps(params, sort_keys=True))
        entry = _cache.get(key)
        if entry and (time.time() - entry["time"] < cache_ttl[method]):
            monitor.increment_metrics(cached=True)
            entry["response"]["id"] = data.get("id", 0)
            return jsonify(entry["response"])

    # ── Wait for a healthy RPC under its rate limit ──
    while True:
        healthy_list = monitor.get_healthy_rpcs()
        if not healthy_list:
            return (
                jsonify({
                    "jsonrpc": "2.0",
                    "id": data.get("id", 0),
                    "error": {"code": -32000, "message": "No healthy RPCs available"},
                }),
                500,
            )

        now_ts = time.time()
        available_rpcs: List[Dict[str, Any]] = []
        for rpc in healthy_list:
            timestamps = rpc['timestamps']
            while timestamps and timestamps[0] < now_ts - 60:
                timestamps.popleft()

            tps_count = sum(1 for ts in list(timestamps) if ts >= now_ts - 1)
            max_tps   = rpc.get("max_tps", 0)

            if max_tps <= 0 or tps_count < max_tps:
                available_rpcs.append(rpc)

        if available_rpcs:
            break
        time.sleep(0.05)

    # ── Split primary vs secondary ──
    primaries = [
        r for r in available_rpcs
        if r["url"] in {ep["url"] for ep in config.get("rpc_endpoints", {}).get("primary", [])}
    ]
    secondaries = [
        r for r in available_rpcs
        if r["url"] in {ep["url"] for ep in config.get("rpc_endpoints", {}).get("secondary", [])}
    ]
    chosen_set = primaries if primaries else secondaries

    # ── Build weighted list ──
    weighted_list: List[Dict[str, Any]] = []
    for rpc in chosen_set:
        w = url_weights.get(rpc["url"], 1)
        weighted_list.extend([rpc] * w)

    # ── Latency-threshold filtering & round-robin ──
    lb_threshold_ms = config.get("relay", {}).get("latency_threshold_ms", None)
    if lb_threshold_ms is not None:
        under_threshold = [
            r for r in weighted_list
            if (r["latency"] * 1000) < lb_threshold_ms
        ]
        if under_threshold:
            candidates = under_threshold
        else:
            min_latency = min(r["latency"] for r in weighted_list)
            candidates  = [r for r in weighted_list if r["latency"] == min_latency]
    else:
        candidates = weighted_list

    idx = getattr(relay, "_lb_index", 0)
    selected_rpc = candidates[idx % len(candidates)]
    setattr(relay, "_lb_index", idx + 1)

    # ── Record the call ──
    selected_url = selected_rpc["url"]
    selected_rpc["timestamps"].append(time.time())
    monitor.record_rpc_call(selected_url)

    # ── PRE-CHECK NONCE LOGIC for JSON txs ──
    if method in ("eth_sendTransaction", "personal_sendTransaction") \
       and isinstance(params, list) and len(params) >= 1 and isinstance(params[0], dict):
        try:
            from_addr = params[0].get("from")
            if from_addr:
                count_payload = {
                    "jsonrpc": "2.0",
                    "id":      data.get("id", 0),
                    "method":  "eth_getTransactionCount",
                    "params":  [from_addr, "pending"],
                }
                count_resp = session.post(selected_url, json=count_payload, timeout=10)
                count_json = count_resp.json()
                correct_nonce = count_json.get("result")
                if correct_nonce:
                    current_nonce = params[0].get("nonce")
                    if current_nonce != correct_nonce:
                        params[0]["nonce"] = correct_nonce
        except Exception:
            logging.exception("Nonce pre-check failed; forwarding original tx")

    # ── Forward upstream via configured session ──
    try:
        payload           = {"jsonrpc": "2.0", "id": data.get("id", 0), "method": method, "params": params}
        resp              = session.post(selected_url, json=payload, timeout=30)
        upstream_response = resp.json()
    except Exception as e:
        return (
            jsonify({
                "jsonrpc": "2.0",
                "id": data.get("id", 0),
                "error": {"code": -32603, "message": f"Upstream provider error: {str(e)}"},
            }),
            500,
        )

    # ── Insert into TTL cache if applicable ──
    if method in cache_ttl:
        cache_key = (method, json.dumps(params, sort_keys=True))
        _cache[cache_key] = {"time": time.time(), "response": upstream_response}

    monitor.increment_metrics(cached=False)
    return jsonify(upstream_response)


# -------------------------------
# 9) RUN THE FLASK SERVER
# -------------------------------
if __name__ == "__main__":
    host = config.get("relay", {}).get("host", "0.0.0.0")
    port = config.get("relay", {}).get("port", 5000)
    app.run(host=host, port=port, threaded=True)
