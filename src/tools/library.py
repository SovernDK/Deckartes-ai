"""The library: files saved for good, which the model searches with the search_library tool.

The user adds a file or a whole folder in the file explorer (key e). Each file is split into
overlapping chunks of about CHUNK_CHARS, embedded with config.rag_embed_model and stored in one
SQLite file at config.library_path, so later sessions find it without loading it again.

Files are identified by their full path. Adding a file replaces only that file's data, in one
transaction after all of its chunks are embedded, so a failed or stopped load keeps the old data.
A file whose stored version is already current isn't embedded again.

Text files are read as they are, PDFs page by page with pypdf. Chunks remember where they come
from ("page 12", "line 340") so answers can say where something is.
"""

import asyncio
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from core import clock
from core.config import config
from model import mlx_client
from tools import disk_scan

CHUNK_CHARS = 1500
OVERLAP_CHARS = 200
TOP_K = 5
EMBED_BATCH = 16
MAX_CHUNKS = 20_000
MAX_FILE_BYTES = 50 * 1024 * 1024
# EmbeddingGemma's task prefixes; they gave clearer matches than plain text in a test
DOCUMENT_PREFIX = "title: {title} | text: "
QUERY_PREFIX = "task: search result | query: "
# A stored file made with other chunking or prefixes is embedded again when it's loaded again
SETTINGS = f"{CHUNK_CHARS}|{OVERLAP_CHARS}|{DOCUMENT_PREFIX}"

# What a folder load picks up; a file picked by itself can be anything that decodes as text
FOLDER_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".org", ".tex", ".pdf", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".xml", ".html", ".htm", ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".c", ".h", ".cpp",
    ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".sql", ".swift", ".kt",
}
SKIP_DIRS = {"node_modules", "venv", "__pycache__", "site-packages"}
# How many unreadable files the summary names
SHOWN_FAILURES = 3

EMPTY_LIBRARY = "The library is empty: the user hasn't saved any files yet."
OTHER_MODEL = ("The user's saved files were embedded with a different model. They have to load them again "
               "before they can be searched.")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    model TEXT NOT NULL,
    settings TEXT NOT NULL,
    saved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    location TEXT NOT NULL,
    text TEXT NOT NULL,
    vector BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_by_file ON chunks (file_id);
"""

@dataclass
class _Section:
    """One PDF page or one line of a text file."""
    kind: str
    number: int
    text: str

    @property
    def position(self) -> tuple[str, int]:
        return self.kind, self.number

@dataclass
class _Chunk:
    text: str
    # Where it is in the file, e.g. "page 3", "pages 3-4" or "lines 120-148"
    where: str

@dataclass
class AddResult:
    """What one load did. Lists hold file names."""
    saved: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    # (file name, why it couldn't be read)
    failed: list[tuple[str, str]] = field(default_factory=list)
    chunks: int = 0

    @property
    def summary(self) -> str:
        """The load's outcome, for telling the user."""
        files = len(self.saved)
        text = (f"Saved {files} file{'' if files == 1 else 's'} ({self.chunks:,} chunk{'' if self.chunks == 1 else 's'}) "
                "to the library") if files else "Nothing new to save"
        if self.unchanged:
            text += f", {len(self.unchanged)} unchanged"
        text += "."
        if self.failed:
            shown = "; ".join(f"{name}: {reason}" for name, reason in self.failed[:SHOWN_FAILURES])
            more = len(self.failed) - SHOWN_FAILURES
            text += f" Could not read {shown}{f'; and {more} more' if more > 0 else ''}."
        return text

@dataclass
class Entry:
    """A file in the library."""
    path: Path
    chunks: int
    saved_at: str
    # False when it was embedded with another model, so searches leave it out until it's loaded again
    current: bool

@dataclass
class _Index:
    """Every searchable vector in memory; version says which state of the database it holds."""
    version: tuple
    ids: np.ndarray
    matrix: np.ndarray

_index: _Index | None = None

class _Cancelled(Exception):
    pass

class _Unreadable(ValueError):
    """This file can't be loaded; a folder load carries on with the next one."""

@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    """A connection whose changes are committed together at the end, or not at all."""
    config.library_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.library_path, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(_SCHEMA)
        with connection:
            yield connection
    finally:
        connection.close()

