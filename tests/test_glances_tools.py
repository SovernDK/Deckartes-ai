"""The text the server tools give the model, from fake Glances responses."""

import unittest

import httpx

from http_fake import HttpTest
from tools import tools

GB = 1024 ** 3

def api(**endpoints) -> dict:
    return {f"/api/4/{name}": answer for name, answer in endpoints.items()}

CPU = {"total": 12.5, "user": 8, "system": 4.5, "cpucore": 4}
LOAD = {"min1": 1.0, "min5": 2.0, "min15": 3.0, "cpucore": 4}
MEM = {"used": 4 * GB, "total": 16 * GB, "percent": 25.0, "available": 12 * GB}
SWAP = {"total": 0}
FS = [{"device_name": "/dev/sda1", "fs_type": "ext4", "used": 50 * GB, "size": 100 * GB, "percent": 50.0, "free": 50 * GB},
      {"device_name": "/dev/sda1", "fs_type": "ext4", "used": 50 * GB, "size": 100 * GB, "percent": 50.0, "free": 50 * GB}]
SENSORS = [{"type": "temperature_core", "label": "Core 0", "value": 85, "warning": 80},
           {"type": "temperature_core", "label": "Core 1", "value": 43, "warning": 90},
           {"type": "temperature_core", "label": "Absent", "value": 0},
           {"type": "fan_speed", "label": "Fan", "value": 2100},
           {"type": "battery", "label": "BAT0", "value": 97}]
CONTAINERS = [{"name": "idle", "status": "running", "uptime": "3 days", "cpu_percent": 0.5, "memory_usage": 200 * 1024 ** 2},
              {"name": "busy", "status": "running", "uptime": "3 days", "cpu_percent": 40.0, "memory_usage": 3 * GB},
              {"name": "crashed", "status": "exited", "uptime": None, "cpu_percent": None, "memory_usage": None}]

class ServerToolTest(HttpTest):
    async def test_cpu_mentions_iowait_only_when_it_matters(self):
        self.serve(api(cpu=CPU | {"iowait": 2.5, "steal": 0.5}))
        self.assertEqual(await tools.get_cpu_usage(),
                         "CPU: 12.5% used (user 8.0%, system 4.5%, iowait 2.5%), 4 cores")

    async def test_load_includes_share_of_capacity(self):
        self.serve(api(load=LOAD))
        self.assertEqual(await tools.get_system_load(),
                         "Load average: 1.00 (1 min), 2.00 (5 min), 3.00 (15 min) on 4 cores = 50% of capacity")

    async def test_memory_shows_used_total_and_available(self):
        self.serve(api(mem=MEM))
        self.assertEqual(await tools.get_memory_usage(), "RAM: 4.0 GB of 16.0 GB used (25.0%), 12.0 GB available")

    async def test_zero_swap_is_none(self):
        self.serve(api(memswap=SWAP))
        self.assertEqual(await tools.get_swap_usage(), "Swap: none")

    async def test_disks_are_listed_once_per_device(self):
        self.serve(api(fs=FS))
        self.assertEqual(await tools.get_disk_usage(), "Disk /dev/sda1 (ext4): 50.0 GB of 100.0 GB used (50.0%), 50.0 GB free")

    async def test_gpu_without_a_load_says_unknown(self):
        self.serve(api(gpu=[{"gpu_id": "gpu0", "proc": None, "mem": 10, "temperature": 50}]))
        self.assertEqual(await tools.get_gpu_usage(), "GPU gpu0: load unknown, memory 10% used, 50°C")

    async def test_says_when_no_gpu_is_reported(self):
        self.serve(api(gpu=[]))
        self.assertEqual(await tools.get_gpu_usage(), "No GPU reported")

    async def test_temperatures_state_the_warning_level(self):
        self.serve(api(sensors=SENSORS))
        self.assertEqual(await tools.get_server_temperatures(),
                         "Temperatures: hottest 85°C (Core 0), warning level 80°C; Core 0: 85°C, Core 1: 43°C\n"
                         "Above warning level: Core 0\n"
                         "Fans: Fan: 2100 RPM\n"
                         "Battery: 97%")

    async def test_containers_problems_first_and_shared_uptime_once(self):
        self.serve(api(containers=CONTAINERS))
        self.assertEqual(await tools.get_containers(),
                         "3 containers (2 running, 1 exited); up 3 days unless noted\n"
                         "crashed: exited, up ?, CPU 0.0%, RAM 0 MB\n"
                         "busy: running, CPU 40.0%, RAM 3.0 GB\n"
                         "idle: running, CPU 0.5%, RAM 200 MB")

    async def test_says_when_no_containers_are_reported(self):
        self.serve(api(containers=[]))
        self.assertEqual(await tools.get_containers(), "No containers reported")

    def serve_overview(self, containers: list):
        self.serve(api(status={"version": "4.3"}, uptime='"5 days"', cpu=CPU, load=LOAD, mem=MEM, memswap=SWAP, fs=FS,
                       gpu=[], sensors=[], containers=containers))

    async def test_overview_has_one_line_per_area_and_ranks_containers(self):
        self.serve_overview(CONTAINERS)
        self.assertEqual(await tools.get_server_overview(),
                         "Online (Glances 4.3), up 5 days\n"
                         "CPU: 12.5% used (user 8.0%, system 4.5%), 4 cores\n"
                         "Load average: 1.00 (1 min), 2.00 (5 min), 3.00 (15 min) on 4 cores = 50% of capacity\n"
                         "RAM: 4.0 GB of 16.0 GB used (25.0%), 12.0 GB available\n"
                         "Swap: none\n"
                         "Disk /dev/sda1 (ext4): 50.0 GB of 100.0 GB used (50.0%), 50.0 GB free\n"
                         "No GPU reported\n"
                         "No sensors reported\n"
                         "3 containers (2 running, 1 exited); up 3 days unless noted\n"
                         "Not running: crashed: exited, up ?, CPU 0.0%, RAM 0 MB\n"
                         "Most CPU: busy 40.0%; idle 0.5%; crashed 0.0%\n"
                         "Most RAM: busy 3.0 GB; idle 200 MB; crashed 0 MB")

    async def test_overview_without_containers_ends_by_saying_so(self):
        self.serve_overview([])
        self.assertTrue((await tools.get_server_overview()).endswith("\nNo sensors reported\nNo containers reported"))

    async def test_unreachable_glances_is_reported(self):
        self.serve({"/api/4/cpu": httpx.ConnectError("no route to host")})
        self.assertEqual(await tools.run_tool("get_cpu_usage", {}),
                         "Tool get_cpu_usage failed: Could not reach Glances: no route to host")

if __name__ == "__main__":
    unittest.main()
