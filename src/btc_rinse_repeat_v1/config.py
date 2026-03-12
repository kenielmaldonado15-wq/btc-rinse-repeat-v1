from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class StrategyConfig:
    min_risk_reward: float = 1.6
    min_volume_quality: float = 0.45
    min_structure_quality: float = 0.50

    min_atr: float = 80.0
    max_atr: float = 2400.0

    atr_stop_multiple: float = 1.25
    atr_target_multiple: float = 2.0
    per_trade_risk_fraction: float = 0.01
    scoring_threshold: float = 0.55

    # Regime v1.1+
    regime_adx_trend_min: float = 22.0
    regime_slope_min: float = 0.003
    regime_chop_max_efficiency: float = 0.38

    # Timeframe alignment
    min_alignment_score: float = 0.0

    # Entry zone validation
    chase_atr_tolerance: float = 0.15

    # Early invalidation
    early_exit_atr_multiple: float = 0.35
    early_exit_require_structure_break: bool = True

    # Event risk (v1.2)
    manual_event_risk_active: bool = False
    event_risk_start_hour_utc: int | None = None
    event_risk_end_hour_utc: int | None = None

    # Live data (v1.2)
    exchange_id: str = "binance"
    symbol: str = "BTC/USDT"
    primary_timeframe: str = "4h"
    confirm_timeframe: str = "1h"
    primary_limit: int = 250
    confirm_limit: int = 300
    max_retries: int = 3
    poll_interval_minutes: int = 10

    # Paper realism (v1.2 hardening)
    taker_fee_rate: float = 0.001
    slippage_rate: float = 0.0005
    outcome_horizon_hours: int = 48
    partial_take_profit_fraction: float = 1.0

    # Paper equity
    starting_equity_usd: float = 100000.0

    # Kill switch
    kill_switch_consecutive_losses: int = 3
    kill_switch_hours: int = 24

    # Runtime trust-tier handling for legacy realized rows without position_id
    runtime_legacy_row_policy: str = "upgrade_deterministic"  # [upgrade_deterministic|reject]
    runtime_low_context_legacy_policy: str = "bound_event_only"  # [bound_event_only|reject]
    runtime_sparse_history_policy: str = "reconstruct_supportable"  # [reconstruct_supportable|bound_event_only|reject]

    # External evidence signer binding for manual remediation
    remediation_external_signature_policy: str = "require"  # [require|allow_internal_attestation]
    remediation_trusted_signer_ids: tuple[str, ...] = ("ops-ledger",)
    remediation_revoked_signer_ids: tuple[str, ...] = ()
    remediation_signer_revoked_before_utc: dict[str, str] | None = None
    remediation_revocation_policy: str = "reject_all_revoked"  # [reject_all_revoked|allow_pre_revocation_only]
    remediation_key_instance_revoked_ids: tuple[str, ...] = ()
    remediation_key_instance_revoked_before_utc: dict[str, str] | None = None
    remediation_key_rotation_policy: str = "allow_historical_non_active"  # [require_active_key_lineage|allow_historical_non_active]
    remediation_signer_active_key_instances: dict[str, tuple[str, ...]] | None = None
    remediation_trusted_issuer_ids: tuple[str, ...] = ("ops-root-ca",)
    remediation_revoked_issuer_ids: tuple[str, ...] = ()
    remediation_issuer_revoked_before_utc: dict[str, str] | None = None
    remediation_issuer_revocation_policy: str = "reject_all_revoked"  # [reject_all_revoked|allow_pre_revocation_only]
    remediation_issuer_chain_policy: str = "allow_unmapped_key_instance"  # [require_key_instance_binding|allow_unmapped_key_instance]
    remediation_key_instance_issuer_bindings: dict[str, str] | None = None
    remediation_certificate_path_policy: str = "require_verified_path"  # [require_verified_path|allow_metadata_only]
    remediation_certificate_path_min_depth: int = 2
    remediation_certificate_path_max_depth: int = 6
    remediation_trusted_root_fingerprints: tuple[str, ...] = ("f" * 64,)
    remediation_issuer_root_fingerprints: dict[str, str] | None = None
    remediation_certificate_link_signature_scheme: str = "der_tbs_rsa_sha256_v1"
    remediation_certificate_link_policy: str = "allow_metadata_only"  # [require_verified_links|allow_metadata_only]
    remediation_evidence_manifest_policy: str = "allow_legacy_no_manifest"  # [require_complete_manifest|allow_legacy_no_manifest]
    remediation_evidence_retrieval_policy: str = "allow_manifest_without_retrieval_proofs"  # [require_retrieval_proofs|allow_manifest_without_retrieval_proofs]
    remediation_retrieval_receipt_auth_policy: str = "allow_unsigned_receipts"  # [require_authentic_receipts|allow_unsigned_receipts]
    remediation_retrieval_receipt_signature_scheme: str = "sha256_receipt_bind_v1"
    remediation_trusted_retrieval_receipt_attestor_ids: tuple[str, ...] = ("ops-retrieval-ledger",)
    remediation_revoked_retrieval_receipt_attestor_ids: tuple[str, ...] = ()
    remediation_retrieval_receipt_attestor_revoked_before_utc: dict[str, str] | None = None
    remediation_retrieval_receipt_attestor_lifecycle_policy: str = "reject_all_revoked"  # [reject_all_revoked|allow_pre_revocation_only]
    remediation_retrieval_receipt_attestor_rotation_policy: str = "allow_historical_non_active"  # [require_active_attestor_lineage|allow_historical_non_active]
    remediation_retrieval_receipt_active_attestor_ids: tuple[str, ...] = ()
    remediation_trusted_retrieval_receipt_attestor_key_instances: dict[str, tuple[str, ...]] | None = None
    remediation_revoked_retrieval_receipt_attestor_key_instances: tuple[str, ...] = ()
    remediation_retrieval_receipt_attestor_key_instance_revoked_before_utc: dict[str, str] | None = None
    remediation_retrieval_receipt_attestor_key_rotation_policy: str = "allow_historical_non_active"  # [require_active_key_lineage|allow_historical_non_active]
    remediation_retrieval_receipt_attestor_key_lifecycle_evidence_policy: str = "allow_local_policy_only"  # [require_anchored_evidence|allow_local_policy_only]
    remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_ids: tuple[str, ...] = ("ops-retrieval-key-ledger",)
    remediation_retrieval_receipt_attestor_key_lifecycle_signature_scheme: str = "sha256_key_lifecycle_bind_v1"
    remediation_retrieval_receipt_attestor_key_lifecycle_anchor_key_governance_policy: str = "allow_ungoverned_anchor_keys"  # [require_governed_anchor_keys|allow_ungoverned_anchor_keys]
    remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances: dict[str, tuple[str, ...]] | None = None
    remediation_revoked_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances: tuple[str, ...] = ()
    remediation_retrieval_receipt_attestor_key_lifecycle_anchor_key_instance_revoked_before_utc: dict[str, str] | None = None
    remediation_retrieval_receipt_attestor_key_lifecycle_anchor_key_lifecycle_policy: str = "reject_all_revoked"  # [reject_all_revoked|allow_pre_revocation_only]
    remediation_retrieval_receipt_attestor_key_lifecycle_anchor_key_rotation_policy: str = "allow_historical_non_active"  # [require_active_key_lineage|allow_historical_non_active]
    remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_policy: str = "allow_local_policy_only"  # [require_anchored_dataset|allow_local_policy_only]
    remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_anchor_ids: tuple[str, ...] = ("ops-retrieval-anchor-governance-ledger",)
    remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_signature_scheme: str = "sha256_anchor_governance_dataset_bind_v1"
    remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_policy: str = "allow_local_policy_only"  # [require_anchored_identity_provenance|allow_local_policy_only]
    remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_ids: tuple[str, ...] = ("ops-retrieval-anchor-governance-ledger",)
    remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_active_ids: tuple[str, ...] = ()
    remediation_revoked_retrieval_receipt_attestor_key_lifecycle_governance_anchor_ids: tuple[str, ...] = ()
    remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_revoked_before_utc: dict[str, str] | None = None
    remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_lifecycle_policy: str = "reject_all_revoked"  # [reject_all_revoked|allow_pre_revocation_only]
    remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_rotation_policy: str = "allow_historical_non_active"  # [require_active_anchor_lineage|allow_historical_non_active]
    remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_anchor_ids: tuple[str, ...] = ("ops-retrieval-anchor-identity-ledger",)
    remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_signature_scheme: str = "sha256_governance_anchor_identity_bind_v1"
    remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_governance_policy: str = "allow_local_policy_only"  # [require_anchored_identity_provenance_anchor_governance|allow_local_policy_only]
    remediation_trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_ids: tuple[str, ...] = ("ops-retrieval-anchor-identity-ledger",)
    remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_active_ids: tuple[str, ...] = ()
    remediation_revoked_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_ids: tuple[str, ...] = ()
    remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_revoked_before_utc: dict[str, str] | None = None
    remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_lifecycle_policy: str = "reject_all_revoked"  # [reject_all_revoked|allow_pre_revocation_only]
    remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_rotation_policy: str = "allow_historical_non_active"  # [require_active_anchor_lineage|allow_historical_non_active]
    remediation_trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_anchor_ids: tuple[str, ...] = ("ops-retrieval-anchor-identity-root-ledger",)
    remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_signature_scheme: str = "sha256_identity_provenance_anchor_bind_v1"
    remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_governance_policy: str = "allow_local_policy_only"  # [require_anchored_root_provenance_anchor_governance|allow_local_policy_only]
    remediation_trusted_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_ids: tuple[str, ...] = ("ops-retrieval-anchor-identity-root-ledger",)
    remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_active_ids: tuple[str, ...] = ()
    remediation_revoked_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_ids: tuple[str, ...] = ()
    remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_revoked_before_utc: dict[str, str] | None = None
    remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_lifecycle_policy: str = "reject_all_revoked"  # [reject_all_revoked|allow_pre_revocation_only]
    remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_rotation_policy: str = "allow_historical_non_active"  # [require_active_anchor_lineage|allow_historical_non_active]
    remediation_trusted_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_provenance_anchor_ids: tuple[str, ...] = ("ops-retrieval-anchor-root-root-ledger",)
    remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_provenance_signature_scheme: str = "sha256_root_provenance_anchor_bind_v1"



