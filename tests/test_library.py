"""The library from files on disk to tool text, with real files and SQLite and only the embedding client faked."""

import asyncio
import importlib
import re
import tempfile
import threading
import unittest
from pathlib import Path

import library
import mlx_client
from config import config
from library import AddResult, Entry
from test_conversation import TurnTest, call, chunk

TOPICS = 40

def vector(text: str) -> list[float]:
    """One axis per topic word in text, and one every text shares, so no vector is zero."""
    found = {int(n) for n in re.findall(r"topic(\d+)", text)}
    return [1.0 if i in found else 0.0 for i in range(TOPICS)] + [1.0]

class FakeEmbedder:
    def __init__(self):
        self.calls = []
        self.error = None

    def __call__(self, texts):
        return self.embed(texts)

    def embed(self, texts):
        self.calls.append((config.rag_embed_model, list(texts)))
        if self.error:
            raise self.error
        return [vector(text) for text in texts]

def paragraph(i: int) -> str:
    """About 1,000 characters, so each paragraph ends up as a chunk of its own."""
    return f"topic{i} " + " ".join(["word"] * 200)

def long_text(count=8) -> str:
    return "\n\n".join(paragraph(i) for i in range(count))

def pdf(*pages: str) -> bytes:
    """A minimal valid PDF with one line of text per page."""
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objects = ["<< /Type /Catalog /Pages 2 0 R >>",
               f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>",
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i} 0 R "
                       "/Resources << /Font << /F1 3 0 R >> >> >>")
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    return out

def labels(text: str) -> list[str]:
    return re.findall(r"^\[(.+)\]$", text, re.MULTILINE)

class LibraryTest(TurnTest):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name).resolve()
        self.patch(config, "library_path", self.folder / "data" / "library.db")
        self.embedder = FakeEmbedder()
        self.patch(mlx_client, "embed_file_text", self.embedder)

    def write(self, content, name="notes.txt") -> Path:
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return path

    def saved_names(self) -> list[str]:
        return [entry.path.name for entry in library.entries()]

    def chunk_count(self, name="notes.txt") -> int:
        return next(entry.chunks for entry in library.entries() if entry.path.name == name)

    def block_embedding(self, block_when):
        """Embedding pauses on texts block_when accepts, until released. Returns (started, release)."""
        started, release = threading.Event(), threading.Event()
        def embed(texts):
            if block_when(texts):
                started.set()
                release.wait(5)
            return [vector(text) for text in texts]
        self.embedder.embed = embed
        return started, release

    async def cancel_during_embedding(self, path, started, release):
        task = asyncio.create_task(library.add(path))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.sleep(0.1)

class PersistenceTest(LibraryTest):
    async def test_a_new_session_finds_a_file_saved_earlier(self):
        await library.add(self.write(long_text()))
        importlib.reload(library)
        found = await library.search("topic3")
        self.assertEqual(labels(found)[0], "notes.txt, line 7")
        self.assertIn("topic3 word", found)

    async def test_saved_text_is_still_found_after_the_file_is_deleted(self):
        path = self.write("topic5 secret")
        await library.add(path)
        path.unlink()
        self.assertIn("topic5 secret", await library.search("topic5"))

