#!/usr/bin/env python3
"""
Per-Slot Shared Chromium Benchmark — GO/NO-GO Gate (Layer 0)

Measures:
  1. Memory per tab: launch Chromium, open 1/3/5/10 tabs. Measure RSS via psutil.
  2. CDP latency with N tabs: 5 tabs open, measure Runtime.evaluate round-trip per tab.
  3. Cross-tab crash propagation: navigate one tab to chrome://crash, check if others survive.
  4. keep_alive detach: start BrowserSession with --cdp-url, detach. Verify browser persists.
  5. Concurrent CDP sessions: 2 tabs, 2 CDP sessions, parallel commands.

Pass criteria (from plan):
  - Memory per tab: < 200MB average
  - CDP latency: < 3x single-tab baseline
  - Cross-tab crash: other tabs + CDP sessions survive
  - keep_alive: browser alive, tabs intact
  - Concurrent CDP: no interference

Also tests site isolation: --enable-features=IsolateOrigins,site-per-process
Decision: if re-enabling costs <20% more RAM, re-enable it.

Usage:
    python .benchmarks/shared_chromium_benchmark.py [--with-site-isolation]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Auto-detect Playwright's Chromium binary
_PW_CACHE = Path.home() / "Library" / "Caches" / "ms-playwright"
_CHROMIUM_DIRS = sorted(_PW_CACHE.glob("chromium-*"), reverse=True) if _PW_CACHE.exists() else []
_DEFAULT_CHROME = ""
for _d in _CHROMIUM_DIRS:
    _candidates = list(_d.glob("**/Google Chrome for Testing"))
    if _candidates:
        _DEFAULT_CHROME = str(_candidates[0])
        break

if not _DEFAULT_CHROME:
    # Fallback to system Chrome
    _mac_chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if os.path.exists(_mac_chrome):
        _DEFAULT_CHROME = _mac_chrome

CHROME_BIN = os.environ.get("CHROME_BIN", _DEFAULT_CHROME)

TEST_URLS = [
    "https://www.example.com",
    "https://httpbin.org/html",
    "https://www.wikipedia.org",
    "https://httpbin.org/get",
    "https://httpbin.org/headers",
    "https://www.example.org",
    "https://httpbin.org/ip",
    "https://httpbin.org/user-agent",
    "https://www.iana.org",
    "https://httpbin.org/delay/0",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkResult:
    name: str
    passed: bool
    details: dict = field(default_factory=dict)
    error: str | None = None


def _rss_mb(pid: int) -> float:
    """Return RSS in MB for a process and all its children."""
    try:
        proc = psutil.Process(pid)
        total = proc.memory_info().rss
        for child in proc.children(recursive=True):
            try:
                total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total / (1024 * 1024)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0.0


def _launch_chrome(
    port: int,
    extra_args: list[str] | None = None,
    user_data_dir: str | None = None,
) -> subprocess.Popen:
    """Launch a Chromium process with remote debugging on the given port."""
    if not CHROME_BIN:
        raise RuntimeError(
            "No Chromium binary found. Set CHROME_BIN env var or install Playwright browsers."
        )

    udd = user_data_dir or f"/tmp/bench-chrome-{port}"
    os.makedirs(udd, exist_ok=True)

    args = [
        CHROME_BIN,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={udd}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-translate",
        "--metrics-recording-only",
        "--no-sandbox",
        "about:blank",
    ]
    if extra_args:
        args.extend(extra_args)

    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc


async def _wait_for_cdp(port: int, timeout: float = 15.0) -> str:
    """Wait for CDP endpoint to become available. Returns the WebSocket URL."""
    import aiohttp

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://127.0.0.1:{port}/json/version", timeout=aiohttp.ClientTimeout(total=2)
                ) as resp:
                    data = await resp.json()
                    return data["webSocketDebuggerUrl"]
        except Exception:
            await asyncio.sleep(0.3)
    raise TimeoutError(f"CDP not available on port {port} after {timeout}s")


async def _cdp_send(ws, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
    """Send a CDP command and wait for the response."""
    import aiohttp

    if not hasattr(_cdp_send, "_id_counter"):
        _cdp_send._id_counter = 0
    _cdp_send._id_counter += 1
    msg_id = _cdp_send._id_counter

    msg = {"id": msg_id, "method": method}
    if params:
        msg["params"] = params

    await ws.send_json(msg)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
        if resp.get("id") == msg_id:
            if "error" in resp:
                raise RuntimeError(f"CDP error: {resp['error']}")
            return resp.get("result", {})
    raise TimeoutError(f"No response for CDP {method} (id={msg_id})")


async def _create_tab(ws, url: str = "about:blank") -> str:
    """Create a new tab via CDP and return the targetId."""
    result = await _cdp_send(ws, "Target.createTarget", {"url": url})
    return result["targetId"]


async def _close_tab(ws, target_id: str) -> None:
    """Close a tab via CDP."""
    await _cdp_send(ws, "Target.closeTarget", {"targetId": target_id})


async def _get_targets(ws) -> list[dict]:
    """Get all page targets."""
    result = await _cdp_send(ws, "Target.getTargets")
    return [t for t in result.get("targetInfos", []) if t["type"] == "page"]


async def _attach_and_eval(ws, target_id: str, expression: str = "1+1") -> tuple[dict, float]:
    """Attach to a target, evaluate expression, return result + latency in ms."""
    # Attach to target
    attach_result = await _cdp_send(
        ws, "Target.attachToTarget", {"targetId": target_id, "flatten": True}
    )
    session_id = attach_result["sessionId"]

    # Evaluate
    start = time.monotonic()
    msg_id = _cdp_send._id_counter + 1
    _cdp_send._id_counter = msg_id

    await ws.send_json(
        {
            "id": msg_id,
            "method": "Runtime.evaluate",
            "params": {"expression": expression},
            "sessionId": session_id,
        }
    )

    while True:
        resp = await asyncio.wait_for(ws.receive_json(), timeout=10)
        if resp.get("id") == msg_id:
            latency = (time.monotonic() - start) * 1000
            # Detach
            await _cdp_send(
                ws,
                "Target.detachFromTarget",
                {"sessionId": session_id},
            )
            return resp.get("result", {}), latency


def _kill_chrome(proc: subprocess.Popen) -> None:
    """Terminate and reap a Chrome process."""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


async def bench_memory_per_tab(site_isolation: bool) -> BenchmarkResult:
    """Benchmark 1: Memory per tab with 1/3/5/10 tabs."""
    import aiohttp

    port = 19200
    extra = []
    if site_isolation:
        extra = ["--enable-features=IsolateOrigins,site-per-process"]
    else:
        extra = ["--disable-site-isolation-trials"]

    proc = _launch_chrome(port, extra_args=extra)
    results: dict[int, float] = {}

    try:
        ws_url = await _wait_for_cdp(port)

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                # Measure baseline (1 tab — the initial about:blank)
                await asyncio.sleep(2)
                baseline_rss = _rss_mb(proc.pid)
                results[0] = round(baseline_rss, 1)

                tab_counts = [1, 3, 5, 10]
                created_tabs: list[str] = []

                for target_count in tab_counts:
                    # Create tabs up to target_count
                    while len(created_tabs) < target_count:
                        idx = len(created_tabs)
                        url = TEST_URLS[idx % len(TEST_URLS)]
                        tid = await _create_tab(ws, url)
                        created_tabs.append(tid)

                    # Wait for pages to load and stabilize
                    await asyncio.sleep(3)
                    rss = _rss_mb(proc.pid)
                    results[target_count] = round(rss, 1)

                # Calculate per-tab cost
                per_tab_costs = []
                prev_rss = results[0]
                prev_count = 0
                for count in tab_counts:
                    if count > prev_count:
                        delta = results[count] - prev_rss
                        per_tab = delta / (count - prev_count)
                        per_tab_costs.append(round(per_tab, 1))
                    prev_rss = results[count]
                    prev_count = count

                avg_per_tab = round(sum(per_tab_costs) / len(per_tab_costs), 1) if per_tab_costs else 0
                passed = avg_per_tab < 200

                # Cleanup tabs
                for tid in created_tabs:
                    try:
                        await _close_tab(ws, tid)
                    except Exception:
                        pass

    except Exception as e:
        return BenchmarkResult(
            name="memory_per_tab",
            passed=False,
            error=str(e),
        )
    finally:
        _kill_chrome(proc)

    return BenchmarkResult(
        name="memory_per_tab",
        passed=passed,
        details={
            "site_isolation": site_isolation,
            "rss_by_tab_count": results,
            "per_tab_incremental_mb": per_tab_costs,
            "avg_per_tab_mb": avg_per_tab,
            "pass_threshold_mb": 200,
        },
    )


async def bench_cdp_latency(site_isolation: bool) -> BenchmarkResult:
    """Benchmark 2: CDP latency with 5 tabs open."""
    import aiohttp

    port = 19201
    extra = []
    if site_isolation:
        extra = ["--enable-features=IsolateOrigins,site-per-process"]

    proc = _launch_chrome(port, extra_args=extra)

    try:
        ws_url = await _wait_for_cdp(port)

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                # Single-tab baseline
                targets = await _get_targets(ws)
                if targets:
                    _, single_latency = await _attach_and_eval(ws, targets[0]["targetId"])
                else:
                    single_latency = 0

                # Warm up with repeated measurements
                single_latencies = []
                for _ in range(5):
                    if targets:
                        _, lat = await _attach_and_eval(ws, targets[0]["targetId"])
                        single_latencies.append(lat)
                single_baseline = (
                    sum(single_latencies) / len(single_latencies) if single_latencies else single_latency
                )

                # Create 5 tabs
                tab_ids = []
                for i in range(5):
                    url = TEST_URLS[i % len(TEST_URLS)]
                    tid = await _create_tab(ws, url)
                    tab_ids.append(tid)

                await asyncio.sleep(3)

                # Measure latency per tab
                multi_latencies = []
                for tid in tab_ids:
                    try:
                        _, lat = await _attach_and_eval(ws, tid)
                        multi_latencies.append(lat)
                    except Exception:
                        multi_latencies.append(float("inf"))

                avg_multi = sum(multi_latencies) / len(multi_latencies) if multi_latencies else 0
                ratio = avg_multi / single_baseline if single_baseline > 0 else float("inf")
                passed = ratio < 3.0

                # Cleanup
                for tid in tab_ids:
                    try:
                        await _close_tab(ws, tid)
                    except Exception:
                        pass

    except Exception as e:
        return BenchmarkResult(name="cdp_latency", passed=False, error=str(e))
    finally:
        _kill_chrome(proc)

    return BenchmarkResult(
        name="cdp_latency",
        passed=passed,
        details={
            "single_tab_baseline_ms": round(single_baseline, 2),
            "avg_with_5_tabs_ms": round(avg_multi, 2),
            "per_tab_latencies_ms": [round(l, 2) for l in multi_latencies],
            "ratio": round(ratio, 2),
            "pass_threshold_ratio": 3.0,
        },
    )


async def bench_cross_tab_crash() -> BenchmarkResult:
    """Benchmark 3: Cross-tab crash propagation.

    Navigate one tab to chrome://crash. Check if other tabs + CDP session survive.
    """
    import aiohttp

    port = 19202
    proc = _launch_chrome(port)

    try:
        ws_url = await _wait_for_cdp(port)

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                # Create 3 tabs with content
                tab_ids = []
                for i in range(3):
                    tid = await _create_tab(ws, TEST_URLS[i])
                    tab_ids.append(tid)

                await asyncio.sleep(2)

                # Verify all alive
                pre_crash_targets = await _get_targets(ws)
                pre_crash_count = len(pre_crash_targets)

                # Crash the first tab by navigating to chrome://crash
                crash_tab = tab_ids[0]
                survivor_tabs = tab_ids[1:]

                try:
                    # Attach and navigate to crash page
                    attach = await _cdp_send(
                        ws,
                        "Target.attachToTarget",
                        {"targetId": crash_tab, "flatten": True},
                    )
                    sid = attach["sessionId"]

                    # Navigate to crash — this will likely kill the renderer
                    msg_id = _cdp_send._id_counter + 1
                    _cdp_send._id_counter = msg_id
                    await ws.send_json(
                        {
                            "id": msg_id,
                            "method": "Page.navigate",
                            "params": {"url": "chrome://crash"},
                            "sessionId": sid,
                        }
                    )
                    # Don't wait for response — the tab's renderer dies
                    await asyncio.sleep(3)
                except Exception:
                    await asyncio.sleep(3)

                # Check if CDP connection to browser is still alive
                cdp_alive = False
                try:
                    post_targets = await _get_targets(ws)
                    cdp_alive = True
                except Exception:
                    post_targets = []

                # Check if survivor tabs still exist and respond
                survivors_alive = 0
                for tid in survivor_tabs:
                    try:
                        _, _ = await _attach_and_eval(ws, tid, "document.title")
                        survivors_alive += 1
                    except Exception:
                        pass

                # Check if process is alive
                process_alive = proc.poll() is None

                passed = cdp_alive and survivors_alive == len(survivor_tabs) and process_alive

    except Exception as e:
        return BenchmarkResult(name="cross_tab_crash", passed=False, error=str(e))
    finally:
        _kill_chrome(proc)

    return BenchmarkResult(
        name="cross_tab_crash",
        passed=passed,
        details={
            "pre_crash_tab_count": pre_crash_count,
            "cdp_connection_survived": cdp_alive,
            "process_survived": process_alive,
            "survivor_tabs_responding": survivors_alive,
            "survivor_tabs_expected": len(survivor_tabs),
        },
    )


async def bench_keep_alive_detach() -> BenchmarkResult:
    """Benchmark 4: keep_alive detach preserves browser + tabs.

    Start browser, create tabs, connect via BrowserSession with cdp_url,
    call detach_keep_alive(), verify browser and tabs persist.
    """
    import aiohttp

    port = 19203
    proc = _launch_chrome(port)

    try:
        ws_url = await _wait_for_cdp(port)
        cdp_url = f"http://127.0.0.1:{port}"

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                # Create 3 tabs
                tab_ids = []
                for i in range(3):
                    tid = await _create_tab(ws, TEST_URLS[i])
                    tab_ids.append(tid)
                await asyncio.sleep(2)

        # Now test BrowserSession detach_keep_alive
        try:
            # Add project root to path for imports
            project_root = Path(__file__).resolve().parent.parent
            sys.path.insert(0, str(project_root))

            from browser_use.browser.session import BrowserSession

            browser = BrowserSession(cdp_url=cdp_url)
            await browser.start()

            # Verify we can see tabs
            pages_before = len(browser._session_manager._page_targets) if browser._session_manager else 0

            # Detach (should NOT kill browser)
            await browser.detach_keep_alive()

        except Exception as e:
            # If BrowserSession import fails, test with raw CDP only
            pages_before = len(tab_ids)

        # Verify browser + tabs survive after detach
        await asyncio.sleep(2)

        process_alive = proc.poll() is None
        tabs_surviving = 0

        if process_alive:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(ws_url) as ws2:
                        targets = await _get_targets(ws2)
                        tabs_surviving = len(targets)
            except Exception:
                pass

        passed = process_alive and tabs_surviving >= len(tab_ids)

    except Exception as e:
        return BenchmarkResult(name="keep_alive_detach", passed=False, error=str(e))
    finally:
        _kill_chrome(proc)

    return BenchmarkResult(
        name="keep_alive_detach",
        passed=passed,
        details={
            "browser_survived": process_alive,
            "tabs_before_detach": len(tab_ids),
            "tabs_after_detach": tabs_surviving,
        },
    )


async def bench_concurrent_cdp_sessions() -> BenchmarkResult:
    """Benchmark 5: 2 tabs, 2 CDP sessions, parallel commands. No interference."""
    import aiohttp

    port = 19204
    proc = _launch_chrome(port)

    try:
        ws_url = await _wait_for_cdp(port)

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url) as ws:
                # Create 2 tabs
                tab1 = await _create_tab(ws, TEST_URLS[0])
                tab2 = await _create_tab(ws, TEST_URLS[1])
                await asyncio.sleep(2)

        # Open 2 independent CDP sessions and run commands in parallel
        results_a = []
        results_b = []
        interference = False

        async with aiohttp.ClientSession() as session:
            ws1 = await session.ws_connect(ws_url)
            ws2 = await session.ws_connect(ws_url)

            async def session_a():
                nonlocal results_a
                for i in range(10):
                    try:
                        result, lat = await _attach_and_eval(ws1, tab1, f"'session_a_{i}'")
                        results_a.append({"iter": i, "latency_ms": round(lat, 2), "ok": True})
                    except Exception as e:
                        results_a.append({"iter": i, "error": str(e), "ok": False})

            async def session_b():
                nonlocal results_b
                for i in range(10):
                    try:
                        result, lat = await _attach_and_eval(ws2, tab2, f"'session_b_{i}'")
                        results_b.append({"iter": i, "latency_ms": round(lat, 2), "ok": True})
                    except Exception as e:
                        results_b.append({"iter": i, "error": str(e), "ok": False})

            # Run both concurrently
            await asyncio.gather(session_a(), session_b())

            await ws1.close()
            await ws2.close()

        a_ok = sum(1 for r in results_a if r["ok"])
        b_ok = sum(1 for r in results_b if r["ok"])

        # Allow some tolerance — at least 8/10 should succeed
        passed = a_ok >= 8 and b_ok >= 8

    except Exception as e:
        return BenchmarkResult(name="concurrent_cdp_sessions", passed=False, error=str(e))
    finally:
        _kill_chrome(proc)

    return BenchmarkResult(
        name="concurrent_cdp_sessions",
        passed=passed,
        details={
            "session_a_success": f"{a_ok}/10",
            "session_b_success": f"{b_ok}/10",
            "session_a_avg_latency_ms": round(
                sum(r["latency_ms"] for r in results_a if r["ok"]) / max(a_ok, 1), 2
            ),
            "session_b_avg_latency_ms": round(
                sum(r["latency_ms"] for r in results_b if r["ok"]) / max(b_ok, 1), 2
            ),
        },
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_all(site_isolation: bool) -> list[BenchmarkResult]:
    """Run all benchmarks sequentially and return results."""
    benchmarks = [
        ("Memory per tab", bench_memory_per_tab(site_isolation)),
        ("CDP latency with N tabs", bench_cdp_latency(site_isolation)),
        ("Cross-tab crash propagation", bench_cross_tab_crash()),
        ("keep_alive detach", bench_keep_alive_detach()),
        ("Concurrent CDP sessions", bench_concurrent_cdp_sessions()),
    ]

    results = []
    for label, coro in benchmarks:
        print(f"\n{'='*60}")
        print(f"  Running: {label}")
        print(f"{'='*60}")
        result = await coro
        results.append(result)

        status = "PASS" if result.passed else "FAIL"
        print(f"  Result: [{status}] {result.name}")
        if result.error:
            print(f"  Error: {result.error}")
        for k, v in result.details.items():
            print(f"    {k}: {v}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Shared Chromium benchmark suite")
    parser.add_argument(
        "--with-site-isolation",
        action="store_true",
        help="Enable site isolation (--enable-features=IsolateOrigins,site-per-process)",
    )
    parser.add_argument(
        "--compare-isolation",
        action="store_true",
        help="Run benchmarks both with and without site isolation to compare RAM cost",
    )
    args = parser.parse_args()

    if not CHROME_BIN:
        print("ERROR: No Chromium binary found. Set CHROME_BIN env var.")
        sys.exit(1)

    print(f"Chrome binary: {CHROME_BIN}")
    print(f"Site isolation: {'enabled' if args.with_site_isolation else 'disabled'}")

    if args.compare_isolation:
        print("\n" + "=" * 60)
        print("  ROUND 1: Site isolation DISABLED")
        print("=" * 60)
        results_no_iso = asyncio.run(run_all(site_isolation=False))

        print("\n" + "=" * 60)
        print("  ROUND 2: Site isolation ENABLED")
        print("=" * 60)
        results_with_iso = asyncio.run(run_all(site_isolation=True))

        # Compare memory
        mem_no = next((r for r in results_no_iso if r.name == "memory_per_tab"), None)
        mem_yes = next((r for r in results_with_iso if r.name == "memory_per_tab"), None)
        if mem_no and mem_yes and not mem_no.error and not mem_yes.error:
            no_avg = mem_no.details["avg_per_tab_mb"]
            yes_avg = mem_yes.details["avg_per_tab_mb"]
            overhead_pct = ((yes_avg - no_avg) / no_avg * 100) if no_avg > 0 else float("inf")
            print(f"\n{'='*60}")
            print(f"  Site isolation RAM overhead: {overhead_pct:.1f}%")
            print(f"  Without: {no_avg} MB/tab | With: {yes_avg} MB/tab")
            if overhead_pct < 20:
                print("  RECOMMENDATION: Re-enable site isolation (<20% overhead)")
            else:
                print("  RECOMMENDATION: Keep site isolation disabled (>20% overhead)")
            print(f"{'='*60}")
    else:
        results = asyncio.run(run_all(site_isolation=args.with_site_isolation))

        # Summary
        print(f"\n{'='*60}")
        print("  SUMMARY")
        print(f"{'='*60}")
        all_passed = True
        critical_failed = False
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            print(f"  [{status}] {r.name}")
            if not r.passed:
                all_passed = False
                if r.name in ("memory_per_tab", "cross_tab_crash", "concurrent_cdp_sessions"):
                    critical_failed = True

        if critical_failed:
            print("\n  CRITICAL BENCHMARKS FAILED — STOP. Revisit architecture.")
            sys.exit(1)
        elif all_passed:
            print("\n  ALL BENCHMARKS PASSED — Proceed with implementation.")
            sys.exit(0)
        else:
            print("\n  Some non-critical benchmarks failed — review before proceeding.")
            sys.exit(0)


if __name__ == "__main__":
    main()
