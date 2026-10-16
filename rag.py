"""A file loaded into the conversation (picked in the file explorer, key e).

A file that fits in WHOLE_FILE_SHARE of the context window is sent whole. A longer one is split
into overlapping chunks of about CHUNK_CHARS, embedded with config.rag_embed_model, and each
question gets the TOP_K chunks most like it (plus the previous question, for follow-ups).
Embeddings are cached on disk per file version, so loading the same file again is instant.

Text files are read as they are, PDFs page by page with pypdf. Chunks remember where they come
from ("page 12", "line 340") so answers can say where something is.

Loading and searching are blocking: call them with asyncio.to_thread.
"""

import hashlib
import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from config import config
from ollama_api import get_client
from tool_router import cosine

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
class Chunk:
    text: str
    # Where it is in the file, e.g. "page 3", "pages 3-4" or "lines 120-148"
    where: str

@dataclass
class Document:
    path: Path
    chunks: list[Chunk]
    # None for a file sent whole
    vectors: list[list[float]] | None = None
    text: str = ""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def whole(self) -> bool:
        return self.vectors is None

    @property
    def tokens(self) -> int:
        return round(len(self.text) / CHARS_PER_TOKEN)

class Cancelled(Exception):
    pass

# Called with (chunks embedded, chunks in total) while a long file is embedded
Progress = Callable[[int, int], None] | None

def _decode(data: bytes) -> str:
    if b"\0" in data[:8192]:
        raise ValueError("this looks like a binary file; only text files and PDFs can be loaded")
    for encoding in ("utf-8-sig", "cp1250"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")

def read_sections(path: Path) -> list[tuple[str, str]]:
    """The file's text as (where, text) sections: one per PDF page, one per line otherwise."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"the file is bigger than {MAX_FILE_BYTES // 1024 // 1024} MB")
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(path)
        # Many PDFs are encrypted with an empty password, only to restrict printing or copying
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("the PDF is password-protected")
        pages = [(f"page {i}", page.extract_text() or "") for i, page in enumerate(reader.pages, 1)]
        if not any(text.strip() for _, text in pages):
            raise ValueError("the PDF has no text layer (it's probably scanned images)")
        return pages
    text = _decode(path.read_bytes())
    return [(f"line {i}", line) for i, line in enumerate(text.splitlines(), 1)]

def _pieces(sections: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Paragraphs (blank-line separated) with where each starts; long ones cut at sentence ends."""
    pieces, current, start = [], [], None
    def flush():
        nonlocal current, start
        if current:
            pieces.append((start, "\n".join(current)))
        current, start = [], None
    for where, text in sections:
        if where.startswith("page"):
            flush()
            for paragraph in re.split(r"\n\s*\n", text):
                if paragraph.strip():
                    pieces.append((where, paragraph.strip()))
            continue
        if not text.strip():
            flush()
        else:
            start = start or where
            current.append(text)
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

def _span(first: str, last: str) -> str:
    """"line 120" and "line 148" -> "lines 120-148"; the same place stays as it is."""
    if first == last:
        return first
    kind, start = first.split(" ")
    return f"{kind}s {start}-{last.split(' ')[1]}"

def chunk(sections: list[tuple[str, str]]) -> list[Chunk]:
    """Paragraphs packed into chunks of up to about CHUNK_CHARS, each starting with the last
    OVERLAP_CHARS of the one before, so text cut at a boundary is whole in one of them."""
    chunks, parts, first, last, fresh = [], [], None, None, False
    for piece_where, text in _pieces(sections):
        # fresh: parts holds more than the overlap carried over from the previous chunk
        if fresh and sum(len(p) for p in parts) + len(text) > CHUNK_CHARS:
            body = "\n\n".join(parts)
            chunks.append(Chunk(body, _span(first, last)))
            parts, first = [body[-OVERLAP_CHARS:]], piece_where
        parts.append(text)
        first, last, fresh = first or piece_where, piece_where, True
    if fresh:
        chunks.append(Chunk("\n\n".join(parts), _span(first, last)))
    return chunks

def _cache_file(path: Path) -> Path:
    stat = path.stat()
    key = f"{path}|{stat.st_mtime_ns}|{stat.st_size}|{config.rag_embed_model}|{CHUNK_CHARS}|{OVERLAP_CHARS}|{DOCUMENT_PREFIX}"
    return CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"

def _embed(texts: list[str]) -> list[list[float]]:
    return get_client().embed(model=config.rag_embed_model, input=texts).embeddings

def load(path: Path, on_progress: Progress = None, cancel: threading.Event | None = None) -> Document:
    """Read a file; embed its chunks if it's too long to send whole. Raises Cancelled if cancel is set."""
    path = path.expanduser().resolve()
    sections = read_sections(path)
    text = "\n".join(t for _, t in sections) if path.suffix.lower() != ".pdf" else \
        "\n\n".join(f"[{where}]\n{t}" for where, t in sections)
    if not text.strip():
        raise ValueError("the file is empty")
    if len(text) / CHARS_PER_TOKEN <= config.context_length * WHOLE_FILE_SHARE:
        return Document(path, [], None, text)

    chunks = chunk(sections)
    if len(chunks) > MAX_CHUNKS:
        raise ValueError(f"the file is too long ({len(chunks):,} chunks, at most {MAX_CHUNKS:,})")
    cache = _cache_file(path)
    if cache.exists():
        try:
            return Document(path, chunks, json.loads(cache.read_text())["vectors"], text)
        except (OSError, ValueError, KeyError):
            pass
    vectors = []
    if on_progress:
        on_progress(0, len(chunks))
    for start in range(0, len(chunks), EMBED_BATCH):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        batch = chunks[start:start + EMBED_BATCH]
        vectors += _embed([DOCUMENT_PREFIX + c.text for c in batch])
        if on_progress:
            on_progress(len(vectors), len(chunks))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"path": str(path), "vectors": vectors}))
    return Document(path, chunks, vectors, text)

def search(document: Document, query: str, previous: str | None = None) -> list[Chunk]:
    """The TOP_K chunks most like the question (or, weighted down, the previous one), in file order."""
    vectors = _embed([QUERY_PREFIX + q for q in ([query, previous] if previous else [query])])
    scores = []
    for i, chunk_vector in enumerate(document.vectors):
        score = cosine(vectors[0], chunk_vector)
        if previous:
            score = max(score, PREVIOUS_WEIGHT * cosine(vectors[1], chunk_vector))
        scores.append((score, i))
    best = sorted(i for _, i in sorted(scores, reverse=True)[:TOP_K])
    return [document.chunks[i] for i in best]

def whole_file_prompt(document: Document) -> str:
    return (f"The user loaded the file \"{document.name}\". Its full text is below. Answer questions "
            f"about the file from this text.\n\n<file name=\"{document.name}\">\n{document.text}\n</file>")

def excerpts_prompt(document: Document, chunks: list[Chunk]) -> str:
    parts = "\n\n".join(f"[{c.where}]\n{c.text}" for c in chunks)
    # The rule is repeated after the excerpts: small models follow what they read last
    return (f"The user loaded the file \"{document.name}\", which is too long to show whole. These are "
            f"the parts of it most related to the user's question, in file order, each labelled with "
            f"where it is in the file.\n\n{parts}\n\nEnd of excerpts. For a question about the file, "
            f"answer only from these excerpts and give the label of the one you used. If none of them "
            f"answers it, say the file doesn't mention it; don't answer from your own knowledge or make "
            f"up a page or line.")