class OverwriteTest(LibraryTest):
    async def test_loading_a_changed_file_replaces_its_old_text(self):
        await library.add(self.write("topic1 first version"))
        await library.add(self.write("topic2 second version, which is longer"))
        found = await library.search("topic1")
        self.assertIn("second version", found)
        self.assertNotIn("first version", found)
        self.assertEqual(self.saved_names(), ["notes.txt"])

    async def test_loading_a_changed_file_drops_chunks_it_no_longer_has(self):
        path = self.write(long_text(8))
        await library.add(path)
        self.assertEqual(self.chunk_count(), 8)
        await library.add(self.write(long_text(3) + " changed"))
        self.assertEqual(self.chunk_count(), 3)

    async def test_loading_a_new_file_keeps_the_others(self):
        await library.add(self.write("topic1 kept", "a.txt"))
        await library.add(self.write("topic2 new", "b.txt"))
        self.assertIn("topic1 kept", await library.search("topic1"))
        self.assertEqual(self.saved_names(), ["a.txt", "b.txt"])

    async def test_overwriting_one_file_keeps_the_others(self):
        await library.add(self.write("topic1 kept", "a.txt"))
        await library.add(self.write("topic2 old", "b.txt"))
        await library.add(self.write("topic3 fresh", "b.txt"))
        self.assertIn("topic1 kept", await library.search("topic1"))
        self.assertEqual(self.saved_names(), ["a.txt", "b.txt"])

    async def test_the_same_name_in_another_folder_is_another_file(self):
        await library.add(self.write("topic1 here", "one/notes.txt"))
        await library.add(self.write("topic2 there", "two/notes.txt"))
        self.assertEqual(len(library.entries()), 2)

    async def test_an_unchanged_file_is_not_embedded_again(self):
        path = self.write(long_text())
        await library.add(path)
        calls = len(self.embedder.calls)
        result = await library.add(path)
        self.assertEqual(len(self.embedder.calls), calls)
        self.assertEqual((result.saved, result.unchanged), ([], ["notes.txt"]))

    async def test_a_new_embedding_model_embeds_an_unchanged_file_again(self):
        path = self.write("topic1 text")
        await library.add(path)
        self.patch(config, "rag_embed_model", "other/model")
        result = await library.add(path)
        self.assertEqual(result.saved, ["notes.txt"])
        self.assertEqual(self.embedder.calls[-1][0], "other/model")

    async def test_a_failed_embedding_keeps_the_old_text(self):
        await library.add(self.write("topic1 old text"))
        self.embedder.error = ConnectionError("server down")
        with self.assertRaisesRegex(ConnectionError, "server down"):
            await library.add(self.write("topic2 new text, which is longer"))
        self.embedder.error = None
        self.assertIn("topic1 old text", await library.search("topic1"))

    async def test_cancelling_keeps_the_old_text(self):
        await library.add(self.write("topic1 old text"))
        path = self.write("topic2 new text, which is longer")
        started, release = self.block_embedding(lambda texts: "new text" in texts[0])
        await self.cancel_during_embedding(path, started, release)
        self.embedder.embed = lambda texts: [vector(text) for text in texts]
        self.assertIn("topic1 old text", await library.search("topic1"))

class FolderTest(LibraryTest):
    async def test_files_in_subfolders_are_saved(self):
        self.write("topic1 a", "docs/a.txt")
        self.write("topic2 b", "docs/deep/er/b.md")
        result = await library.add(self.folder / "docs")
        self.assertEqual(result.saved, ["a.txt", "b.md"])
        self.assertEqual(result.chunks, 2)
        self.assertEqual(self.saved_names(), ["a.txt", "b.md"])

    async def test_what_a_folder_load_leaves_out(self):
        self.write("topic1 wanted", "docs/wanted.txt")
        self.write("hidden", "docs/.hidden.txt")
        self.write("hidden folder", "docs/.git/config.txt")
        self.write("dependency", "docs/node_modules/lib/readme.md")
        self.write("environment", "docs/venv/notes.txt")
        self.write(b"\x89PNG", "docs/picture.png")
        await library.add(self.folder / "docs")
        self.assertEqual(self.saved_names(), ["wanted.txt"])

    async def test_a_symlink_in_a_folder_is_not_followed(self):
        outside = self.write("topic1 outside", "outside.txt")
        self.write("topic2 inside", "docs/inside.txt")
        (self.folder / "docs" / "link.txt").symlink_to(outside)
        await library.add(self.folder / "docs")
        self.assertEqual(self.saved_names(), ["inside.txt"])

    async def test_a_file_picked_directly_can_have_any_suffix(self):
        await library.add(self.write("topic1 settings", "app.conf"))
        self.assertEqual(self.saved_names(), ["app.conf"])

    async def test_an_unreadable_file_is_reported_and_the_rest_are_saved(self):
        self.write("topic1 a", "docs/a.txt")
        self.write(b"\0binary", "docs/bad.txt")
        self.write(b"not really a pdf", "docs/broken.pdf")
        self.write("topic2 z", "docs/z.txt")
        result = await library.add(self.folder / "docs")
        self.assertEqual(result.saved, ["a.txt", "z.txt"])
        self.assertEqual(result.failed[0],
                         ("bad.txt", "this looks like a binary file; only text files and PDFs can be loaded"))
        self.assertEqual([name for name, _ in result.failed], ["bad.txt", "broken.pdf"])

    async def test_an_embedding_failure_stops_the_load_and_keeps_finished_files(self):
        self.write("topic1 a", "docs/a.txt")
        self.write("topic2 b", "docs/b.txt")
        self.write("topic3 c", "docs/c.txt")
        def embed(texts):
            if "topic2" in texts[0]:
                raise ConnectionError("server down")
            return [vector(text) for text in texts]
        self.embedder.embed = embed
        with self.assertRaisesRegex(ConnectionError, "server down"):
            await library.add(self.folder / "docs")
        self.assertEqual(self.saved_names(), ["a.txt"])

    async def test_cancelling_keeps_the_files_already_finished(self):
        self.write("topic1 a", "docs/a.txt")
        self.write("topic2 b", "docs/b.txt")
        started, release = self.block_embedding(lambda texts: "topic2" in texts[0])
        await self.cancel_during_embedding(self.folder / "docs", started, release)
        self.assertEqual(self.saved_names(), ["a.txt"])

    async def test_a_folder_with_nothing_to_load(self):
        self.write(b"\x89PNG", "docs/picture.png")
        with self.assertRaisesRegex(ValueError, "no text files or PDFs in this folder"):
            await library.add(self.folder / "docs")

    async def test_progress_numbers_the_files(self):
        self.write("topic1 a", "docs/a.txt")
        self.write("topic2 b", "docs/b.txt")
        progress = []
        await library.add(self.folder / "docs", progress.append)
        self.assertEqual(progress, ["Reading 1/2 a.txt", "Embedding 1/2 a.txt: 0/1 chunks", "Embedding 1/2 a.txt: 1/1 chunks",
                                    "Reading 2/2 b.txt", "Embedding 2/2 b.txt: 0/1 chunks", "Embedding 2/2 b.txt: 1/1 chunks"])

