# CiteCheck — Academic Citation Audit Agent

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

CiteCheck verifies academic references and tells you which citations need attention.

**Use it for:** checking a bibliography, reference list or draft before submission; spotting wrong DOIs,
wrong years or wrong authors; checking whether a cited paper's abstract supports the claim you cite it for.

## How to use

Paste up to 15 references (one per line, numbered or bulleted). DOIs help. Optionally add a
`Claim:` line under a reference.

```
1. LeCun, Y., Bengio, Y., & Hinton, G. (2015). Deep learning. Nature. doi:10.1038/nature14539
2. Jumper, J., et al. (2021). Highly accurate protein structure prediction with AlphaFold. Nature. doi:10.1038/s41586-021-03819-2
   Claim: AlphaFold predictions are far less accurate than experimental structures.
```

## What you get

- Per-reference status: verified, metadata mismatch, DOI not found, no confident match, insufficient information, or lookup failed.
- Field-level differences (title, year, first author, DOI) against live **Crossref** and **OpenAlex** records, with source links.
- Suggested bibliography entries built **only** from retrieved registry metadata.
- Claim support (plausible / uncertain / contradicted) judged by ASI1 from the **abstract only**, accepted only when it quotes the abstract verbatim.
- An independent quality check by a separate **CiteCheck Verifier agent** that recomputes counts, re-validates every correction and re-fetches matched DOIs.

## Limits

A record missing from Crossref/OpenAlex does not prove a citation is fake. A valid DOI does not prove
the paper supports your claim. An abstract is not the full paper. Passing verification means the defined
checks passed, not that the scholarship is correct.
