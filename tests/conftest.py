from collections.abc import Iterator
from io import BytesIO

import pytest
from reportlab.pdfgen import canvas

from app.schemas import DraftAnswer, Passage


@pytest.fixture
def pdf_bytes() -> bytes:
    output = BytesIO()
    page = canvas.Canvas(output)
    page.drawString(72, 720, "This is the cover page.")
    page.showPage()
    page.drawString(72, 720, "The service runs on Google Cloud Platform (GCP).")
    page.save()
    return output.getvalue()


class FakeProvider:
    def __init__(self) -> None:
        self.embedding_batches: list[int] = []
        self.answer_calls = 0
        self.active = 0
        self.max_active = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedding_batches.append(len(texts))
        return [
            [
                float("cloud" in text.lower() or "gcp" in text.lower()),
                float("incident" in text.lower()),
                float("backup" in text.lower()),
                1.0,
            ]
            for text in texts
        ]

    async def answer(self, question: str, passages: list[Passage]) -> DraftAnswer:
        import asyncio

        self.answer_calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.001)
            if "unknown" in question.lower():
                return DraftAnswer("Data-Not-Found", "No matching evidence.", [], False, "low")
            match = next((passage for passage in passages if "GCP" in passage.text), None)
            if match:
                return DraftAnswer(
                    "Google Cloud Platform",
                    "The source explicitly names GCP.",
                    [match.id],
                    True,
                    "high",
                )
            return DraftAnswer("Data-Not-Found", "No matching evidence.", [], False, "low")
        finally:
            self.active -= 1


@pytest.fixture
def fake_provider() -> Iterator[FakeProvider]:
    yield FakeProvider()
