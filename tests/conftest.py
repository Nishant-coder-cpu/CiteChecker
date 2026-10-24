import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from citecheck import sources  # noqa: E402
from citecheck.models import SourceRecord  # noqa: E402

DEEP_LEARNING = SourceRecord(
    source="crossref", url="https://doi.org/10.1038/nature14539", doi="10.1038/nature14539",
    title="Deep learning", authors=["LeCun", "Bengio", "Hinton"], year=2015, venue="Nature",
)
ALEXNET = SourceRecord(
    source="crossref", url="https://doi.org/10.1145/3065386", doi="10.1145/3065386",
    title="ImageNet classification with deep convolutional neural networks",
    authors=["Krizhevsky", "Sutskever", "Hinton"], year=2017, venue="Communications of the ACM",
    abstract=("We trained a large, deep convolutional neural network to classify the 1.2 million "
              "high-resolution images in the ImageNet LSVRC-2010 contest into the 1000 different classes. "
              "On the test data, we achieved top-1 and top-5 error rates of 37.5% and 17.0%, which is "
              "considerably better than the previous state-of-the-art."),
)
RESNET = SourceRecord(
    source="crossref", url="https://doi.org/10.1109/cvpr.2016.90", doi="10.1109/cvpr.2016.90",
    title="Deep Residual Learning for Image Recognition", authors=["He", "Zhang", "Ren", "Sun"],
    year=2016, venue="2016 IEEE Conference on Computer Vision and Pattern Recognition (CVPR)",
)
BY_DOI = {r.doi: r for r in (DEEP_LEARNING, ALEXNET, RESNET)}


@pytest.fixture
def fake_registries(monkeypatch):
    """Crossref/OpenAlex stand-ins backed by the fixture records above."""
    calls = []

    def by_doi(source):
        def fn(doi, session=None):
            calls.append((source, "doi", doi))
            rec = BY_DOI.get(doi)
            if rec is None:
                return sources.Lookup(source, not_found=True)
            return sources.Lookup(source, records=[rec.model_copy(update={"source": source})])
        return fn

    def search(source):
        def fn(query, rows=3, session=None):
            calls.append((source, "search", query))
            q = query.lower()
            hits = [r.model_copy(update={"source": source}) for r in BY_DOI.values()
                    if r.title.lower() in q or q in r.title.lower()]
            return sources.Lookup(source, records=hits, not_found=not hits)
        return fn

    monkeypatch.setattr(sources, "crossref_by_doi", by_doi("crossref"))
    monkeypatch.setattr(sources, "openalex_by_doi", by_doi("openalex"))
    monkeypatch.setattr(sources, "crossref_search", search("crossref"))
    monkeypatch.setattr(sources, "openalex_search", search("openalex"))
    return calls


@pytest.fixture
def down_registries(monkeypatch):
    def fail(source):
        return lambda *a, **k: sources.Lookup(source, error=f"{source} timed out after 10s")
    for name, src in [("crossref_by_doi", "crossref"), ("openalex_by_doi", "openalex"),
                      ("crossref_search", "crossref"), ("openalex_search", "openalex")]:
        monkeypatch.setattr(sources, name, fail(src))


class FakeLLM:
    """Mimics AsyncOpenAI.chat.completions.create with a canned reply."""

    def __init__(self, reply: str | Exception):
        self.reply = reply
        self.chat = self
        self.completions = self
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply

        class _Msg:
            content = self.reply

        class _Choice:
            message = _Msg()

        class _Result:
            choices = [_Choice()]

        return _Result()