class EmbeddingTest(LibraryTest):
    async def test_progress_counts_chunks_in_batches(self):
        progress = []
        await library.add(self.write(long_text(20)), progress.append)
        self.assertEqual(progress, ["Reading notes.txt", "Embedding notes.txt: 0/20 chunks",
                                    "Embedding notes.txt: 16/20 chunks", "Embedding notes.txt: 20/20 chunks"])

    async def test_chunks_are_embedded_with_their_file_name_as_title(self):
        await library.add(self.write("topic1 text", "lease.txt"))
        self.assertEqual(self.embedder.calls[0][1], ["title: lease.txt | text: topic1 text"])

    async def test_questions_are_embedded_with_the_query_prefix(self):
        await library.add(self.write("topic1 text"))
        await library.search("topic1")
        self.assertEqual(self.embedder.calls[-1][1], ["task: search result | query: topic1"])

class SearchTest(LibraryTest):
    async def test_tool_text(self):
        await library.add(self.write("First part.\n\nSecond part."))
        self.assertEqual(await library.search("q"), (
            "Passages from the user's saved files most related to the search, best match first, each labelled with "
            "its file and where it is in the file.\n\n[notes.txt, lines 1-3]\nFirst part.\n\nSecond part.\n\nEnd of "
            "passages. For a question about the user's files, answer only from these passages and give the label of "
            "the one you used. If none of them answers it, say the saved files don't mention it; don't answer from "
            "your own knowledge or make up a file, page or line."))

    async def test_empty_library(self):
        self.assertEqual(await library.search("q"), "The library is empty: the user hasn't saved any files yet.")
        self.assertEqual(self.embedder.calls, [])

    async def test_best_match_first_then_five_at_most(self):
        await library.add(self.write(long_text()))
        found = labels(await library.search("topic3"))
        # Topic 3 wins; the rest tie and stay in file order
        self.assertEqual(found, ["notes.txt, line 7", "notes.txt, line 1", "notes.txt, line 3",
                                 "notes.txt, line 5", "notes.txt, line 9"])

    async def test_searches_across_files(self):
        await library.add(self.write("topic1 cats", "a.txt"))
        await library.add(self.write("topic2 dogs", "b.txt"))
        self.assertEqual(labels(await library.search("topic2")), ["b.txt, line 1", "a.txt, line 1"])

    async def test_a_new_save_is_searchable_at_once(self):
        await library.add(self.write("topic1 a", "a.txt"))
        await library.search("topic1")
        await library.add(self.write("topic2 b", "b.txt"))
        self.assertIn("topic2 b", await library.search("topic2"))

    async def test_files_from_another_embedding_model_are_left_out(self):
        await library.add(self.write("topic1 old model", "a.txt"))
        self.patch(config, "rag_embed_model", "other/model")
        await library.add(self.write("topic2 new model", "b.txt"))
        found = await library.search("topic1")
        self.assertIn("new model", found)
        self.assertNotIn("old model", found)

    async def test_only_files_from_another_embedding_model(self):
        await library.add(self.write("topic1 text"))
        self.patch(config, "rag_embed_model", "other/model")
        self.assertEqual(await library.search("q"),
                         "The user's saved files were embedded with a different model. They have to load them again "
                         "before they can be searched.")

    async def test_a_search_failure_is_raised_for_the_tool_runner(self):
        await library.add(self.write("topic1 text"))
        self.embedder.error = ConnectionError("server down")
        with self.assertRaisesRegex(ConnectionError, "server down"):
            await library.search("q")

