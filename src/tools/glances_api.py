"""Home server (thinkpad) stats from the Glances REST API (v4), as short plain-text summaries.

Raw Glances output is mostly counters and raw byte values the model can't use (the
containers endpoint alone is ~8,000 tokens), and small models misread bytes, so every
function here returns a few lines with readable units: about 10-30 tokens per fact.
Glances runs in a container on the server, so file systems are listed once per device
(its mount points are bind mounts like /etc/hosts).
"""

import asyncio
from typing import Any

from core import http_client
from core.config import config

GB = 1024 ** 3
# Containers listed in the overview, busiest first
OVERVIEW_CONTAINERS = 3

class GlancesError(Exception):
    pass

async def _get(endpoint: str) -> Any:
    res = await http_client.request("GET", f"{config.glances_host}/api/4/{endpoint}",
                                    name="Glances", error=GlancesError, timeout=10)
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

def _format_gpu(reported: list) -> str:
    lines = []
    for card in reported:
        parts = [f"{card['proc']}% busy" if card.get("proc") is not None else "load unknown"]
        if card.get("mem") is not None:
            parts.append(f"memory {card['mem']}% used")
        if card.get("temperature") is not None:
            parts.append(f"{card['temperature']}°C")
        lines.append(f"GPU {card.get('name') or card.get('gpu_id')}: {', '.join(parts)}")
    return "\n".join(lines) or "No GPU reported"

def _format_sensors(reported: list) -> str:
    temps = [sensor for sensor in reported if sensor["type"].startswith("temperature") and sensor["value"]]
    lines = []
    if temps:
        hottest = max(temps, key=lambda sensor: sensor["value"])
        readings = ", ".join(f"{sensor['label']}: {sensor['value']}°C" for sensor in temps)
        # Without the limit, models guess one ("near its limit" at 43°C)
        warning_levels = [sensor["warning"] for sensor in temps if sensor.get("warning")]
        limit = f", warning level {min(warning_levels)}°C" if warning_levels else ""
        lines.append(f"Temperatures: hottest {hottest['value']}°C ({hottest['label']}){limit}; {readings}")
        hot = [sensor for sensor in temps if sensor.get("warning") and sensor["value"] >= sensor["warning"]]
        if hot:
            lines.append("Above warning level: " + ", ".join(sensor["label"] for sensor in hot))
    fan_sensors = [sensor for sensor in reported if sensor["type"] == "fan_speed"]
    if fan_sensors:
        lines.append("Fans: " + ", ".join(f"{fan['label']}: {fan['value']} RPM" for fan in fan_sensors))
    for sensor in reported:
        if sensor["type"] == "battery":
            lines.append(f"Battery: {sensor['value']}%")
    return "\n".join(lines) or "No sensors reported"

def _common_uptime(reported: list) -> str | None:
    uptimes = [container.get("uptime") for container in reported]
    common = max(set(uptimes), key=uptimes.count, default=None)
    return common if common and uptimes.count(common) > len(uptimes) / 2 else None

def _container_line(container: dict, common_uptime: str | None = None) -> str:
    """Uptime is left out when it's the one most containers share (given in the summary)."""
    uptime = "" if container.get("uptime") == common_uptime else f", up {container.get('uptime') or '?'}"
    return (f"{container['name']}: {container['status']}{uptime}, CPU {container.get('cpu_percent') or 0:.1f}%, "
            f"RAM {_size(container.get('memory_usage') or 0)}")

def _problem(container: dict) -> bool:
    return container["status"] not in ("running", "healthy")

def _containers_summary(reported: list) -> str:
    if not reported:
        return "No containers reported"
    tally = {}
    for container in reported:
        tally[container["status"]] = tally.get(container["status"], 0) + 1
    by_status = ", ".join(f"{count} {status}"
                          for status, count in sorted(tally.items(), key=lambda item: -item[1]))
    common = _common_uptime(reported)
    uptime = f"; up {common} unless noted" if common else ""
    return f"{len(reported)} containers ({by_status}){uptime}"

def _format_containers(reported: list) -> str:
    """Problem containers first, then busiest first."""
    ordered = sorted(reported, key=lambda container: (not _problem(container), -(container.get("cpu_percent") or 0)))
    common = _common_uptime(reported)
    return "\n".join([_containers_summary(reported), *(_container_line(container, common) for container in ordered)])

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
    (version, uptime, cpu_data, load_data, mem_data, swap_data, fs_data, graphics_data,
     sensor_data, container_data) = await asyncio.gather(*map(_get, [
        "status", "uptime", "cpu", "load", "mem", "memswap", "fs", "gpu", "sensors", "containers"]))
    busiest = sorted(container_data, key=lambda container: -(container.get("cpu_percent") or 0))[:OVERVIEW_CONTAINERS]
    biggest = sorted(container_data, key=lambda container: -(container.get("memory_usage") or 0))[:OVERVIEW_CONTAINERS]
    problems = [container for container in container_data if _problem(container)]
    common = _common_uptime(container_data)
    lines = [
        f"Online (Glances {version.get('version', '?')}), up {uptime}",
        _format_cpu(cpu_data), _format_load(load_data), _format_mem(mem_data), _format_swap(swap_data),
        _format_fs(fs_data), _format_gpu(graphics_data), _format_sensors(sensor_data),
        _containers_summary(container_data),
        *(f"Not running: {_container_line(container, common)}" for container in problems),
    ]
    if container_data:
        lines += ["Most CPU: " + "; ".join(f"{container['name']} {container.get('cpu_percent') or 0:.1f}%"
                                           for container in busiest),
                  "Most RAM: " + "; ".join(f"{container['name']} {_size(container.get('memory_usage') or 0)}"
                                           for container in biggest)]
    return "\n".join(lines)
