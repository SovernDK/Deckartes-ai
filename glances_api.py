"""Home server (thinkpad) stats from the Glances REST API (v4), as short plain-text summaries.

Raw Glances output is mostly counters and raw byte values the model can't use (the
containers endpoint alone is ~8,000 tokens), and small models misread bytes, so every
function here returns a few lines with readable units: about 10-30 tokens per fact.
Glances runs in a container on the server, so file systems are listed once per device
(its mount points are bind mounts like /etc/hosts).
"""

import asyncio
from typing import Any

import httpx

from config import config

GB = 1024 ** 3
# Containers listed in the overview, busiest first
OVERVIEW_CONTAINERS = 3

class GlancesUnreachableError(Exception):
    def __init__(self, cause: Exception) -> None:
        super().__init__("Could not reach the Glances API")
        self.__cause__ = cause

class GlancesStatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"Glances API responded with status {status}")
        self.status = status

async def _get(endpoint: str) -> Any:
    """GET {config.glances_host}/api/4/{endpoint} as JSON; raise a Glances error on failure."""
    url = f"{config.glances_host}/api/4/{endpoint}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(url)
    except httpx.HTTPError as e:
        raise GlancesUnreachableError(e) from e

    if res.status_code != 200:
        raise GlancesStatusError(res.status_code)

    return res.json()

def _gb(value: float) -> str:
    return f"{value / GB:.1f} GB"

def _size(value: float) -> str:
    """GB, or MB below 1 GB so small containers don't all read 0.0 GB."""
    return _gb(value) if value >= GB else f"{value / 1024 ** 2:.0f} MB"

def _percent(value: float) -> str:
    return f"{value:.1f}%"

# Each formatter takes the endpoint's JSON, so the overview can reuse them

def _format_cpu(cpu: dict) -> str:
    parts = [f"user {_percent(cpu['user'])}", f"system {_percent(cpu['system'])}"]
    # Only worth mentioning when the CPU waits on disk or the host
    for key in ("iowait", "steal"):
        if cpu.get(key, 0) >= 1:
            parts.append(f"{key} {_percent(cpu[key])}")
    return f"CPU: {_percent(cpu['total'])} used ({', '.join(parts)}), {cpu['cpucore']} cores"

def _format_load(load: dict) -> str:
    cores = load["cpucore"]
    return (f"Load average: {load['min1']:.2f} (1 min), {load['min5']:.2f} (5 min), "
            f"{load['min15']:.2f} (15 min) on {cores} cores = {load['min5'] / cores:.0%} of capacity")

def _format_mem(mem: dict) -> str:
    return (f"RAM: {_gb(mem['used'])} of {_gb(mem['total'])} used ({_percent(mem['percent'])}), "
            f"{_gb(mem['available'])} available")

def _format_swap(swap: dict) -> str:
    if not swap.get("total"):
        return "Swap: none"
    return f"Swap: {_gb(swap['used'])} of {_gb(swap['total'])} used ({_percent(swap['percent'])})"

def _format_fs(filesystems: list) -> str:
    lines = []
    seen = set()
    for fs in filesystems:
        if fs["device_name"] in seen:
            continue
        seen.add(fs["device_name"])
        lines.append(f"Disk {fs['device_name']} ({fs['fs_type']}): {_gb(fs['used'])} of {_gb(fs['size'])} "
                     f"used ({_percent(fs['percent'])}), {_gb(fs['free'])} free")
    return "\n".join(lines) or "No file systems reported"

def _format_gpu(gpus: list) -> str:
    lines = []
    for gpu in gpus:
        parts = [f"{gpu['proc']}% busy" if gpu.get("proc") is not None else "load unknown"]
        if gpu.get("mem") is not None:
            parts.append(f"memory {gpu['mem']}% used")
        if gpu.get("temperature") is not None:
            parts.append(f"{gpu['temperature']}°C")
        lines.append(f"GPU {gpu.get('name') or gpu.get('gpu_id')}: {', '.join(parts)}")
    return "\n".join(lines) or "No GPU reported"