class ChunkTest(LibraryTest):
    async def found(self, content, name="notes.txt", query="q") -> str:
        await library.add(self.write(content, name))
        return await library.search(query)

    async def test_chunk_starts_with_the_end_of_the_one_before(self):
        first, second = paragraph(0), paragraph(1)
        found = await self.found(f"{first}\n\n{second}")
        self.assertIn(f"[notes.txt, line 3]\n{first[-200:]}\n\n{second}\n\n", found)

    async def test_long_paragraph_is_cut_at_a_sentence_end(self):
        sentence = "A sentence of about forty characters long. "
        found = await self.found(sentence * 50)
        self.assertIn(f"[notes.txt, line 1]\n{(sentence * 34).strip()}\n\n", found)

    async def test_paragraph_over_several_lines_is_labelled_with_the_span(self):
        found = await self.found("One.\nTwo.\nThree.\n\n" + "\n\n".join(paragraph(i) for i in range(1, 3)))
        self.assertIn("[notes.txt, lines 1-5]\nOne.\nTwo.\nThree.\n\n" + paragraph(1), found)

    async def test_pdf_chunk_spans_pages(self):
        found = await self.found(pdf("First page", "Second page"), "paper.pdf")
        self.assertIn("[paper.pdf, pages 1-2]\nFirst page\n\nSecond page\n\n", found)

    async def test_cp1250_text(self):
        self.assertIn("zażółć", await self.found("zażółć".encode("cp1250")))

class RefusedFileTest(LibraryTest):
    async def refused(self, path, message):
        with self.assertRaisesRegex(ValueError, message):
            await library.add(path)
        self.assertEqual(library.entries(), [])

    async def test_binary(self):
        await self.refused(self.write(b"\0binary"), "this looks like a binary file; only text files and PDFs can be loaded")

    async def test_empty(self):
        await self.refused(self.write("  \n"), "the file is empty")

    async def test_too_big(self):
        self.patch(library, "MAX_FILE_BYTES", 1024 * 1024)
        await self.refused(self.write("x" * (1024 * 1024 + 1)), "the file is bigger than 1 MB")

    async def test_too_many_chunks(self):
        self.patch(library, "MAX_CHUNKS", 3)
        await self.refused(self.write(long_text(4)), r"the file is too long \(4 chunks, at most 3\)")
        self.assertEqual(self.embedder.calls, [])

class ForgetTest(LibraryTest):
    async def save_three(self):
        await library.add(self.write("topic1 a", "docs/a.txt"))
        await library.add(self.write("topic2 b", "docs/deep/b.txt"))
        await library.add(self.write("topic3 c", "docs2/c.txt"))

    def matched(self, text) -> list[str]:
        return [entry.path.name for entry in library.matching(text)]

    async def test_matches_a_full_path(self):
        await self.save_three()
        self.assertEqual(self.matched(str(self.folder / "docs/a.txt")), ["a.txt"])

    async def test_matches_everything_under_a_folder_and_not_a_similar_name(self):
        await self.save_three()
        self.assertEqual(self.matched(str(self.folder / "docs")), ["a.txt", "b.txt"])

    async def test_matches_a_file_name_in_any_case(self):
        await self.save_three()
        self.assertEqual(self.matched("B.TXT"), ["b.txt"])

    async def test_a_part_of_a_name_matches_nothing(self):
        await self.save_three()
        self.assertEqual(self.matched("b.tx"), [])

    async def test_forgetting_removes_the_text_and_keeps_the_rest(self):
        await self.save_three()
        library.forget(library.matching(str(self.folder / "docs")))
        self.assertEqual(self.saved_names(), ["c.txt"])
        found = await library.search("topic1")
        self.assertIn("topic3 c", found)
        self.assertNotIn("topic1 a", found)

    async def test_forgotten_file_is_embedded_again_when_loaded_again(self):
        path = self.write("topic1 a")
        await library.add(path)
        library.forget(library.matching("notes.txt"))
        result = await library.add(path)
        self.assertEqual(result.saved, ["notes.txt"])

