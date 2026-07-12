from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Stored state models ────────────────────────────────────────────────────────

class TensionHistory(BaseModel):
    cycle: int
    event: str


class Tension(BaseModel):
    tension_id: str
    created_cycle: int
    created_at: str
    description: str
    status: str = "active"  # active | resolved
    history: list[TensionHistory] = Field(default_factory=list)
    resolution: Optional[dict] = None


class Commitment(BaseModel):
    commitment_id: str
    statement: str
    confidence: float
    dependencies: list[str] = Field(default_factory=list)
    change_condition: str
    origin_cycle: int
    created_at: str
    status: str = "active"  # active | abandoned
    abandoned_at_cycle: Optional[int] = None
    abandon_reason: Optional[str] = None


class TransitionEntry(BaseModel):
    cycle: int
    timestamp: str
    entry: str


class StateFile(BaseModel):
    model_config = ConfigDict(extra="ignore")  # allow old state with last_cycle_mode/last_cycle_opening

    cycle: int = 0
    next_tension_id: int = 1
    next_commitment_id: int = 1
    silence: Optional[dict] = None
    last_silence_ended_cycle: Optional[int] = None
    # Set when the last cycle attempt failed (API error, timeout, etc.). Website can show this.
    last_failure: Optional[dict] = None  # { timestamp, reason, attempted_cycle, message }
    # Phase B: full monitoring feedback for next cycle's context
    last_monitoring: Optional["MonitoringResult"] = None
    # Challenge System v2: ISO datetime of last Sunday weekly review; used by lapse logic
    last_review_at: Optional[str] = None


# ── Phase B: Resistance, monitoring, retrieval ──────────────────────────────────

class MonitoringResult(BaseModel):
    repetition_score: int = 0
    repetition_comment: str = ""
    substance_summary: str = ""
    cycle_summary: str = ""
    threatened_commitments: list["ThreatMapEntry"] = Field(default_factory=list)
    deflection_flags: list[str] = Field(default_factory=list)
    move_classification: str = "none"
    self_reference_ratio: float = 0.0  # 0.0-1.0, fraction about AI's own nature
    has_concrete_grounding: bool = False
    # Weekly review monitoring (populated only for weekly_review cycles)
    review_challenge_engagement: Optional[list[dict]] = None  # [{challenge: str, engaged: bool, note: str}]
    review_defensive: Optional[bool] = None  # true if response absorbs challenges without commitment impact


class InjectionRecord(BaseModel):
    source: str  # human_challenge | counterposition | creative_constraint | pattern_interruption | weekly_review | none
    text: str
    original_weight: float = 0.0
    effective_weight: float = 0.0
    woven_challenge: Optional[dict] = None  # {submission_id, text, relevance_score, submitter_name}
    challenges_addressed: Optional[list[str]] = None  # weekly_review: submission ids with full response
    challenges_mentioned: Optional[list[str]] = None  # weekly_review: submission ids "already encountered"


class ThreatMapEntry(BaseModel):
    commitment_id: str
    explanation: str


# ── Inquiry Engine output schema ───────────────────────────────────────────────

class TensionNew(BaseModel):
    description: str


class TensionResolved(BaseModel):
    tension_id: str
    resolution_note: str


class CommitmentUpdate(BaseModel):
    action: str  # new | update | abandon
    commitment_id: Optional[str] = None
    statement: Optional[str] = None
    confidence: Optional[float] = None
    dependencies: Optional[list[str]] = None
    change_condition: Optional[str] = None
    reason: Optional[str] = None


class ImageDecision(BaseModel):
    create: bool = False
    # What the text cannot say — the gap the image must fill
    beyond_words: Optional[str] = None     # what this entry needs to show that the writing couldn't reach
    visual_energy: Optional[str] = None    # the raw visual force: e.g. "violent contrast", "barely there", "dense and heavy"
    texture: Optional[str] = None          # physical quality of the surface: e.g. "wet ink bleeding", "cracked earth", "molten glass"
    palette: Optional[str] = None          # colour as emotion, not decoration
    temperature: Optional[str] = None      # one word or phrase: "freezing", "feverish", "lukewarm", "white-hot"
    prompt: Optional[str] = None           # final generation prompt — pure visual instruction, no art-historical names
    # Legacy fields kept for backward compat with old cycle records
    concept: Optional[str] = None
    style: Optional[str] = None
    medium: Optional[str] = None
    artist_reference: Optional[str] = None
    why: Optional[str] = None


class LinkCandidate(BaseModel):
    text: str
    kind: str = "person"  # person | work | text
    canonical_name: str
    author: Optional[str] = None  # for kind=work: author name used to disambiguate lookup


class CycleOutput(BaseModel):
    title: str = ""
    mode: str
    thinking: str
    tensions_new: list[TensionNew] = Field(default_factory=list)
    tensions_resolved: list[TensionResolved] = Field(default_factory=list)
    manuscript_update: Optional[str] = None
    transition_entry: Optional[str] = None
    commitment_updates: list[CommitmentUpdate] = Field(default_factory=list)
    summary: Optional[str] = None
    social_output: Optional[str] = None
    image_decision: ImageDecision = Field(default_factory=ImageDecision)
    link_candidates: list[LinkCandidate] = Field(default_factory=list)


# ── Phase C: Web + Filtering + Social ─────────────────────────────────────────

class Submission(BaseModel):
    """Raw visitor submission stored in quarantine."""
    submission_id: str
    timestamp: str
    text: str
    ip_hash: str  # SHA256 of IP, not raw IP
    source: str = "website"  # website | x_reply
    filter_scores: Optional[dict] = None  # populated by filtering pipeline
    status: str = "pending"  # pending | accepted | rejected
    rejection_reason: Optional[str] = None


class ParadigmShift(BaseModel):
    """Major transition event for the Insights page."""
    cycle: int
    timestamp: str
    event_type: str  # manuscript_rewrite | commitment_abandoned | long_tension_resolved
    description: str


# Resolve forward references for Phase B models
StateFile.model_rebuild()
MonitoringResult.model_rebuild()