def _format_sensors(sensors: list) -> str:
    temps = [s for s in sensors if s["type"].startswith("temperature") and s["value"]]
    lines = []
    if temps:
        hottest = max(temps, key=lambda s: s["value"])
        readings = ", ".join(f"{s['label']}: {s['value']}°C" for s in temps)
        # Without the limit, models guess one ("near its limit" at 43°C)
        limits = [s["warning"] for s in temps if s.get("warning")]
        limit = f", warning level {min(limits)}°C" if limits else ""
        lines.append(f"Temperatures: hottest {hottest['value']}°C ({hottest['label']}){limit}; {readings}")
        hot = [s for s in temps if s.get("warning") and s["value"] >= s["warning"]]
        if hot:
            lines.append("Above warning level: " + ", ".join(s["label"] for s in hot))
    fans = [s for s in sensors if s["type"] == "fan_speed"]
    if fans:
        lines.append("Fans: " + ", ".join(f"{s['label']}: {s['value']} RPM" for s in fans))
    for s in sensors:
        if s["type"] == "battery":
            lines.append(f"Battery: {s['value']}%")
    return "\n".join(lines) or "No sensors reported"

def _common_uptime(containers: list) -> str | None:
    uptimes = [c.get("uptime") for c in containers]
    common = max(set(uptimes), key=uptimes.count, default=None)
    return common if common and uptimes.count(common) > len(uptimes) / 2 else None

def _container_line(c: dict, common_uptime: str | None = None) -> str:
    """Uptime is left out when it's the one most containers share (given in the summary)."""
    uptime = "" if c.get("uptime") == common_uptime else f", up {c.get('uptime') or '?'}"
    return (f"{c['name']}: {c['status']}{uptime}, CPU {c.get('cpu_percent') or 0:.1f}%, "
            f"RAM {_size(c.get('memory_usage') or 0)}")

def _problem(c: dict) -> bool:
    return c["status"] not in ("running", "healthy")

def _containers_summary(containers: list) -> str:
    counts = {}
    for c in containers:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    states = ", ".join(f"{n} {status}" for status, n in sorted(counts.items(), key=lambda x: -x[1]))
    common = _common_uptime(containers)
    uptime = f"; up {common} unless noted" if common else ""
    return f"{len(containers)} containers ({states}){uptime}"

def _format_containers(containers: list) -> str:
    """Problem containers first, then busiest first."""
    ordered = sorted(containers, key=lambda c: (not _problem(c), -(c.get("cpu_percent") or 0)))
    common = _common_uptime(containers)
    return "\n".join([_containers_summary(containers), *(_container_line(c, common) for c in ordered)])

async def status() -> str:
    version, uptime = await asyncio.gather(_get("status"), _get("uptime"))
    return f"Online (Glances {version.get('version', '?')}), up {uptime}"

async def cpu() -> str:
    return _format_cpu(await _get("cpu"))

async def load() -> str:
    return _format_load(await _get("load"))

async def mem() -> str:
    return _format_mem(await _get("mem"))

async def memswap() -> str:
    return _format_swap(await _get("memswap"))

async def fs() -> str:
    return _format_fs(await _get("fs"))

async def gpu() -> str:
    return _format_gpu(await _get("gpu"))

async def sensors() -> str:
    return _format_sensors(await _get("sensors"))

async def containers() -> str:
    return _format_containers(await _get("containers"))

async def overview() -> str:
    """Every key number in one result, so "how is the server?" takes a single tool call."""
    (version, uptime, cpu_data, load_data, mem_data, swap_data, fs_data, gpu_data,
     sensor_data, container_data) = await asyncio.gather(*map(_get, [
        "status", "uptime", "cpu", "load", "mem", "memswap", "fs", "gpu", "sensors", "containers"]))
    busiest = sorted(container_data, key=lambda c: -(c.get("cpu_percent") or 0))[:OVERVIEW_CONTAINERS]
    biggest = sorted(container_data, key=lambda c: -(c.get("memory_usage") or 0))[:OVERVIEW_CONTAINERS]
    problems = [c for c in container_data if _problem(c)]
    common = _common_uptime(container_data)
    lines = [
        f"Online (Glances {version.get('version', '?')}), up {uptime}",
        _format_cpu(cpu_data), _format_load(load_data), _format_mem(mem_data), _format_swap(swap_data),
        _format_fs(fs_data), _format_gpu(gpu_data), _format_sensors(sensor_data),
        _containers_summary(container_data),
        *(f"Not running: {_container_line(c, common)}" for c in problems),
        "Most CPU: " + "; ".join(f"{c['name']} {c.get('cpu_percent') or 0:.1f}%" for c in busiest),
        "Most RAM: " + "; ".join(f"{c['name']} {_size(c.get('memory_usage') or 0)}" for c in biggest),
    ]
    return "\n".join(lines)