def _mark_changed(db: sqlite3.Connection) -> None:
    """Call after a write, inside its transaction, so the in-memory index is rebuilt."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    db.execute(f"PRAGMA user_version = {version + 1}")

def _decode(data: bytes) -> str:
    if b"\0" in data[:8192]:
        raise ValueError("this looks like a binary file; only text files and PDFs can be loaded")
    for encoding in ("utf-8-sig", "cp1250"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")

def _read_sections(path: Path) -> list[_Section]:
    """The file's text, one section per PDF page and one per line otherwise."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"the file is bigger than {MAX_FILE_BYTES // 1024 // 1024} MB")
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(path)
        # Many PDFs are encrypted with an empty password, only to restrict printing or copying
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("the PDF is password-protected")
        pages = [_Section("page", i, page.extract_text() or "") for i, page in enumerate(reader.pages, 1)]
        if not any(page.text.strip() for page in pages):
            raise ValueError("the PDF has no text layer (it's probably scanned images)")
        return pages
    text = _decode(path.read_bytes())
    return [_Section("line", i, line) for i, line in enumerate(text.splitlines(), 1)]

def _pieces(sections: list[_Section]) -> list[tuple[tuple[str, int], str]]:
    """Paragraphs (blank-line separated) with where each starts; long ones cut at sentence ends."""
    pieces, current, start = [], [], None
    def flush():
        nonlocal current, start
        if current:
            pieces.append((start, "\n".join(current)))
        current, start = [], None
    for section in sections:
        if section.kind == "page":
            flush()
            for paragraph in re.split(r"\n\s*\n", section.text):
                if paragraph.strip():
                    pieces.append((section.position, paragraph.strip()))
            continue
        if not section.text.strip():
            flush()
        else:
            start = start or section.position
            current.append(section.text)
    flush()
    result = []
    for where, text in pieces:
        while len(text) > CHUNK_CHARS:
            cut = max(text.rfind(". ", 0, CHUNK_CHARS), text.rfind("\n", 0, CHUNK_CHARS))
            cut = cut + 1 if cut > CHUNK_CHARS // 2 else CHUNK_CHARS
            result.append((where, text[:cut].strip()))
            text = text[cut:]
        if text.strip():
            result.append((where, text.strip()))
    return result

def _span(first: tuple[str, int], last: tuple[str, int]) -> str:
    """("line", 120) and ("line", 148) -> "lines 120-148"; the same place reads "line 120"."""
    kind, start = first
    return f"{kind} {start}" if first == last else f"{kind}s {start}-{last[1]}"

def _chunk(sections: list[_Section]) -> list[_Chunk]:
    """Paragraphs packed into chunks of up to about CHUNK_CHARS, each starting with the last
    OVERLAP_CHARS of the one before, so text cut at a boundary is whole in one of them."""
    chunks, parts, first, last, fresh = [], [], None, None, False
    for piece_where, text in _pieces(sections):
        # fresh: parts holds more than the overlap carried over from the previous chunk
        if fresh and sum(len(p) for p in parts) + len(text) > CHUNK_CHARS:
            body = "\n\n".join(parts)
            chunks.append(_Chunk(body, _span(first, last)))
            parts, first = [body[-OVERLAP_CHARS:]], piece_where
        parts.append(text)
        first, last, fresh = first or piece_where, piece_where, True
    if fresh:
        chunks.append(_Chunk("\n\n".join(parts), _span(first, last)))
    return chunks

def _readable_chunks(path: Path) -> list[_Chunk]:
    """The file's chunks; anything that stops them being made is _Unreadable."""
    try:
        chunks = _chunk(_read_sections(path))
    except Exception as e:
        raise _Unreadable(str(e) or type(e).__name__) from e
    if not chunks:
        raise _Unreadable("the file is empty")
    if len(chunks) > MAX_CHUNKS:
        raise _Unreadable(f"the file is too long ({len(chunks):,} chunks, at most {MAX_CHUNKS:,})")
    return chunks

def _embed(texts: list[str]) -> list[list[float]]:
    return mlx_client.embed_file_text(texts)

def _is_current(path: Path, stat: os.stat_result) -> bool:
    """Whether the library already holds exactly what saving this file would store."""
    with _database() as db:
        return db.execute("SELECT 1 FROM files WHERE path = ? AND mtime_ns = ? AND size = ? AND model = ? AND settings = ?",
                          (str(path), stat.st_mtime_ns, stat.st_size, config.rag_embed_model, SETTINGS)).fetchone() is not None