class ReportTest(LibraryTest):
    def test_empty(self):
        self.assertEqual(library.report([]), "The library is empty. Press **e** to load a file or folder.")

    def test_lists_files_and_totals(self):
        entries = [Entry(Path("/t/a.txt"), 8, "2026-11-11 12:00", True), Entry(Path("/t/b.txt"), 1, "2026-11-10 09:30", False)]
        self.assertEqual(library.report(entries),
                         "**Library:** 2 files, 9 chunks\n\n| File | Chunks | Saved |\n|---|---|---|\n"
                         "| /t/a.txt | 8 | 2026-11-11 12:00 |\n"
                         "| /t/b.txt (other embedding model: load it again) | 1 | 2026-11-10 09:30 |")

    def test_shortens_the_home_folder_and_escapes_table_bars(self):
        entries = [Entry(Path.home() / "a|b.txt", 1, "2026-11-11 12:00", True)]
        self.assertIn("| ~/a\\|b.txt | 1 |", library.report(entries))

    async def test_saved_date_is_the_local_time(self):
        await library.add(self.write("topic1 a"))
        self.assertEqual(library.entries()[0].saved_at, "2026-11-11 12:00")

class SummaryTest(unittest.TestCase):
    def test_saved_files(self):
        self.assertEqual(AddResult(["a", "b"], [], [], 16).summary, "Saved 2 files (16 chunks) to the library.")

    def test_one_file(self):
        self.assertEqual(AddResult(["a"], [], [], 1).summary, "Saved 1 file (1 chunk) to the library.")

    def test_unchanged_files(self):
        self.assertEqual(AddResult(["a"], ["b", "c"], [], 4).summary, "Saved 1 file (4 chunks) to the library, 2 unchanged.")

    def test_nothing_new(self):
        self.assertEqual(AddResult([], ["a"], [], 0).summary, "Nothing new to save, 1 unchanged.")

    def test_failed_files_are_named(self):
        self.assertEqual(AddResult(["a"], [], [("b", "bad")], 1).summary,
                         "Saved 1 file (1 chunk) to the library. Could not read b: bad.")

    def test_a_long_failure_list_is_cut(self):
        failed = [("a", "x"), ("b", "x"), ("c", "x"), ("d", "x"), ("e", "x")]
        self.assertEqual(AddResult([], [], failed, 0).summary,
                         "Nothing new to save. Could not read a: x; b: x; c: x; and 2 more.")

class ToolTurnTest(LibraryTest):
    async def test_the_model_answers_from_the_tool_result(self):
        await library.add(self.write(long_text()))
        client = self.use([chunk(tool_calls=[call("search_library", query="topic3")], done=True)],
                          [chunk("from the file", done=True)])
        await self.reply("what about topic3?")
        self.assertIn("search_library", [tool.__name__ for tool in client.sent[0]["tools"]])
        result = client.sent[1]["messages"][-1]
        self.assertEqual(result["role"], "tool")
        self.assertEqual(labels(result["content"])[0], "notes.txt, line 7")
        self.assertIn("topic3 word", result["content"])

    async def test_a_search_failure_goes_back_to_the_model(self):
        await library.add(self.write("topic1 text"))
        self.embedder.error = ConnectionError("server down")
        client = self.use([chunk(tool_calls=[call("search_library", query="q")], done=True)],
                          [chunk("sorry", done=True)])
        await self.reply()
        self.assertIn("server down", client.sent[1]["messages"][-1]["content"])
