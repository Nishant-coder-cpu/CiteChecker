"""Structured records shared by the parser, audit pipeline, verifier and report."""

from enum import Enum

from pydantic import BaseModel, Field


class MetadataStatus(str, Enum):
    VERIFIED = "verified"                 # record found, cited fields agree
    MISMATCH = "mismatch"                 # record found, one or more cited fields disagree
    DOI_NOT_FOUND = "doi_not_found"       # DOI given but no registry resolved it
    NOT_FOUND = "not_found"               # no confident match from a bibliographic search
    INSUFFICIENT_INFO = "insufficient_info"  # too little text to search on
    LOOKUP_ERROR = "lookup_error"         # every source failed (timeout, rate limit, ...)


class ClaimStatus(str, Enum):
    NO_CLAIM = "no_claim"                 # user did not attach a claim
    NOT_ASSESSED = "not_assessed"         # claim given but no evidence text or LLM unavailable
    PLAUSIBLE = "plausible"
    UNCERTAIN = "uncertain"
    CONTRADICTED = "contradicted"


class ParsedReference(BaseModel):
    index: int                            # 1-based position in the user's input
    raw: str
    doi: str | None = None
    year: int | None = None
    title: str | None = None
    first_author: str | None = None
    claim: str | None = None


class SourceRecord(BaseModel):
    """Metadata exactly as returned by one registry. Never filled in by the model."""

    source: str                           # "crossref" | "openalex"
    url: str
    doi: str | None = None
    title: str | None = None
    authors: list[str] = Field(default_factory=list)   # family names, in order
    year: int | None = None
    venue: str | None = None
    abstract: str | None = None


class FieldCheck(BaseModel):
    field: str
    cited: str | None
    retrieved: str | None
    matches: bool


class ClaimAssessment(BaseModel):
    status: ClaimStatus
    rationale: str
    evidence_source: str | None = None    # URL of the text the judgment was based on
    quote: str | None = None              # verbatim abstract passage backing the verdict
    method: str                          # "none" | "asi1-llm-on-abstract"


class ReferenceResult(BaseModel):
    index: int
    raw: str
    parsed: ParsedReference
    metadata_status: MetadataStatus
    matched: SourceRecord | None = None
    corroborating_sources: list[str] = Field(default_factory=list)
    field_checks: list[FieldCheck] = Field(default_factory=list)
    corrected_entry: str | None = None
    claim: ClaimAssessment
    notes: list[str] = Field(default_factory=list)  # lookup errors, unresolved reasons
    # ReAct investigator transcript (Thought / Action / Observation lines), when it ran.
    agent_trace: list[str] = Field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        return self.metadata_status != MetadataStatus.VERIFIED or self.claim.status in (
            ClaimStatus.UNCERTAIN, ClaimStatus.CONTRADICTED, ClaimStatus.NOT_ASSESSED,
        )


class AuditSummary(BaseModel):
    total: int
    by_metadata_status: dict[str, int]
    by_claim_status: dict[str, int]
    needs_attention: int
    unresolved: int                       # no metadata record could be confirmed


class AuditReport(BaseModel):
    audit_id: str
    created_at: str
    results: list[ReferenceResult]
    summary: AuditSummary
    sources_used: list[str]
    reflection: list[str] = Field(default_factory=list)  # verifier-feedback rounds, if any


class VerificationResult(BaseModel):
    passed: bool
    total_checks: int
    failed_checks: int
    skipped_checks: int = 0               # e.g. live re-fetch impossible (registry down)
    unresolved_references: list[int]
    issues: list[str]
    summary: str
    verifier: str                         # who actually ran the checks