def _embed_chunks(path: Path, chunks: list[_Chunk], label: str, progress: Callable[[str], None],
                  cancel: threading.Event) -> list[list[float]]:
    prefix = DOCUMENT_PREFIX.format(title=path.name)
    vectors = []
    progress(f"Embedding {label}: 0/{len(chunks)} chunks")
    for start in range(0, len(chunks), EMBED_BATCH):
        if cancel.is_set():
            raise _Cancelled()
        vectors += _embed([prefix + chunk.text for chunk in chunks[start:start + EMBED_BATCH]])
        progress(f"Embedding {label}: {len(vectors)}/{len(chunks)} chunks")
    if cancel.is_set():
        raise _Cancelled()
    return vectors

def _store(path: Path, stat: os.stat_result, chunks: list[_Chunk], vectors: list[list[float]]) -> None:
    """Replace this file's data, and only this file's."""
    with _database() as db:
        db.execute("DELETE FROM files WHERE path = ?", (str(path),))
        file_id = db.execute("INSERT INTO files (path, mtime_ns, size, model, settings, saved_at) VALUES (?, ?, ?, ?, ?, ?)",
                             (str(path), stat.st_mtime_ns, stat.st_size, config.rag_embed_model, SETTINGS,
                              clock.now().strftime("%Y-%m-%d %H:%M"))).lastrowid
        db.executemany("INSERT INTO chunks (file_id, seq, location, text, vector) VALUES (?, ?, ?, ?, ?)",
                       ((file_id, seq, chunk.where, chunk.text, np.asarray(vector, dtype=np.float32).tobytes())
                        for seq, (chunk, vector) in enumerate(zip(chunks, vectors))))
        _mark_changed(db)

def _save(path: Path, label: str, result: AddResult, progress: Callable[[str], None], cancel: threading.Event) -> None:
    if cancel.is_set():
        raise _Cancelled()
    progress(f"Reading {label}")
    try:
        stat = path.stat()
    except OSError as e:
        raise _Unreadable(e.strerror or str(e)) from e
    if _is_current(path, stat):
        result.unchanged.append(path.name)
        return
    chunks = _readable_chunks(path)
    vectors = _embed_chunks(path, chunks, label, progress, cancel)
    _store(path, stat, chunks, vectors)
    result.saved.append(path.name)
    result.chunks += len(chunks)

def _files_in(folder: Path) -> list[Path]:
    """The files a folder load picks up, in path order. Symlinks are left out."""
    found = []
    for root, folders, names in os.walk(folder):
        folders[:] = [name for name in folders if name not in SKIP_DIRS and disk_scan.is_user_facing(Path(root, name))]
        for name in names:
            path = Path(root, name)
            if path.suffix.lower() in FOLDER_SUFFIXES and not path.is_symlink() and disk_scan.is_user_facing(path):
                found.append(path)
    return sorted(found)

def _add(path: Path, progress: Callable[[str], None], cancel: threading.Event) -> AddResult:
    path = path.expanduser().resolve()
    result = AddResult()
    if not path.is_dir():
        _save(path, path.name, result, progress, cancel)
        return result
    files = _files_in(path)
    if not files:
        raise ValueError("no text files or PDFs in this folder")
    for number, file in enumerate(files, 1):
        try:
            _save(file, f"{number}/{len(files)} {file.name}", result, progress, cancel)
        except _Unreadable as e:
            result.failed.append((file.name, str(e)))
    return result

async def add(path: Path, on_progress: Callable[[str], None] | None = None) -> AddResult:
    """Save a file, or every suitable file under a folder. on_progress gets a line describing the
    current step, on the event loop. Cancelling stops at the next batch; finished files stay saved.
    Raises ValueError for a single file that can't be read; a folder load reports those in the result."""
    loop = asyncio.get_running_loop()
    cancel = threading.Event()
    def progress(label: str) -> None:
        if on_progress:
            loop.call_soon_threadsafe(on_progress, label)
    try:
        return await asyncio.to_thread(_add, path, progress, cancel)
    except asyncio.CancelledError:
        cancel.set()
        raise

