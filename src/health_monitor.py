# src/health_monitor.py

import time
import yaml
from pathlib import Path
from typing import Any, Dict, List
from collections import deque

from web3 import Web3, HTTPProvider
from rich.table import Table

class RPCStatusMonitor:
    def __init__(self, config: Dict[str, Any]):
        # Path to the same config.yaml (for dynamic reload)
        self.config_file = Path(__file__).resolve().parent.parent / 'config.yaml'
        self.config = config or {}

        # Load initial endpoints + thresholds
        self.rpcs: List[Dict[str, Any]] = []
        self._build_initial_rpc_list()

        # Throttle config reloads to once every 30 s
        self._last_reload_time = 0
        self._last_config_snapshot = config.copy()

        # Metrics
        self.total_calls = 0
        self.cached_calls = 0

        # Column widths for the table (can be overridden in config.yaml)
        self.col_widths = {
            'url': 43,
            'status': 11,
            'behind': 6,
            'block': 13,
            'latency': 10,
            'tps': 6,
            'tpm': 6,
            'errors': 6,
            'calls': 9
        }

        # Max blocks behind threshold (default = 6 if not in config)
        self.max_behind = self.config.get('health_monitor', {})\
                             .get('max_blocks_behind', 6)

    def _build_initial_rpc_list(self) -> None:
        """
        Build self.rpcs from config.yaml’s rpc_endpoints (primary + secondary).
        Each endpoint dict contains:
          - url (str)
          - max_tps (int)
          - healthy (bool)
          - behind (int)
          - latest_block (int)
          - latency (float)
          - errors (int)
          - call_count (int)
          - timestamps (deque) for rate limiting
        """
        primary = self.config.get('rpc_endpoints', {}).get('primary', [])
        secondary = self.config.get('rpc_endpoints', {}).get('secondary', [])
        all_endpoints = primary + secondary

        self.rpcs = []
        for ep in all_endpoints:
            url = ep.get('url')
            if url:
                self.rpcs.append({
                    'url': url,
                    'max_tps': ep.get('max_tps', 0),
                    'healthy': True,
                    'behind': 0,
                    'latest_block': 0,
                    'latency': float('inf'),
                    'errors': 0,
                    'call_count': 0,
                    'timestamps': deque()
                })

    def _reload_config(self) -> None:
        """
        Reload config.yaml from disk if it has changed (throttled to once every 30 s).
        If rpc_endpoints or thresholds changed, update self.rpcs accordingly.
        """
        now = time.time()
        if now - self._last_reload_time < 30:
            return

        try:
            with open(self.config_file, 'r') as f:
                new_cfg = yaml.safe_load(f)
        except Exception:
            # If config.yaml cannot be read, just skip
            return

        # Compare with last snapshot
        if new_cfg != self._last_config_snapshot:
            self.config = new_cfg
            self._last_config_snapshot = new_cfg.copy()
            self.max_behind = new_cfg.get('health_monitor', {})\
                                .get('max_blocks_behind', self.max_behind)

            # Update column widths if changed
            col_cfg = self.config.get('health_monitor', {})\
                       .get('column_widths', {})
            for k, v in col_cfg.items():
                if k in self.col_widths:
                    self.col_widths[k] = v

            # Determine new endpoint map {url: max_tps}
            new_primary = self.config.get('rpc_endpoints', {}).get('primary', [])
            new_secondary = self.config.get('rpc_endpoints', {}).get('secondary', [])
            incoming = {
                ep.get('url'): ep.get('max_tps', 0)
                for ep in (new_primary + new_secondary) if ep.get('url')
            }

            existing_urls = {rpc['url'] for rpc in self.rpcs}

            # Step 1a: If URLs changed, rebuild entire list
            if set(incoming.keys()) != existing_urls:
                old_state = {
                    rpc['url']: {
                        'call_count': rpc['call_count'],
                        'timestamps': rpc['timestamps'],
                        'latest_block': rpc.get('latest_block', 0)
                    }
                    for rpc in self.rpcs
                }
                self._build_initial_rpc_list()
                # Restore call_count, timestamps, and latest_block for matching URLs
                for rpc in self.rpcs:
                    url = rpc['url']
                    if url in old_state:
                        rpc['call_count'] = old_state[url]['call_count']
                        rpc['timestamps'] = old_state[url]['timestamps']
                        rpc['latest_block'] = old_state[url]['latest_block']
            else:
                # Step 1b: If only max_tps changed, update those values
                for rpc in self.rpcs:
                    if rpc['url'] in incoming:
                        rpc['max_tps'] = incoming[rpc['url']]

        self._last_reload_time = now

    def update_statuses(self) -> None:
        """
        1) Possibly reload config.yaml (throttled).
        2) For each RPC endpoint: probe eth_blockNumber (timeout = 3 s).
           - If probe fails: mark healthy=False, behind=∞, increment errors.
           - If probe succeeds: record latest_block, latency, reset errors, set healthy=True.
        3) Compute max_block across all healthy endpoints.
        4) For each healthy RPC: compute behind = max_block - latest_block.
           If behind > max_blocks_behind → mark healthy=False, behind=∞.
        """
        self._reload_config()

        # Step 2: Probe each RPC endpoint
        temp_blocks: Dict[str, int] = {}
        block_numbers: List[int] = []
        for rpc in self.rpcs:
            url = rpc['url']
            try:
                w3 = Web3(HTTPProvider(url, request_kwargs={'timeout': 3}))
                start = time.time()
                latest_block = w3.eth.block_number
                latency = time.time() - start

                rpc['healthy'] = True
                rpc['latency'] = latency
                rpc['errors'] = 0
                rpc['latest_block'] = latest_block
                temp_blocks[url] = latest_block
                block_numbers.append(latest_block)
            except Exception:
                rpc['healthy'] = False
                rpc['latency'] = float('inf')
                rpc['behind'] = float('inf')
                rpc['errors'] += 1

        # Step 3: Compute max_block among healthy ones
        max_block = max(block_numbers) if block_numbers else 0

        # Step 4: Compute “behind” and enforce max_blocks_behind
        for rpc in self.rpcs:
            if rpc['healthy']:
                latest_block = temp_blocks.get(rpc['url'], 0)
                behind = max_block - latest_block
                rpc['behind'] = behind
                if behind > self.max_behind:
                    rpc['healthy'] = False
                    rpc['behind'] = float('inf')
            else:
                continue

    def get_healthy_rpcs(self) -> List[Dict[str, Any]]:
        """
        Return a list of healthy RPC dicts, sorted by (behind, latency).
        """
        healthy_list = [rpc for rpc in self.rpcs if rpc['healthy']]
        return sorted(healthy_list, key=lambda r: (r['behind'], r['latency']))

    def record_rpc_call(self, url: str) -> None:
        """
        Increment counters when the relay forwards a request to `url`:
          - self.total_calls
          - rpc['call_count'] for that endpoint
        """
        self.total_calls += 1
        for rpc in self.rpcs:
            if rpc['url'] == url:
                rpc['call_count'] += 1
                break

    def increment_metrics(self, cached: bool) -> None:
        """
        If a request was served from cache, increment self.cached_calls.
        """
        if cached:
            self.cached_calls += 1

    def _generate_table(self) -> Table:
        """
        Build a Rich Table with columns:
          URL | Status | >> (behind) | Block | Latency (ms) | TPS | TPM | Err | Calls
        """
        # Define the table and column headers
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("URL", width=self.col_widths.get('url', 43))
        table.add_column("Status", width=self.col_widths.get('status', 11))
        table.add_column(">>", justify="right", width=self.col_widths.get('behind', 6))
        table.add_column("Block", justify="right", width=self.col_widths.get('block', 13))
        table.add_column("Latency (ms)", justify="right", width=self.col_widths.get('latency', 10))
        table.add_column("TPS", justify="right", width=self.col_widths.get('tps', 6))
        table.add_column("TPM", justify="right", width=self.col_widths.get('tpm', 6))
        table.add_column("Err", justify="right", width=self.col_widths.get('errors', 6))
        table.add_column("Calls", justify="right", width=self.col_widths.get('calls', 9))

        now = time.time()
        for rpc in self.rpcs:
            status_str = "🟢 OK" if rpc['healthy'] else "🔴 DOWN"
            behind_str = str(rpc['behind']) if rpc['behind'] != float('inf') else "∞"
            block_str = str(rpc['latest_block']) if rpc.get('latest_block', 0) else "0"

            if rpc['latency'] != float('inf'):
                latency_ms = rpc['latency'] * 1000
                latency_str = f"{latency_ms:.1f}"
            else:
                latency_str = "∞"

            timestamps_snapshot = list(rpc['timestamps'])
            # Compute TPS by counting timestamps in the last 1 second          
            tps_count = sum(1 for ts in timestamps_snapshot if ts >= now - 1)
            tps_str = f"{tps_count}"

            # Compute TPM by counting timestamps in the last 60 seconds
            tpm_count = sum(1 for ts in timestamps_snapshot if ts >= now - 60)
            tpm_str = f"{tpm_count}"

            errors_str = str(rpc['errors'])
            calls_str = str(rpc['call_count'])

            table.add_row(
                rpc['url'],
                status_str,
                behind_str,
                block_str,
                latency_str,
                tps_str, tpm_str,
                errors_str,
                calls_str
            )
        return table
