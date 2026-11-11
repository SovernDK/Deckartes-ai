"""What the disk tools give the model, from a real temporary folder."""

import json
import tempfile
import unittest
from pathlib import Path

from tools import tools


class DiskToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        (self.root / "a.txt").write_bytes(b"abc")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "b.bin").write_bytes(b"x" * 2048)

    async def test_list_folder_puts_folders_and_files_biggest_first(self):
        self.assertEqual(await tools.run_tool("list_folder", {"path": str(self.root)}), json.dumps({
            "path": str(self.root), "size_human": "2.0 KB", "count": 2,
            "folders": [{"name": "sub", "type": "folder", "size": 2048, "size_human": "2.0 KB"}],
            "files": [{"name": "a.txt", "type": "file", "size": 3, "size_human": "3 B"}],
            "truncated": False, "complete": True}))

    async def test_list_folder_caps_entries(self):
        for i in range(100):
            (self.root / f"file{i}").touch()
        listing = await tools.list_folder(str(self.root))
        self.assertEqual((listing["count"], len(listing["folders"]) + len(listing["files"]), listing["truncated"]), (102, 100, True))

    async def test_search_files_matches_names_case_insensitively_in_subfolders(self):
        self.assertEqual(await tools.run_tool("search_files", {"name": "BIN", "path": str(self.root)}), json.dumps({
            "path": str(self.root), "pattern": "BIN", "count": 1,
            "matches": [{"path": str(self.root / "sub" / "b.bin"), "type": "file", "size": 2048, "size_human": "2.0 KB"}],
            "complete": True}))

    async def test_folder_size_counts_every_file(self):
        self.assertEqual(await tools.run_tool("get_folder_size", {"path": str(self.root)}), json.dumps({
            "path": str(self.root), "size": 2051, "size_human": "2.0 KB", "files": 2, "complete": True}))

    async def test_listing_a_file_says_it_is_not_a_folder(self):
        path = self.root / "a.txt"
        self.assertEqual(await tools.run_tool("list_folder", {"path": str(path)}),
                         f"Tool list_folder failed: Not a folder: {path}")

if __name__ == "__main__":
    unittest.main()
