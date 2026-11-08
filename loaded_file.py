"""A file loaded into the conversation (picked in the file explorer, key e).

A file that fits in WHOLE_FILE_SHARE of the context window is sent whole. A longer one is split
into overlapping chunks of about CHUNK_CHARS, embedded with config.rag_embed_model, and each
question gets the TOP_K chunks most like it (plus the previous question, for follow-ups).
Embeddings are cached on disk per file version, so loading the same file again is instant.

Text files are read as they are, PDFs page by page with pypdf. Chunks remember where they come
from ("page 12", "line 340") so answers can say where something is.
"""

import asyncio
import hashlib
import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from config import config
from ollama_client import get_client
from similarity import cosine

# Rough characters per token; Polish and code use more tokens per character than English prose
CHARS_PER_TOKEN = 3.5
# A file up to this share of config.context_length is sent whole instead of in excerpts
WHOLE_FILE_SHARE = 0.25
CHUNK_CHARS = 1500
OVERLAP_CHARS = 200
TOP_K = 5
# A follow-up ("and chapter 3?") also matches the previous question, at this weight
PREVIOUS_WEIGHT = 0.8
EMBED_BATCH = 16
# Embedding a few thousand chunks takes minutes; refuse bigger files
MAX_CHUNKS = 2000
MAX_FILE_BYTES = 50 * 1024 * 1024
# EmbeddingGemma's task prefixes; they gave clearer matches than plain text in a test
DOCUMENT_PREFIX = "title: none | text: "
QUERY_PREFIX = "task: search result | query: "
CACHE_DIR = Path.home() / ".cache" / "deckartes" / "embeddings"

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
class FileContext:
    """What one turn sends from the loaded file."""
    # For the usage line: "whole" or "5 excerpts"
    label: str
    # Goes at the end of the system message; None for excerpts
    system: str | None = None
    # Goes just before the question; None for a file sent whole
    excerpts: str | None = None

@dataclass
class LoadedFile:
    path: Path
    text: str
    chunks: list[_Chunk]
    # None for a file sent whole
    vectors: list[list[float]] | None = None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def how_sent(self) -> str:
        """How turns will receive the file, for telling the user."""
        if self.vectors is None:
            return f"whole, about {round(len(self.text) / CHARS_PER_TOKEN):,} tokens"
        return f"{len(self.chunks):,} chunks; each question gets the {TOP_K} most related"

    async def context_for(self, question: str, previous: str | None) -> FileContext:
        """What to send with this question; previous is the question before it, if any."""
        if self.vectors is None:
            return FileContext("whole", system=self._whole_prompt())
        chunks = await asyncio.to_thread(self._search, question, previous)
        return FileContext(f"{len(chunks)} excerpts", excerpts=self._excerpts_prompt(chunks))

    def _search(self, question: str, previous: str | None) -> list[_Chunk]:
        """The TOP_K chunks most like the question (or, weighted down, the previous one), in file order."""
        vectors = _embed([QUERY_PREFIX + q for q in ([question, previous] if previous else [question])])
        scores = []
        for i, chunk_vector in enumerate(self.vectors):
            score = cosine(vectors[0], chunk_vector)
            if previous:
                score = max(score, PREVIOUS_WEIGHT * cosine(vectors[1], chunk_vector))
            scores.append((score, i))
        best = sorted(i for _, i in sorted(scores, reverse=True)[:TOP_K])
        return [self.chunks[i] for i in best]

    def _whole_prompt(self) -> str:
        return (f"The user loaded the file \"{self.name}\". Its full text is below. Answer questions "
                f"about the file from this text.\n\n<file name=\"{self.name}\">\n{self.text}\n</file>")

    def _excerpts_prompt(self, chunks: list[_Chunk]) -> str:
        parts = "\n\n".join(f"[{c.where}]\n{c.text}" for c in chunks)
        # The rule is repeated after the excerpts: small models follow what they read last
        return (f"The user loaded the file \"{self.name}\", which is too long to show whole. These are "
                f"the parts of it most related to the user's question, in file order, each labelled with "
                f"where it is in the file.\n\n{parts}\n\nEnd of excerpts. For a question about the file, "
                f"answer only from these excerpts and give the label of the one you used. If none of them "
                f"answers it, say the file doesn't mention it; don't answer from your own knowledge or make "
                f"up a page or line.")

class _Cancelled(Exception):
    pass

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

def _full_text(sections: list[_Section]) -> str:
    if sections and sections[0].kind == "page":
        return "\n\n".join(f"[page {section.number}]\n{section.text}" for section in sections)
    return "\n".join(section.text for section in sections)

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

def _cache_file(path: Path) -> Path:
    stat = path.stat()
    key = f"{path}|{stat.st_mtime_ns}|{stat.st_size}|{config.rag_embed_model}|{CHUNK_CHARS}|{OVERLAP_CHARS}|{DOCUMENT_PREFIX}"
    return CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"

def _embed(texts: list[str]) -> list[list[float]]:
    return get_client().embed(model=config.rag_embed_model, input=texts).embeddings

def _load(path: Path, on_progress: Callable[[int, int], None], cancel: threading.Event) -> LoadedFile:
    path = path.expanduser().resolve()
    sections = _read_sections(path)
    text = _full_text(sections)
    if not text.strip():
        raise ValueError("the file is empty")
    if len(text) / CHARS_PER_TOKEN <= config.context_length * WHOLE_FILE_SHARE:
        return LoadedFile(path, text, [])

    chunks = _chunk(sections)
    if len(chunks) > MAX_CHUNKS:
        raise ValueError(f"the file is too long ({len(chunks):,} chunks, at most {MAX_CHUNKS:,})")
    cache = _cache_file(path)
    if cache.exists():
        try:
            return LoadedFile(path, text, chunks, json.loads(cache.read_text())["vectors"])
        except (OSError, ValueError, KeyError):
            pass
    vectors = []
    on_progress(0, len(chunks))
    for start in range(0, len(chunks), EMBED_BATCH):
        if cancel.is_set():
            raise _Cancelled()
        batch = chunks[start:start + EMBED_BATCH]
        vectors += _embed([DOCUMENT_PREFIX + c.text for c in batch])
        on_progress(len(vectors), len(chunks))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"path": str(path), "vectors": vectors}))
    return LoadedFile(path, text, chunks, vectors)

async def load(path: Path, on_progress: Callable[[int, int], None] | None = None) -> LoadedFile:
    """Read a file, embedding its chunks if it's too long to send whole. on_progress gets
    (chunks embedded, chunks in total) on the event loop. Cancelling stops the embedding."""
    loop = asyncio.get_running_loop()
    cancel = threading.Event()
    def progress(done: int, total: int) -> None:
        if on_progress:
            loop.call_soon_threadsafe(on_progress, done, total)
    try:
        return await asyncio.to_thread(_load, path, progress, cancel)
    except asyncio.CancelledError:
        cancel.set()
        raise
