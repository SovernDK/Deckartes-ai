"""A loaded file from disk to what a turn sends, with real files and only the embedding client faked."""

import asyncio
import re
import threading
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import ollama_client
import loaded_file
from config import config
from conversation import Failed
from test_conversation import DATE_LINE, TurnTest, chunk

TOPICS = 40

def vector(text: str) -> list[float]:
    """One axis per topic word in text, and one every text shares, so no vector is zero."""
    found = {int(n) for n in re.findall(r"topic(\d+)", text)}
    return [1.0 if i in found else 0.0 for i in range(TOPICS)] + [1.0]

class FakeEmbedder:
    def __init__(self):
        self.calls = []
        self.error = None

    def embed(self, model, input):
        self.calls.append((model, list(input)))
        if self.error:
            raise self.error
        return NS(embeddings=[vector(text) for text in input])

def paragraph(i: int) -> str:
    """About 1,000 characters, so each paragraph ends up as a chunk of its own."""
    return f"topic{i} " + " ".join(["word"] * 200)

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

def excerpts_prompt(name: str, parts: str) -> str:
    return (f"The user loaded the file \"{name}\", which is too long to show whole. These are "
            f"the parts of it most related to the user's question, in file order, each labelled with "
            f"where it is in the file.\n\n{parts}\n\nEnd of excerpts. For a question about the file, "
            f"answer only from these excerpts and give the label of the one you used. If none of them "
            f"answers it, say the file doesn't mention it; don't answer from your own knowledge or make "
            f"up a page or line.")

class FileTest(TurnTest):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name).resolve()
        self.patch(loaded_file, "CACHE_DIR", self.folder / "cache")
        self.patch(config, "context_length", 1000)
        self.embedder = FakeEmbedder()
        patcher = mock.patch.dict(ollama_client._clients, {("sync", config.host): self.embedder})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, content, name="notes.txt") -> Path:
        path = self.folder / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return path

    async def load(self, path, progress=None):
        return await loaded_file.load(path, progress)

    async def attach(self, content, name="notes.txt"):
        self.conversation.file = await self.load(self.write(content, name))

    def long_text(self, count=8) -> str:
        return "\n\n".join(paragraph(i) for i in range(count))

    async def excerpts(self, content, query="q", name="notes.txt") -> str:
        """The excerpts message a turn sends for this file, in a context that forces excerpts."""
        self.patch(config, "context_length", 10)
        await self.attach(content, name)
        client = self.use([chunk("ok", done=True)])
        await self.reply(query)
        self.assertEqual(client.sent[0]["messages"][1]["role"], "system")
        return client.sent[0]["messages"][1]["content"]

class ShortFileTest(FileTest):
    async def test_goes_whole_in_the_system_message_every_turn(self):
        await self.attach("Some notes.\nMore notes.")
        client = self.use([chunk("a", done=True)], [chunk("b", done=True)])
        await self.reply("one")
        await self.reply("two")
        system = (f"{DATE_LINE}\n\nThe user loaded the file \"notes.txt\". Its full text is below. Answer questions "
                  "about the file from this text.\n\n<file name=\"notes.txt\">\nSome notes.\nMore notes.\n</file>")
        self.assertEqual(self.sent_contents(client, 0), [system, "one"])
        self.assertEqual(self.sent_contents(client, 1), [system, "one", "a", "two"])
        self.assertEqual(self.conversation.last_turn.file_context, "whole")
        self.assertEqual(self.embedder.calls, [])

    async def test_size_limit_follows_the_context_length(self):
        text = "x" * 875  # 250 tokens, a quarter of 1000
        await self.attach(text)
        client = self.use([chunk("a", done=True)])
        await self.reply()
        self.assertEqual(self.conversation.last_turn.file_context, "whole")
        await self.attach(text + "xxxx")
        self.use([chunk("a", done=True)])
        await self.reply()
        self.assertEqual(self.conversation.last_turn.file_context, "1 excerpts")

    async def test_cp1250_text(self):
        await self.attach("zażółć".encode("cp1250"))
        client = self.use([chunk("a", done=True)])
        await self.reply()
        self.assertIn("\nzażółć\n", client.sent[0]["messages"][0]["content"])

    async def test_pdf_pages_are_labelled(self):
        await self.attach(pdf("First page", "Second page"), "paper.pdf")
        client = self.use([chunk("a", done=True)])
        await self.reply()
        self.assertIn("\n[page 1]\nFirst page\n\n[page 2]\nSecond page\n", client.sent[0]["messages"][0]["content"])

