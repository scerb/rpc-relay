run with docker compose up --build for first start. then docker compose up/down

JSON‑RPC Proxying
Forwards any JSON‑RPC call to one of your upstream endpoints, preserving request/response shapes.

Dynamic Config Reload
Every 30 s (throttled), re‑reads config.yaml on disk—so you can add/remove endpoints, change weights, adjust TTLs or thresholds without restarting.

HTTP Connection Pooling
Uses a requests.Session with up to 100 concurrent connections to fan out many RPC calls in parallel.

Health Monitoring
Tracks each endpoint’s latency, TPS (per‑second), TPM (per‑minute) against per‑endpoint thresholds. Marks endpoints healthy vs. throttled.

Rich Terminal Dashboard
Background thread renders:

Banner: total calls, cache hits, hit rate

Table: each RPC’s latency, TPS/TPM, healthy status, with footer totals.

TTL Cache
In‑memory cache for configured methods (e.g. eth_getBlockByNumber, price calls, etc.). Keyed by (method, params_json), with per‑method TTLs.

Rate‑Limit Enforcement
Before sending any request, waits until at least one healthy endpoint is under its TPS cap (sliding 60 s window).

Weighted & Latency‑Aware Load‑Balancing

Primary vs. Secondary: prefer primary endpoints when healthy

Weights: replicate each URL by its weight for round‑robin bias

Latency Threshold: if configured, only choose from endpoints below that ms threshold; otherwise pick lowest‑latency.

Nonce Management

Forced “pending” for all eth_getTransactionCount calls (even if client asked “latest”) so nonces include in‑flight txs.



rpc_endpoints.primary / .secondary
Lists of upstream URLs.

weight
Higher weight → more heavily favoured in round‑robin.

max_tps / max_tpm / max_latency_ms
Per‑endpoint health thresholds.

relay.monitor_interval
How often (s) to poll and update health metrics.

relay.latency_threshold_ms
Global filter for load‑balancer.

cache_ttl
Map of method names → TTL in seconds; only those methods are cached.
