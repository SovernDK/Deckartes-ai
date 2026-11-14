"""Scan the local disk: list folders with sizes and search for files by name.

Only names, types and sizes are read, never file contents. Every scan is capped
(MAX_SCANNED entries) so a scan of a huge folder like ~ returns in reasonable time;
results then say they are incomplete.
"""

import asyncio
import fnmatch
import os
import stat
from pathlib import Path

MAX_SCANNED = 200_000

def resolve(path: str) -> Path:
    return Path(os.path.expanduser(path or "~")).resolve()

def human_size(size: float) -> str:
    if size < 1024:
        return f"{size:.0f} B"
    for unit in ["KB", "MB", "GB"]:
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size / 1024:.1f} TB"

def is_user_facing(path: Path) -> bool:
    """Whether Finder shows path: not a dot-file and not flagged hidden (like ~/Library)."""
    if path.name.startswith("."):
        return False
    try:
        return not path.lstat().st_flags & stat.UF_HIDDEN
    except OSError:
        return False

class Budget:
    """How many more entries a scan may look at; shared so one listing can't exceed MAX_SCANNED."""
    def __init__(self, limit: int = MAX_SCANNED) -> None:
        self.left = limit

def walk(root: Path, budget: Budget):
    """Yield every os.DirEntry under root (not following symlinks), skipping unreadable folders.
    When the budget runs out it yields None and stops, so callers know the walk was cut short."""
    stack = [root]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if budget.left <= 0:
                        yield None
                        return
                    budget.left -= 1
                    yield entry
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
        except OSError:
            continue

def entry_size(entry: os.DirEntry) -> int:
    try:
        return entry.stat(follow_symlinks=False).st_size
    except OSError:
        return 0

def folder_size(path: str, budget: Budget | None = None) -> dict:
    root = resolve(path)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a folder: {root}")

    total, files, complete = 0, 0, True
    for entry in walk(root, budget or Budget()):
        if entry is None:
            complete = False
            break
        if entry.is_file(follow_symlinks=False):
            total += entry_size(entry)
            files += 1

    return {"path": str(root), "size": total, "size_human": human_size(total),
            "files": files, "complete": complete}

def _list_directory(path: str, max_entries: int = 100) -> dict:
    root = resolve(path)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a folder: {root}")

    entries = []
    complete = True
    budget = Budget()
    with os.scandir(root) as children:
        for child in children:
            if child.is_dir(follow_symlinks=False):
                info = folder_size(child.path, budget)
                complete = complete and info["complete"]
                entry = {"name": child.name, "type": "folder", "size": info["size"]}
                if not info["complete"]:
                    # Scan budget ran out: real size is at least this much
                    entry["size_incomplete"] = True
                entries.append(entry)
            else:
                kind = "link" if child.is_symlink() else "file"
                entries.append({"name": child.name, "type": kind, "size": entry_size(child)})

    entries.sort(key=lambda entry: entry["size"], reverse=True)
    for entry in entries:
        entry["size_human"] = human_size(entry["size"])
    total = sum(entry["size"] for entry in entries)

    # Separate lists, so the model doesn't have to group a mixed list (and can't repeat an entry in both)
    shown = entries[:max_entries]
    return {"path": str(root), "size_human": human_size(total), "count": len(entries),
            "folders": [entry for entry in shown if entry["type"] == "folder"],
            "files": [entry for entry in shown if entry["type"] != "folder"],
            "truncated": len(entries) > max_entries, "complete": complete}

def _find_files(name_pattern: str, path: str = "~", max_results: int = 50) -> dict:
    root = resolve(path)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a folder: {root}")

    pattern = name_pattern.lower()
    if not any(ch in pattern for ch in "*?["):
        pattern = f"*{pattern}*"

    matches, complete = [], True
    for entry in walk(root, Budget()):
        if entry is None:
            complete = False
            break
        if fnmatch.fnmatch(entry.name.lower(), pattern):
            is_dir = entry.is_dir(follow_symlinks=False)
            size = 0 if is_dir else entry_size(entry)
            matches.append({"path": entry.path, "type": "folder" if is_dir else "file",
                            "size": size, "size_human": "" if is_dir else human_size(size)})
            if len(matches) >= max_results:
                complete = False
                break

    return {"path": str(root), "pattern": name_pattern, "count": len(matches),
            "matches": matches, "complete": complete}

async def list_directory(path: str) -> dict:
    return await asyncio.to_thread(_list_directory, path)

async def find_files(name_pattern: str, path: str) -> dict:
    return await asyncio.to_thread(_find_files, name_pattern, path)

async def measure_folder(path: str) -> dict:
    return await asyncio.to_thread(folder_size, path)