class ExcerptsTest(FileTest):
    async def test_prompt_goes_just_before_the_question_and_is_not_saved(self):
        self.patch(config, "context_length", 10)
        await self.attach("First part.\n\nSecond part.")
        client = self.use([chunk("ok", done=True)], [chunk("ok", done=True)])
        await self.reply("q")
        prompt = excerpts_prompt("notes.txt", "[lines 1-3]\nFirst part.\n\nSecond part.")
        self.assertEqual(self.sent_contents(client), [DATE_LINE, prompt, "q"])
        self.assertEqual(self.conversation.last_turn.file_context, "1 excerpts")
        await self.reply("next")
        self.assertEqual(self.sent_contents(client, 1), [DATE_LINE, "q", "ok", prompt, "next"])

    async def test_best_chunks_in_file_order(self):
        sent = await self.excerpts(self.long_text(), "tell me about topic3")
        # Topic 3 wins; the rest tie, and ties go to the later chunks
        self.assertEqual(re.findall(r"^\[line (\d+)\]", sent, re.M), ["7", "9", "11", "13", "15"])
        self.assertEqual(self.conversation.last_turn.file_context, "5 excerpts")

    async def test_previous_question_counts_at_a_lower_weight(self):
        self.patch(config, "context_length", 10)
        await self.attach(self.long_text())
        client = self.use([chunk("a", done=True)], [chunk("b", done=True)])
        await self.reply("topic0")
        await self.reply("topic3")
        sent = client.sent[1]["messages"][3]["content"]
        self.assertEqual(re.findall(r"^\[line (\d+)\]", sent, re.M), ["1", "7", "11", "13", "15"])

    async def test_chunk_starts_with_the_end_of_the_one_before(self):
        first, second = paragraph(0), paragraph(1)
        sent = await self.excerpts(f"{first}\n\n{second}")
        self.assertIn(f"[line 1]\n{first}\n\n[line 3]\n{first[-200:]}\n\n{second}\n\nEnd of excerpts", sent)

    async def test_long_paragraph_is_cut_at_a_sentence_end(self):
        sentence = "A sentence of about forty characters long. "
        text = sentence * 50
        sent = await self.excerpts(text)
        first = sentence * 34
        self.assertIn(f"[line 1]\n{first.strip()}\n\n", sent)

    async def test_paragraph_over_several_lines_is_labelled_with_the_span(self):
        sent = await self.excerpts("One.\nTwo.\nThree.\n\n" + "\n\n".join(paragraph(i) for i in range(1, 3)))
        self.assertIn("[lines 1-5]\nOne.\nTwo.\nThree.\n\n" + paragraph(1), sent)

    async def test_pdf_chunk_spans_pages(self):
        sent = await self.excerpts(pdf("First page", "Second page"), name="paper.pdf")
        self.assertIn("[pages 1-2]\nFirst page\n\nSecond page\n\nEnd of excerpts", sent)

    async def test_search_failure_ends_the_turn(self):
        self.patch(config, "context_length", 10)
        await self.attach("First part.\n\nSecond part.")
        self.embedder.error = OSError("cache gone")
        client = self.use([chunk("never", done=True)])
        self.assertEqual(await self.reply(), [Failed("could not search the loaded file: cache gone")])
        self.assertEqual(client.sent, [])

class EmbeddingTest(FileTest):
    def setUp(self):
        super().setUp()
        self.patch(config, "context_length", 10)

    async def test_embeds_once_and_reuses_the_cache(self):
        path = self.write(self.long_text())
        await self.load(path)
        self.assertEqual(len(self.embedder.calls), 1)
        self.assertTrue(self.embedder.calls[0][1][0].startswith("title: none | text: topic0"))
        await self.load(path)
        self.assertEqual(len(self.embedder.calls), 1)

    async def test_changed_file_is_embedded_again(self):
        await self.load(self.write(self.long_text()))
        await self.load(self.write(self.long_text(9)))
        self.assertEqual(len(self.embedder.calls), 2)

    async def test_broken_cache_is_ignored(self):
        path = self.write(self.long_text())
        await self.load(path)
        for cache in (self.folder / "cache").iterdir():
            cache.write_text("not json")
        await self.load(path)
        self.assertEqual(len(self.embedder.calls), 2)

    async def test_progress_counts_chunks_in_batches(self):
        progress = []
        await self.load(self.write(self.long_text(20)), lambda done, total: progress.append((done, total)))
        self.assertEqual(progress, [(0, 20), (16, 20), (20, 20)])

    async def test_cancelling_stops_at_the_next_batch(self):
        started, release = threading.Event(), threading.Event()
        def embed(model, input):
            started.set()
            release.wait(5)
            self.embedder.calls.append((model, list(input)))
            return NS(embeddings=[vector(text) for text in input])
        self.embedder.embed = embed
        task = asyncio.create_task(self.load(self.write(self.long_text(20))))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.embedder.calls), 1)
        self.assertFalse((self.folder / "cache").exists())

class HowSentTest(FileTest):
    async def test_short_file(self):
        file = await self.load(self.write("x" * 700))
        self.assertEqual(file.how_sent, "whole, about 200 tokens")

    async def test_long_file(self):
        self.patch(config, "context_length", 10)
        file = await self.load(self.write(self.long_text()))
        self.assertEqual(file.how_sent, "8 chunks; each question gets the 5 most related")

class RefusedFileTest(FileTest):
    async def refused(self, path, message):
        with self.assertRaisesRegex(ValueError, message):
            await self.load(path)

    async def test_binary(self):
        await self.refused(self.write(b"\0binary"), "this looks like a binary file; only text files and PDFs can be loaded")

    async def test_empty(self):
        await self.refused(self.write("  \n"), "the file is empty")

    async def test_too_big(self):
        self.patch(loaded_file, "MAX_FILE_BYTES", 1024 * 1024)
        await self.refused(self.write("x" * (1024 * 1024 + 1)), "the file is bigger than 1 MB")

    async def test_too_many_chunks(self):
        self.patch(config, "context_length", 10)
        self.patch(loaded_file, "MAX_CHUNKS", 3)
        await self.refused(self.write(self.long_text(4)), r"the file is too long \(4 chunks, at most 3\)")
        self.assertEqual(self.embedder.calls, [])
