from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MarketRegime(str, Enum):
    TREND_UP = "Trend Up"
    TREND_DOWN = "Trend Down"
    RANGE = "Range"
    CHOPPY = "Choppy / Indeterminate"


class FinalDecision(str, Enum):
    PAPER_LONG = "Paper Long"
    PAPER_SHORT = "Paper Short"
    STAND_ASIDE = "Stand Aside"
    EXIT_EARLY = "Exit Early"


@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class StructureAssessment:
    swing_highs: list[float]
    swing_lows: list[float]
    breakout: bool
    breakdown: bool
    retest_hold: bool
    retest_fail: bool
    rejection_failed_reclaim: bool
    quality: float
    notes: list[str] = field(default_factory=list)


@dataclass
class TradeCandidate:
    direction: FinalDecision
    entry_zone: tuple[float, float]
    stop_loss: float
    target: float
    risk_reward: float
    valid: bool
    invalid_reason: str | None = None


@dataclass
class DecisionResult:
    timestamp: datetime
    market_regime: MarketRegime
    trade_bias: str
    entry_zone: tuple[float, float] | None
    stop_loss: float | None
    target: float | None
    atr: float
    risk_reward_ratio: float | None
    confidence_score: float
    final_decision: FinalDecision
    reasoning: list[str] = field(default_factory=list)
    position_size_btc: float = 0.0
    hard_rule_failures: list[str] = field(default_factory=list)


@dataclass
class JournalEntry:
    timestamp: datetime
    market_regime: str
    decision: str
    entry: str
    stop: str
    target: str
    projected_rr: float | None
    confidence: float
    reasoning: str
    what_would_have_made_this_stand_aside: str
    position_size_btc: float = 0.0
    intended_position_size_btc: float | None = None
    executed_size_btc: float | None = None
    unfilled_size_btc: float | None = None
    entry_fill_fraction: float | None = None
    target_exit_size_btc: float | None = None
    stop_exit_size_btc: float | None = None
    residual_position_size_btc: float | None = None
    realized_exit_size_btc: float | None = None
    exit_fill_count: int | None = None
    residual_lifecycle_stage: int | None = None
    residual_last_event_ts: datetime | None = None
    entry_fill_timestamp: datetime | None = None
    realized_gross_so_far_usd: float | None = None
    position_id: str | None = None
    position_open_timestamp: datetime | None = None
    position_status: str | None = None
    position_close_timestamp: datetime | None = None
    executed_entry_price: float | None = None
    executed_notional_usd: float | None = None
    actual_outcome: str = "TBD"
    actual_exit_price: float | None = None
    # semantic: net realized pnl for full paper position after friction
    actual_pnl_usd: float | None = None
    actual_pnl_basis: str = "net"
    actual_pnl_pct: float | None = None
    held_duration_hours: float | None = None
    remediation_status: str | None = None
    remediation_bundle_id: str | None = None
    remediation_evidence_ref: str | None = None
    remediation_evidence_manifest_json: str | None = None
    remediation_evidence_manifest_hash_sha256: str | None = None
    remediation_evidence_retrieval_proofs_json: str | None = None
    remediation_evidence_retrieval_proofs_hash_sha256: str | None = None
    remediation_retrieval_receipt_attestor_key_lifecycle_evidence_json: str | None = None
    remediation_retrieval_receipt_attestor_key_lifecycle_evidence_hash_sha256: str | None = None
    remediation_retrieval_receipt_attestor_key_lifecycle_evidence_anchor_key_instance_id: str | None = None
    remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_json: str | None = None
    remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_hash_sha256: str | None = None
    remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_json: str | None = None
    remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_hash_sha256: str | None = None
    remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_json: str | None = None
    remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_hash_sha256: str | None = None
    remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_provenance_json: str | None = None
    remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_provenance_hash_sha256: str | None = None
    remediation_parent_lineage_tier: str | None = None
    remediation_operator: str | None = None
    remediation_reason: str | None = None
    remediation_timestamp_utc: datetime | None = None
    remediation_evidence_payload: str | None = None
    remediation_evidence_hash_sha256: str | None = None
    remediation_external_source: str | None = None
    remediation_signer_id: str | None = None
    remediation_key_instance_id: str | None = None
    remediation_key_epoch: int | None = None
    remediation_key_serial: str | None = None
    remediation_issuer_id: str | None = None
    remediation_certificate_chain_id: str | None = None
    remediation_certificate_fingerprint_sha256: str | None = None
    remediation_certificate_path_fingerprints: str | None = None
    remediation_certificate_path_anchor_fingerprint_sha256: str | None = None
    remediation_certificate_objects: str | None = None
    remediation_certificate_der_hex_chain: str | None = None
    remediation_certificate_link_signatures: str | None = None
    remediation_certificate_link_signature_scheme: str | None = None
    remediation_signature: str | None = None
    remediation_signature_scheme: str | None = None
    review_note: str = "TBD"


@dataclass
class KillSwitchState:
    consecutive_losses: int = 0
    stand_aside_until: datetime | None = None
    review_required: bool = False


@dataclass
class SimulatedEquityState:
    starting_equity_usd: float
    current_equity_usd: float
    high_water_mark_usd: float
    current_consecutive_losses: int = 0
    max_consecutive_losses: int = 0
    max_drawdown_usd: float = 0.0
    max_drawdown_pct: float = 0.0
