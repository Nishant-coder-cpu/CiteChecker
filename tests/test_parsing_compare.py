from citecheck.compare import best_candidate, compare_fields, format_entry, title_similarity
from citecheck.models import ParsedReference, SourceRecord
from citecheck.parsing import normalize_doi, parse_references

from conftest import DEEP_LEARNING


def test_doi_normalization_variants():
    assert normalize_doi("doi:10.1038/NATURE14539.") == "10.1038/nature14539"
    assert normalize_doi("https://doi.org/10.1145/3065386") == "10.1145/3065386"
    assert normalize_doi("see (10.1016/S0140-6736(20)30183-5)") == "10.1016/s0140-6736(20)30183-5"
    assert normalize_doi("no doi here") is None


def test_parse_numbered_list_with_claims_and_preamble():
    text = """Please verify these:
1. LeCun, Y., Bengio, Y., & Hinton, G. (2015). Deep learning. Nature. doi:10.1038/nature14539
   Claim: Deep learning learns representations.
2. He, K. (2014). Deep residual learning for image recognition. CVPR. | claim: Residual nets are deep
3. Some paper about cells."""
    refs = parse_references(text)
    assert [r.index for r in refs] == [1, 2, 3]
    assert refs[0].doi == "10.1038/nature14539"
    assert refs[0].year == 2015 and refs[0].title == "Deep learning" and refs[0].first_author == "LeCun"
    assert refs[0].claim == "Deep learning learns representations."
    assert refs[1].claim == "Residual nets are deep" and "claim" not in refs[1].raw.lower()
    assert refs[2].doi is None and refs[2].year is None


def test_parse_unnumbered_lines_ignores_chatter():
    refs = parse_references("hi, can you check\nLeCun (2015). Deep learning. Nature.\nthanks")
    assert len(refs) == 1 and refs[0].year == 2015


def test_parse_empty_and_garbage():
    assert parse_references("") == []
    assert parse_references("hello there") == []


def test_title_similarity_rejects_lookalikes():
    assert title_similarity("Attention is all you need", "Attention Is All You Need") == 1.0
    assert title_similarity("Deep residual learning", "Deep residual learning: a review") >= 0.9
    # two-word titles are too generic for subtitle matching
    assert title_similarity("Deep learning", "Deep learning: a review") < 0.9
    assert title_similarity("Attention is all you need",
                            "Text-Guided Attention is All You Need for Zero-Shot Robustness") < 0.9
    assert title_similarity("Attention is all you need",
                            "Attention Is All You Need In Speech Separation") < 0.9


def test_compare_fields_only_cited_fields():
    ref = ParsedReference(index=1, raw="x", title="Deep learning", year=2013)
    checks = {c.field: c for c in compare_fields(ref, DEEP_LEARNING)}
    assert set(checks) == {"title", "year"}
    assert checks["title"].matches and not checks["year"].matches


def test_search_candidate_with_wrong_year_is_rejected():
    ref = ParsedReference(index=1, raw="Vaswani (2017). Attention is all you need.",
                          title="Attention is all you need", year=2017)
    reupload = SourceRecord(source="openalex", url="u", title="Attention Is All You Need", year=2025,
                            doi="10.65215/x", authors=["Vaswani"])
    rec, _, reason = best_candidate(ref, [reupload])
    assert rec is None and "year differs" in reason


def test_format_entry_uses_only_retrieved_fields():
    entry = format_entry(DEEP_LEARNING)
    assert entry == "LeCun, Bengio, & Hinton (2015). Deep learning. Nature. https://doi.org/10.1038/nature14539"
    bare = SourceRecord(source="crossref", url="u", title="T")
    assert format_entry(bare) == "[authors not listed] (n.d.). T."


def test_run_together_references_are_split():
    text = ("Check my references, please: Kingma, D. P., & Ba, J. (2015). Adam: A method for stochastic optimisation. "
            "ICLR. Krizhevsky, A., Sutskever, I., & Hinton, G. (2017). ImageNet classification with deep convolutional "
            "neural networks. Communications of the ACM. doi:10.1038/nature14539 Jumper, J., et al. (2021). Highly "
            "accurate protein structure prediction with AlphaFold. Nature. doi:10.1038/s41586-021-03819-2 Claim: "
            "AlphaFold predictions are far less accurate than experimental structures. Smith, J. (2021). Quantum "
            "blockchain methods for citation integrity. Journal of Imaginary Results. doi:10.9999/fake.2021.001")
    refs = parse_references(text)
    assert [r.first_author for r in refs] == ["Kingma", "Krizhevsky", "Jumper", "Smith"]
    assert refs[1].doi == "10.1038/nature14539" and refs[3].doi == "10.9999/fake.2021.001"
    assert refs[2].claim == "AlphaFold predictions are far less accurate than experimental structures."
    assert refs[0].title.startswith("Adam")