def _searchable(db: sqlite3.Connection) -> _Index:
    """The in-memory vectors for the current model, read again only if the database changed."""
    global _index
    version = (str(config.library_path), config.rag_embed_model, db.execute("PRAGMA user_version").fetchone()[0])
    if _index is None or _index.version != version:
        rows = db.execute("SELECT chunks.id, vector FROM chunks JOIN files ON files.id = file_id "
                          "WHERE model = ? ORDER BY chunks.id", (config.rag_embed_model,)).fetchall()
        ids = np.fromiter((row[0] for row in rows), dtype=np.int64, count=len(rows))
        matrix = np.frombuffer(b"".join(row[1] for row in rows), dtype=np.float32).reshape(len(rows), -1 if rows else 0)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1
        _index = _Index(version, ids, matrix / norms)
    return _index

def _passages_text(passages: list[tuple[str, str, str]]) -> str:
    parts = "\n\n".join(f"[{Path(path).name}, {where}]\n{text}" for path, where, text in passages)
    # The rule is repeated after the passages: small models follow what they read last
    return ("Passages from the user's saved files most related to the search, best match first, each labelled with "
            f"its file and where it is in the file.\n\n{parts}\n\nEnd of passages. For a question about the user's "
            "files, answer only from these passages and give the label of the one you used. If none of them answers "
            "it, say the saved files don't mention it; don't answer from your own knowledge or make up a file, page "
            "or line.")

def _search(query: str) -> str:
    with _database() as db:
        index = _searchable(db)
        if not len(index.ids):
            return OTHER_MODEL if db.execute("SELECT 1 FROM files LIMIT 1").fetchone() else EMPTY_LIBRARY
    question = np.asarray(_embed([QUERY_PREFIX + query])[0], dtype=np.float32)
    question /= np.linalg.norm(question) or 1
    best = [int(index.ids[i]) for i in np.argsort(-(index.matrix @ question), kind="stable")[:TOP_K]]
    with _database() as db:
        marks = ",".join("?" * len(best))
        rows = {row[0]: row[1:] for row in db.execute(
            f"SELECT chunks.id, path, location, text FROM chunks JOIN files ON files.id = file_id WHERE chunks.id IN ({marks})",
            best)}
    return _passages_text([rows[chunk_id] for chunk_id in best if chunk_id in rows])

async def search(query: str) -> str:
    """The tool text: the TOP_K saved chunks most like the query, best first."""
    return await asyncio.to_thread(_search, query)

def entries() -> list[Entry]:
    """Everything in the library, in path order."""
    with _database() as db:
        rows = db.execute("SELECT path, COUNT(chunks.id), saved_at, model FROM files LEFT JOIN chunks "
                          "ON chunks.file_id = files.id GROUP BY files.id ORDER BY path").fetchall()
    return [Entry(Path(path), count, saved_at, model == config.rag_embed_model) for path, count, saved_at, model in rows]

def matching(text: str) -> list[Entry]:
    """Entries a user's words mean: starting with / or ~, a file or everything under a folder;
    otherwise a file name in any folder."""
    text = text.strip()
    if text.startswith(("/", "~")):
        target = Path(text).expanduser().resolve()
        return [entry for entry in entries() if entry.path.is_relative_to(target)]
    return [entry for entry in entries() if entry.path.name.lower() == text.lower()]

def forget(chosen: list[Entry]) -> None:
    """Remove these files from the library."""
    with _database() as db:
        db.executemany("DELETE FROM files WHERE path = ?", ((str(entry.path),) for entry in chosen))
        _mark_changed(db)

def _shown_path(entry: Entry) -> str:
    home = Path.home()
    path = f"~/{entry.path.relative_to(home)}" if entry.path.is_relative_to(home) else str(entry.path)
    return path.replace("|", "\\|") + ("" if entry.current else " (other embedding model: load it again)")

def report(all_entries: list[Entry]) -> str:
    """The library as a Markdown table, for the /library command."""
    if not all_entries:
        return "The library is empty. Press **e** to load a file or folder."
    files, chunks = len(all_entries), sum(entry.chunks for entry in all_entries)
    rows = "\n".join(f"| {_shown_path(entry)} | {entry.chunks:,} | {entry.saved_at} |" for entry in all_entries)
    return (f"**Library:** {files:,} file{'' if files == 1 else 's'}, {chunks:,} chunk{'' if chunks == 1 else 's'}\n\n"
            f"| File | Chunks | Saved |\n|---|---|---|\n{rows}")
