"""File explorer popup (key e): browse the home folder as Finder shows it, no hidden files.

The highlighted entry's size is scanned in the background (a folder's size includes everything
in it, hidden files too). Selecting a file closes the popup with its path, for the app to use.
"""

from collections.abc import Iterable
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import VerticalGroup
from textual.screen import ModalScreen
from textual.widgets import DirectoryTree, Static
from textual.worker import get_current_worker

import disk_scan

# Folder scans stop after this many entries, so highlighting ~/Pictures can't hang for long
SCAN_BUDGET = 50_000
# Wait this long after the highlight moves before scanning, so arrowing through a list is cheap
SCAN_DELAY = 0.3

class UserDirectoryTree(DirectoryTree):
    """DirectoryTree without dot-files and Finder-hidden entries."""

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [path for path in paths if disk_scan.is_user_facing(path)]

class FileExplorerScreen(ModalScreen[Path | None]):
    BINDINGS = [("e", "close", "Close"), ("escape", "close", "Close")]

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        self.root = root or Path.home()
        self.scan_timer = None

    def compose(self) -> ComposeResult:
        with VerticalGroup(id="explorer"):
            yield UserDirectoryTree(self.root)
            yield Static("", id="explorer_details", markup=False)

    def on_mount(self) -> None:
        self.query_one("#explorer").border_title = str(self.root).replace(str(Path.home()), "~", 1)
        self.query_one("#explorer").border_subtitle = "enter open · esc close"
        self.query_one(UserDirectoryTree).focus()

    def on_tree_node_highlighted(self, event: DirectoryTree.NodeHighlighted) -> None:
        if event.node.data is None:
            return
        if self.scan_timer is not None:
            self.scan_timer.stop()
        path = event.node.data.path
        self.show_details(f"{self.short(path)} · scanning...")
        self.scan_timer = self.set_timer(SCAN_DELAY, lambda: self.scan(path))

    def short(self, path: Path) -> str:
        home = Path.home()
        return f"~/{path.relative_to(home)}" if path.is_relative_to(home) and path != home else str(path)

    def show_details(self, text: str) -> None:
        self.query_one("#explorer_details", Static).update(text)

    @work(thread=True, exclusive=True, group="scan")
    def scan(self, path: Path) -> None:
        """Size of the highlighted file, or a folder's total size and file count."""
        try:
            if path.is_dir():
                info = disk_scan.folder_size(str(path), disk_scan.Budget(SCAN_BUDGET))
                size = info["size_human"] if info["complete"] else f"at least {info['size_human']}"
                files = f"{info['files']:,}{'+' if not info['complete'] else ''} files"
                text = f"{self.short(path)} · folder · {size}, {files}"
            else:
                text = f"{self.short(path)} · {disk_scan.human_size(path.stat().st_size)}"
        except OSError as e:
            text = f"{self.short(path)} · can't read: {e.strerror}"
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self.show_details, text)

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.dismiss(event.path)

    def action_close(self) -> None:
        self.dismiss(None)