def load_strategy_config(path: str | None = None) -> StrategyConfig:
    if not path:
        return StrategyConfig()

    p = Path(path)
    if not p.exists():
        return StrategyConfig()

    # lightweight: JSON only for explicit, auditable overrides
    raw = json.loads(p.read_text(encoding="utf-8"))
    defaults = asdict(StrategyConfig())
    merged = {**defaults, **raw}
    if isinstance(merged.get("remediation_trusted_signer_ids"), list):
        merged["remediation_trusted_signer_ids"] = tuple(str(v) for v in merged["remediation_trusted_signer_ids"])
    if isinstance(merged.get("remediation_revoked_signer_ids"), list):
        merged["remediation_revoked_signer_ids"] = tuple(str(v) for v in merged["remediation_revoked_signer_ids"])
    if merged.get("remediation_signer_revoked_before_utc") is None:
        merged["remediation_signer_revoked_before_utc"] = {}
    if isinstance(merged.get("remediation_key_instance_revoked_ids"), list):
        merged["remediation_key_instance_revoked_ids"] = tuple(str(v) for v in merged["remediation_key_instance_revoked_ids"])
    if merged.get("remediation_key_instance_revoked_before_utc") is None:
        merged["remediation_key_instance_revoked_before_utc"] = {}
    active = merged.get("remediation_signer_active_key_instances")
    if active is None:
        merged["remediation_signer_active_key_instances"] = {}
    elif isinstance(active, dict):
        merged["remediation_signer_active_key_instances"] = {
            str(k): tuple(str(v) for v in vals) if isinstance(vals, list) else (str(vals),)
            for k, vals in active.items()
        }
    if isinstance(merged.get("remediation_trusted_issuer_ids"), list):
        merged["remediation_trusted_issuer_ids"] = tuple(str(v) for v in merged["remediation_trusted_issuer_ids"])
    if isinstance(merged.get("remediation_revoked_issuer_ids"), list):
        merged["remediation_revoked_issuer_ids"] = tuple(str(v) for v in merged["remediation_revoked_issuer_ids"])
    if merged.get("remediation_issuer_revoked_before_utc") is None:
        merged["remediation_issuer_revoked_before_utc"] = {}
    bindings = merged.get("remediation_key_instance_issuer_bindings")
    if bindings is None:
        merged["remediation_key_instance_issuer_bindings"] = {}
    elif isinstance(bindings, dict):
        merged["remediation_key_instance_issuer_bindings"] = {str(k): str(v) for k, v in bindings.items()}
    if isinstance(merged.get("remediation_trusted_root_fingerprints"), list):
        merged["remediation_trusted_root_fingerprints"] = tuple(str(v) for v in merged["remediation_trusted_root_fingerprints"])
    if isinstance(merged.get("remediation_trusted_retrieval_receipt_attestor_ids"), list):
        merged["remediation_trusted_retrieval_receipt_attestor_ids"] = tuple(str(v) for v in merged["remediation_trusted_retrieval_receipt_attestor_ids"])
    if isinstance(merged.get("remediation_revoked_retrieval_receipt_attestor_ids"), list):
        merged["remediation_revoked_retrieval_receipt_attestor_ids"] = tuple(str(v) for v in merged["remediation_revoked_retrieval_receipt_attestor_ids"])
    if merged.get("remediation_retrieval_receipt_attestor_revoked_before_utc") is None:
        merged["remediation_retrieval_receipt_attestor_revoked_before_utc"] = {}
    if isinstance(merged.get("remediation_retrieval_receipt_active_attestor_ids"), list):
        merged["remediation_retrieval_receipt_active_attestor_ids"] = tuple(str(v) for v in merged["remediation_retrieval_receipt_active_attestor_ids"])
    key_active = merged.get("remediation_trusted_retrieval_receipt_attestor_key_instances")
    if key_active is None:
        merged["remediation_trusted_retrieval_receipt_attestor_key_instances"] = {}
    elif isinstance(key_active, dict):
        merged["remediation_trusted_retrieval_receipt_attestor_key_instances"] = {
            str(k): tuple(str(v) for v in vals) if isinstance(vals, list) else (str(vals),)
            for k, vals in key_active.items()
        }
    if isinstance(merged.get("remediation_revoked_retrieval_receipt_attestor_key_instances"), list):
        merged["remediation_revoked_retrieval_receipt_attestor_key_instances"] = tuple(str(v) for v in merged["remediation_revoked_retrieval_receipt_attestor_key_instances"])
    if merged.get("remediation_retrieval_receipt_attestor_key_instance_revoked_before_utc") is None:
        merged["remediation_retrieval_receipt_attestor_key_instance_revoked_before_utc"] = {}
    if isinstance(merged.get("remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_ids"), list):
        merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_ids"] = tuple(str(v) for v in merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_ids"])
    lifecycle_anchor_keys = merged.get("remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances")
    if lifecycle_anchor_keys is None:
        merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances"] = {}
    elif isinstance(lifecycle_anchor_keys, dict):
        merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances"] = {
            str(k): tuple(str(v) for v in vals) if isinstance(vals, list) else (str(vals),)
            for k, vals in lifecycle_anchor_keys.items()
        }
    if isinstance(merged.get("remediation_revoked_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances"), list):
        merged["remediation_revoked_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances"] = tuple(str(v) for v in merged["remediation_revoked_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances"])
    if merged.get("remediation_retrieval_receipt_attestor_key_lifecycle_anchor_key_instance_revoked_before_utc") is None:
        merged["remediation_retrieval_receipt_attestor_key_lifecycle_anchor_key_instance_revoked_before_utc"] = {}
    if isinstance(merged.get("remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_anchor_ids"), list):
        merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_anchor_ids"] = tuple(str(v) for v in merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_anchor_ids"])
    if isinstance(merged.get("remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_ids"), list):
        merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_ids"] = tuple(str(v) for v in merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_ids"])
    if isinstance(merged.get("remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_active_ids"), list):
        merged["remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_active_ids"] = tuple(str(v) for v in merged["remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_active_ids"])
    if isinstance(merged.get("remediation_revoked_retrieval_receipt_attestor_key_lifecycle_governance_anchor_ids"), list):
        merged["remediation_revoked_retrieval_receipt_attestor_key_lifecycle_governance_anchor_ids"] = tuple(str(v) for v in merged["remediation_revoked_retrieval_receipt_attestor_key_lifecycle_governance_anchor_ids"])
    if merged.get("remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_revoked_before_utc") is None:
        merged["remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_revoked_before_utc"] = {}
    if isinstance(merged.get("remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_anchor_ids"), list):
        merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_anchor_ids"] = tuple(str(v) for v in merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_anchor_ids"])
    if isinstance(merged.get("remediation_trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_ids"), list):
        merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_ids"] = tuple(str(v) for v in merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_ids"])
    if isinstance(merged.get("remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_active_ids"), list):
        merged["remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_active_ids"] = tuple(str(v) for v in merged["remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_active_ids"])
    if isinstance(merged.get("remediation_revoked_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_ids"), list):
        merged["remediation_revoked_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_ids"] = tuple(str(v) for v in merged["remediation_revoked_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_ids"])
    if merged.get("remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_revoked_before_utc") is None:
        merged["remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_revoked_before_utc"] = {}
    if isinstance(merged.get("remediation_trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_anchor_ids"), list):
        merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_anchor_ids"] = tuple(str(v) for v in merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_anchor_ids"])
    if isinstance(merged.get("remediation_trusted_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_ids"), list):
        merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_ids"] = tuple(str(v) for v in merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_ids"])
    if isinstance(merged.get("remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_active_ids"), list):
        merged["remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_active_ids"] = tuple(str(v) for v in merged["remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_active_ids"])
    if isinstance(merged.get("remediation_revoked_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_ids"), list):
        merged["remediation_revoked_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_ids"] = tuple(str(v) for v in merged["remediation_revoked_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_ids"])
    if merged.get("remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_revoked_before_utc") is None:
        merged["remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_revoked_before_utc"] = {}
    if isinstance(merged.get("remediation_trusted_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_provenance_anchor_ids"), list):
        merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_provenance_anchor_ids"] = tuple(str(v) for v in merged["remediation_trusted_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_provenance_anchor_ids"])
    issuer_roots = merged.get("remediation_issuer_root_fingerprints")
    if issuer_roots is None:
        merged["remediation_issuer_root_fingerprints"] = {"ops-root-ca": "f" * 64}
    elif isinstance(issuer_roots, dict):
        merged["remediation_issuer_root_fingerprints"] = {str(k): str(v) for k, v in issuer_roots.items()}
    return StrategyConfig(**merged)
