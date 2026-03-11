from __future__ import annotations

import json
import math
import hashlib
import re
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, fields
from typing import Any
from pathlib import Path

from .config import StrategyConfig
from .equity import apply_trading_friction
from .models import DecisionResult, FinalDecision, JournalEntry
from .risk import conservative_entry


def build_position_id(timestamp, decision: str, entry: str) -> str:
    return f"{timestamp}|{decision}|{entry}"


REALIZED_PNL_RECONCILIATION_TOLERANCE_USD = 1e-6


JOURNAL_ENTRY_FIELD_NAMES = {f.name for f in fields(JournalEntry)}


def journal_entry_payload(row: dict) -> dict:
    return {k: v for k, v in row.items() if k in JOURNAL_ENTRY_FIELD_NAMES}
EXECUTION_NOTIONAL_COHERENCE_TOLERANCE_USD = 1e-6
FLOAT_COMPARISON_ULP_GUARD_MULTIPLIER = 2.0


@dataclass
class RealizedRowValidation:
    status: str
    reason: str
    normalized_net_pnl_usd: float | None
    entry: JournalEntry
    lineage_trust_tier: str = "native_position_lineage"


@dataclass
class LegacyMigrationReport:
    total_rows: int
    realized_rows: int
    upgraded_rows: int
    ambiguous_rows: int
    unchanged_rows: int
    low_context_bounded_rows: int = 0
    low_context_rejected_rows: int = 0
    sparse_reconstructed_rows: int = 0
    sparse_bounded_rows: int = 0


def _within_absolute_tolerance(a: float, b: float, tolerance: float) -> bool:
    scale = max(abs(a), abs(b), 1.0)
    fp_guard = math.ulp(scale) * FLOAT_COMPARISON_ULP_GUARD_MULTIPLIER
    return abs(a - b) <= (tolerance + fp_guard)

def is_realized_row(entry: JournalEntry) -> bool:
    return str(entry.actual_outcome).lower() in {"win", "loss", "breakeven"} and entry.actual_pnl_usd is not None


def _execution_fields_present(entry: JournalEntry) -> bool:
    effective_size = entry.executed_size_btc if entry.executed_size_btc is not None else entry.position_size_btc
    return (
        effective_size is not None
        and effective_size > 0
        and entry.executed_entry_price is not None
        and entry.executed_entry_price > 0
        and entry.executed_notional_usd is not None
        and entry.executed_notional_usd > 0
    )


def _execution_fields_coherent(entry: JournalEntry) -> bool:
    effective_size = entry.executed_size_btc if entry.executed_size_btc is not None else entry.position_size_btc
    expected = abs(effective_size * entry.executed_entry_price)
    if not _within_absolute_tolerance(expected, float(entry.executed_notional_usd), EXECUTION_NOTIONAL_COHERENCE_TOLERANCE_USD):
        return False

    if entry.executed_size_btc is not None:
        if entry.executed_size_btc <= 0:
            return False
        if entry.position_size_btc <= 0 or entry.executed_size_btc - entry.position_size_btc > EXECUTION_NOTIONAL_COHERENCE_TOLERANCE_USD:
            return False
    return True


def _expected_gross_pnl(entry: JournalEntry) -> float | None:
    if entry.actual_exit_price is None:
        return None

    effective_size = entry.executed_size_btc if entry.executed_size_btc is not None else entry.position_size_btc
    direction = str(entry.decision)
    if direction == "Paper Long":
        return (entry.actual_exit_price - entry.executed_entry_price) * effective_size
    if direction == "Paper Short":
        return (entry.executed_entry_price - entry.actual_exit_price) * effective_size
    return None


def validate_realized_row(entry: JournalEntry, config: StrategyConfig) -> RealizedRowValidation:
    if not is_realized_row(entry):
        return RealizedRowValidation("rejected_requires_manual_review", "not_realized", None, entry)

    if not _execution_fields_present(entry):
        return RealizedRowValidation("rejected_requires_manual_review", "missing_execution_fields", None, entry)

    if not _execution_fields_coherent(entry):
        return RealizedRowValidation("rejected_requires_manual_review", "execution_fields_incoherent", None, entry)

    expected_gross = _expected_gross_pnl(entry)
    if expected_gross is None:
        return RealizedRowValidation("rejected_requires_manual_review", "missing_or_invalid_exit_or_direction", None, entry)

    expected_net = apply_trading_friction(expected_gross, float(entry.executed_notional_usd), config)
    provided = float(entry.actual_pnl_usd)
    tolerance = REALIZED_PNL_RECONCILIATION_TOLERANCE_USD

    outcome = str(entry.actual_outcome).lower()
    if outcome == "win" and (expected_net < 0 or _within_absolute_tolerance(expected_net, 0.0, tolerance)):
        return RealizedRowValidation("rejected_requires_manual_review", "outcome_pnl_sign_mismatch", None, entry)
    if outcome == "loss" and (expected_net > 0 or _within_absolute_tolerance(expected_net, 0.0, tolerance)):
        return RealizedRowValidation("rejected_requires_manual_review", "outcome_pnl_sign_mismatch", None, entry)
    if outcome == "breakeven" and not _within_absolute_tolerance(expected_net, 0.0, tolerance):
        return RealizedRowValidation("rejected_requires_manual_review", "outcome_pnl_sign_mismatch", None, entry)

    basis = str(entry.actual_pnl_basis).lower()
    if basis == "net":
        if not _within_absolute_tolerance(provided, expected_net, tolerance):
            return RealizedRowValidation("rejected_requires_manual_review", "net_pnl_mismatch", None, entry)
        return RealizedRowValidation("accepted_net", "ok", expected_net, entry)

    if basis == "gross":
        if not _within_absolute_tolerance(provided, expected_gross, tolerance):
            return RealizedRowValidation("rejected_requires_manual_review", "gross_pnl_mismatch", None, entry)
        return RealizedRowValidation("accepted_gross_normalized", "ok", expected_net, entry)

    return RealizedRowValidation("rejected_requires_manual_review", "ambiguous_or_missing_basis", None, entry)






def _normalized_timestamp_value(value) -> str:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()

def _accepted_row_order_key(row: RealizedRowValidation) -> tuple:
    e = row.entry
    ts = _normalized_timestamp_value(e.timestamp)
    return (
        ts,
        str(e.decision),
        str(e.actual_outcome),
        str(e.actual_pnl_basis),
        float(e.position_size_btc),
        float(e.executed_size_btc if e.executed_size_btc is not None else e.position_size_btc),
        float(e.executed_entry_price),
        float(e.executed_notional_usd),
        float(e.actual_exit_price),
        float(e.actual_pnl_usd),
        str(e.entry),
        str(e.stop),
        str(e.target),
    )


def order_accepted_realized_rows(rows: list[RealizedRowValidation]) -> list[RealizedRowValidation]:
    return sorted(rows, key=_accepted_row_order_key)




def _lineage_anchor_key_any(entry: JournalEntry, allow_sparse_timestamp_fallback: bool = False) -> tuple | None:
    anchor_ts = entry.entry_fill_timestamp or entry.position_open_timestamp
    if anchor_ts is None and allow_sparse_timestamp_fallback:
        anchor_ts = entry.timestamp
    if anchor_ts is None:
        return None
    effective_size = entry.executed_size_btc if entry.executed_size_btc is not None else entry.position_size_btc
    if effective_size is None or entry.executed_entry_price is None or entry.executed_notional_usd is None:
        return None
    return (
        _normalized_timestamp_value(anchor_ts),
        str(entry.decision),
        str(entry.entry),
        float(entry.executed_entry_price),
        float(effective_size),
        float(entry.executed_notional_usd),
    )


def is_low_context_legacy_entry(entry: JournalEntry) -> bool:
    return not str(getattr(entry, "position_id", "") or "").strip() and _lineage_anchor_key_any(entry, allow_sparse_timestamp_fallback=True) is None


def _is_sparse_supportable_legacy_entry(entry: JournalEntry) -> bool:
    if str(getattr(entry, "position_id", "") or "").strip():
        return False
    return _lineage_anchor_key_any(entry, allow_sparse_timestamp_fallback=False) is None and _lineage_anchor_key_any(entry, allow_sparse_timestamp_fallback=True) is not None


def _sparse_supportable_anchor(entry: JournalEntry) -> tuple | None:
    if not _is_sparse_supportable_legacy_entry(entry):
        return None
    effective_size = entry.executed_size_btc if entry.executed_size_btc is not None else entry.position_size_btc
    return (
        str(entry.decision),
        str(entry.entry),
        float(entry.executed_entry_price),
        float(effective_size),
        float(entry.executed_notional_usd),
    )




MANUAL_REMEDIATION_ALLOWED_PARENT_TIERS = {
    "legacy_event_only",
    "low_context_legacy_event_only",
    "sparse_event_only_bounded",
    "legacy_lineage_rejected",
}


_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonicalize_remediation_evidence_payload(payload: str) -> str:
    raw = str(payload or "").strip()
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _attested_payload_sha256(payload: str) -> str:
    canonical = _canonicalize_remediation_evidence_payload(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _manual_remediation_manifest_error(entry: JournalEntry, config: StrategyConfig) -> str | None:
    policy = str(getattr(config, "remediation_evidence_manifest_policy", "require_complete_manifest") or "require_complete_manifest").lower().strip()
    manifest_raw = str(getattr(entry, "remediation_evidence_manifest_json", "") or "").strip()
    manifest_hash = str(getattr(entry, "remediation_evidence_manifest_hash_sha256", "") or "").strip().lower()

    if policy not in {"require_complete_manifest", "allow_legacy_no_manifest"}:
        return "manual_remediation_manifest_policy_invalid"
    if policy == "allow_legacy_no_manifest" and not manifest_raw and not manifest_hash:
        return None
    if not manifest_raw or not manifest_hash:
        return "manual_remediation_manifest_indeterminate"
    if not _SHA256_HEX_RE.match(manifest_hash):
        return "manual_remediation_manifest_indeterminate"

    try:
        manifest_obj = json.loads(manifest_raw)
    except Exception:
        return "manual_remediation_manifest_indeterminate"
    if not isinstance(manifest_obj, dict):
        return "manual_remediation_manifest_indeterminate"

    bundle_id = str(getattr(entry, "remediation_bundle_id", "") or "").strip()
    evidence_hash = str(getattr(entry, "remediation_evidence_hash_sha256", "") or "").strip().lower()
    m_bundle_id = str(manifest_obj.get("bundle_id", "") or "").strip()
    m_evidence_hash = str(manifest_obj.get("evidence_hash_sha256", "") or "").strip().lower()
    if not m_bundle_id or not m_evidence_hash:
        return "manual_remediation_manifest_indeterminate"
    if m_bundle_id != bundle_id or m_evidence_hash != evidence_hash:
        return "manual_remediation_manifest_invalid"

    artifacts = manifest_obj.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return "manual_remediation_manifest_indeterminate"

    normalized_artifacts: list[dict[str, Any]] = []
    seen_role_path: set[tuple[str, str]] = set()
    seen_hashes: set[str] = set()
    has_required = False
    has_primary = False
    for item in artifacts:
        if not isinstance(item, dict):
            return "manual_remediation_manifest_indeterminate"
        role = str(item.get("role", "") or "").strip().lower()
        path = str(item.get("path", "") or "").strip()
        sha = str(item.get("sha256", "") or "").strip().lower()
        required = item.get("required", None)
        if not role or not path or not _SHA256_HEX_RE.match(sha):
            return "manual_remediation_manifest_indeterminate"
        if not isinstance(required, bool):
            return "manual_remediation_manifest_indeterminate"
        key = (role, path)
        if key in seen_role_path:
            return "manual_remediation_manifest_invalid"
        seen_role_path.add(key)
        if sha in seen_hashes:
            return "manual_remediation_manifest_invalid"
        seen_hashes.add(sha)
        if required:
            has_required = True
        if role == "primary_evidence":
            if sha != evidence_hash:
                return "manual_remediation_manifest_invalid"
            has_primary = True
        normalized_artifacts.append({"role": role, "path": path, "sha256": sha, "required": required})

    if not has_required or not has_primary:
        return "manual_remediation_manifest_invalid"

    canonical_manifest = {
        "bundle_id": m_bundle_id,
        "evidence_hash_sha256": m_evidence_hash,
        "artifacts": sorted(normalized_artifacts, key=lambda a: (a["role"], a["path"], a["sha256"], int(a["required"]))),
    }
    canonical_text = json.dumps(canonical_manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    canonical_hash = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    if canonical_hash != manifest_hash:
        return "manual_remediation_manifest_invalid"
    return None


def _retrieval_artifact_state(
    required_key: tuple[str, str, str],
    retrieval_rows: list[dict[str, Any]],
) -> str:
    if not retrieval_rows:
        return "retrieval_invalid_artifact"
    if len(retrieval_rows) > 1:
        normalized_rows = {
            (
                bool(row.get("available")),
                str(row.get("content_sha256", "") or "").strip().lower(),
                str(row.get("retrieval_receipt_sha256", "") or "").strip().lower(),
            )
            for row in retrieval_rows
        }
        if len(normalized_rows) > 1:
            return "retrieval_indeterminate_artifact"
        return "retrieval_invalid_artifact"
    row = retrieval_rows[0]
    if row.get("available") is not True:
        return "retrieval_invalid_artifact"
    if str(row.get("content_sha256", "") or "").strip().lower() != required_key[2]:
        return "retrieval_invalid_artifact"
    return "retrieval_valid_artifact"


def _derive_retrieval_bundle_state(artifact_states: list[str]) -> str:
    if not artifact_states:
        return "retrieval_invalid_bundle"
    if any(state == "retrieval_indeterminate_artifact" for state in artifact_states):
        return "retrieval_indeterminate_bundle"
    if any(state == "retrieval_invalid_artifact" for state in artifact_states):
        return "retrieval_invalid_bundle"
    return "retrieval_valid_bundle"


def _expected_retrieval_receipt_signature(
    scheme: str,
    attestor_id: str,
    key_instance_id: str,
    source: str,
    bundle_id: str,
    evidence_hash: str,
    role: str,
    path: str,
    artifact_sha: str,
    content_sha: str,
    receipt_sha: str,
) -> str | None:
    normalized_scheme = str(scheme or "").strip().lower()
    if normalized_scheme != "sha256_receipt_bind_v1":
        return None
    message = "|".join(
        [
            normalized_scheme,
            str(attestor_id).strip().lower(),
            str(key_instance_id).strip().lower(),
            str(source).strip().lower(),
            str(bundle_id).strip(),
            str(evidence_hash).strip().lower(),
            str(role).strip().lower(),
            str(path).strip(),
            str(artifact_sha).strip().lower(),
            str(content_sha).strip().lower(),
            str(receipt_sha).strip().lower(),
        ]
    )
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _trusted_retrieval_receipt_attestors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_trusted_retrieval_receipt_attestor_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_retrieval_receipt_attestors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_revoked_retrieval_receipt_attestor_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_retrieval_receipt_attestor_cutoff_utc(config: StrategyConfig, attestor_id: str) -> datetime | None:
    mapping = getattr(config, "remediation_retrieval_receipt_attestor_revoked_before_utc", None) or {}
    value = mapping.get(str(attestor_id))
    if value is None:
        return None
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _active_retrieval_receipt_attestors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_retrieval_receipt_active_attestor_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_retrieval_receipt_attestor_key_instances(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_revoked_retrieval_receipt_attestor_key_instances", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_retrieval_receipt_attestor_key_instance_cutoff_utc(config: StrategyConfig, key_instance_id: str) -> datetime | None:
    mapping = getattr(config, "remediation_retrieval_receipt_attestor_key_instance_revoked_before_utc", None) or {}
    value = mapping.get(str(key_instance_id))
    if value is None:
        return None
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _active_retrieval_receipt_key_instances_for_attestor(config: StrategyConfig, attestor_id: str) -> set[str]:
    mapping = getattr(config, "remediation_trusted_retrieval_receipt_attestor_key_instances", None) or {}
    raw = mapping.get(str(attestor_id), ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _trusted_retrieval_receipt_attestor_key_lifecycle_anchors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_retrieval_receipt_attestor_key_lifecycle_anchor_keys(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_revoked_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_retrieval_receipt_attestor_key_lifecycle_anchor_key_cutoff_utc(config: StrategyConfig, key_instance_id: str) -> datetime | None:
    mapping = getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_anchor_key_instance_revoked_before_utc", None) or {}
    value = mapping.get(str(key_instance_id))
    if value is None:
        return None
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _active_retrieval_receipt_attestor_key_lifecycle_anchor_keys_for_anchor(config: StrategyConfig, anchor_id: str) -> set[str]:
    mapping = getattr(config, "remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances", None) or {}
    raw = mapping.get(str(anchor_id), ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _trusted_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_anchors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_anchor_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_retrieval_receipt_attestor_key_lifecycle_governance_anchors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_revoked_retrieval_receipt_attestor_key_lifecycle_governance_anchor_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _active_retrieval_receipt_attestor_key_lifecycle_governance_anchors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_active_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_retrieval_receipt_attestor_key_lifecycle_governance_anchor_cutoff_utc(config: StrategyConfig, governance_anchor_id: str) -> datetime | None:
    mapping = getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_revoked_before_utc", None) or {}
    value = mapping.get(str(governance_anchor_id))
    if value is None:
        return None
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_anchors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_anchor_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _active_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_active_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_revoked_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_cutoff_utc(config: StrategyConfig, provenance_anchor_id: str) -> datetime | None:
    mapping = getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_revoked_before_utc", None) or {}
    value = mapping.get(str(provenance_anchor_id))
    if value is None:
        return None
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_anchors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_anchor_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _trusted_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_trusted_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _active_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_active_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_revoked_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_cutoff_utc(config: StrategyConfig, root_anchor_id: str) -> datetime | None:
    mapping = getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_revoked_before_utc", None) or {}
    value = mapping.get(str(root_anchor_id))
    if value is None:
        return None
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _trusted_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_provenance_anchors(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_trusted_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_provenance_anchor_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _expected_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_provenance_signature(
    scheme: str,
    super_root_anchor_id: str,
    source: str,
    dataset_hash: str,
) -> str | None:
    normalized_scheme = str(scheme or "").strip().lower()
    if normalized_scheme != "sha256_root_provenance_anchor_bind_v1":
        return None
    message = "|".join([
        normalized_scheme,
        str(super_root_anchor_id).strip().lower(),
        str(source).strip().lower(),
        str(dataset_hash).strip().lower(),
    ])
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _parse_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_provenance(
    entry: JournalEntry,
    bundle_id: str,
    evidence_hash: str,
    config: StrategyConfig,
) -> tuple[str, dict[str, dict[str, Any]]]:
    policy = str(getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_governance_policy", "allow_local_policy_only") or "allow_local_policy_only").lower().strip()
    if policy not in {"require_anchored_root_provenance_anchor_governance", "allow_local_policy_only"}:
        return "root_provenance_anchor_indeterminate", {}
    if policy == "allow_local_policy_only":
        return "root_provenance_anchor_valid", {}

    raw = str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_provenance_json", "") or "").strip()
    encoded_hash = str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_provenance_hash_sha256", "") or "").strip().lower()
    if not raw and not encoded_hash:
        return "root_provenance_anchor_indeterminate", {}
    if not raw or not encoded_hash or not _SHA256_HEX_RE.match(encoded_hash):
        return "root_provenance_anchor_indeterminate", {}
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != encoded_hash:
        return "root_provenance_anchor_invalid", {}
    try:
        evidence_obj = json.loads(raw)
    except Exception:
        return "root_provenance_anchor_indeterminate", {}
    if not isinstance(evidence_obj, dict):
        return "root_provenance_anchor_indeterminate", {}
    e_bundle_id = str(evidence_obj.get("bundle_id", "") or "").strip()
    e_evidence_hash = str(evidence_obj.get("evidence_hash_sha256", "") or "").strip().lower()
    if e_bundle_id != bundle_id or e_evidence_hash != evidence_hash:
        return "root_provenance_anchor_invalid", {}
    super_root_anchor_id = str(evidence_obj.get("super_root_anchor_id", "") or "").strip()
    source = str(evidence_obj.get("source", "") or "").strip()
    signature = str(evidence_obj.get("signature", "") or "").strip().lower()
    scheme = str(evidence_obj.get("signature_scheme", "") or "").strip().lower()
    if not super_root_anchor_id or not source or not signature or not scheme:
        return "root_provenance_anchor_indeterminate", {}
    if super_root_anchor_id not in _trusted_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_provenance_anchors(config):
        return "root_provenance_anchor_invalid", {}
    if not (source.startswith("https://") or source.startswith("ext://")):
        return "root_provenance_anchor_invalid", {}
    if not _SHA256_HEX_RE.match(signature):
        return "root_provenance_anchor_indeterminate", {}
    signed_payload = {k: v for k, v in evidence_obj.items() if k not in {"signature", "signature_scheme"}}
    signed_hash = hashlib.sha256(json.dumps(signed_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    expected = _expected_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_provenance_signature(scheme, super_root_anchor_id, source, signed_hash)
    if expected is None:
        return "root_provenance_anchor_indeterminate", {}
    if expected != signature:
        return "root_provenance_anchor_invalid", {}
    records = evidence_obj.get("records")
    if not isinstance(records, list) or not records:
        return "root_provenance_anchor_indeterminate", {}
    by_anchor: dict[str, dict[str, Any]] = {}
    for row in records:
        if not isinstance(row, dict):
            return "root_provenance_anchor_indeterminate", {}
        root_anchor_id = str(row.get("root_anchor_id", "") or "").strip()
        lifecycle_state = str(row.get("lifecycle_state", "") or "").strip().lower()
        revoked_before_utc = str(row.get("revoked_before_utc", "") or "").strip()
        if not root_anchor_id or lifecycle_state not in {"active", "retired", "revoked"}:
            return "root_provenance_anchor_indeterminate", {}
        normalized = {
            "root_anchor_id": root_anchor_id,
            "lifecycle_state": lifecycle_state,
            "revoked_before_utc": revoked_before_utc,
        }
        prior = by_anchor.get(root_anchor_id)
        if prior is not None and prior != normalized:
            return "root_provenance_anchor_invalid", {}
        by_anchor[root_anchor_id] = normalized
    return "root_provenance_anchor_valid", by_anchor


def _retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_state(
    root_anchor_id: str,
    remediation_ts: datetime | None,
    config: StrategyConfig,
    root_anchor_state: str,
    root_anchor_records: dict[str, dict[str, Any]] | None,
) -> str:
    if root_anchor_state == "root_provenance_anchor_invalid":
        return "root_provenance_anchor_invalid"
    if root_anchor_state == "root_provenance_anchor_indeterminate":
        return "root_provenance_anchor_indeterminate"

    trusted = _trusted_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchors(config)
    if root_anchor_id not in trusted:
        return "root_provenance_anchor_invalid"

    if root_anchor_records:
        rec = root_anchor_records.get(root_anchor_id)
        if rec is None:
            return "root_provenance_anchor_indeterminate"
        lifecycle_state = str(rec.get("lifecycle_state", "") or "").strip().lower()
        if lifecycle_state not in {"active", "retired", "revoked"}:
            return "root_provenance_anchor_indeterminate"
        if lifecycle_state in {"retired", "revoked"}:
            cutoff_raw = str(rec.get("revoked_before_utc", "") or "").strip()
            if cutoff_raw:
                try:
                    cutoff_dt = datetime.fromisoformat(cutoff_raw)
                except Exception:
                    return "root_provenance_anchor_indeterminate"
                if cutoff_dt.tzinfo is None:
                    cutoff_dt = cutoff_dt.replace(tzinfo=timezone.utc)
                else:
                    cutoff_dt = cutoff_dt.astimezone(timezone.utc)
                if remediation_ts is None:
                    return "root_provenance_anchor_indeterminate"
                if remediation_ts >= cutoff_dt:
                    return "root_provenance_anchor_revoked"
            else:
                return "root_provenance_anchor_revoked"

    revoked = _revoked_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchors(config)
    lifecycle_policy = str(getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_lifecycle_policy", "reject_all_revoked") or "reject_all_revoked").lower().strip()
    if lifecycle_policy not in {"reject_all_revoked", "allow_pre_revocation_only"}:
        return "root_provenance_anchor_indeterminate"
    if root_anchor_id in revoked:
        if lifecycle_policy == "reject_all_revoked":
            return "root_provenance_anchor_revoked"
        cutoff = _revoked_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_cutoff_utc(config, root_anchor_id)
        if cutoff is None or remediation_ts is None:
            return "root_provenance_anchor_indeterminate"
        if remediation_ts >= cutoff:
            return "root_provenance_anchor_revoked"

    rotation_policy = str(getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchor_rotation_policy", "allow_historical_non_active") or "allow_historical_non_active").lower().strip()
    active = _active_retrieval_receipt_attestor_key_lifecycle_root_provenance_anchors(config)
    if rotation_policy == "require_active_anchor_lineage":
        if not active:
            return "root_provenance_anchor_indeterminate"
        if root_anchor_id not in active:
            return "root_provenance_anchor_revoked"
    elif rotation_policy != "allow_historical_non_active":
        return "root_provenance_anchor_indeterminate"

    return "root_provenance_anchor_valid"


def _expected_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_signature(
    scheme: str,
    root_anchor_id: str,
    source: str,
    dataset_hash: str,
) -> str | None:
    normalized_scheme = str(scheme or "").strip().lower()
    if normalized_scheme != "sha256_identity_provenance_anchor_bind_v1":
        return None
    message = "|".join([
        normalized_scheme,
        str(root_anchor_id).strip().lower(),
        str(source).strip().lower(),
        str(dataset_hash).strip().lower(),
    ])
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _parse_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance(
    entry: JournalEntry,
    bundle_id: str,
    evidence_hash: str,
    config: StrategyConfig,
) -> tuple[str, dict[str, dict[str, Any]]]:
    policy = str(getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_governance_policy", "allow_local_policy_only") or "allow_local_policy_only").lower().strip()
    if policy not in {"require_anchored_identity_provenance_anchor_governance", "allow_local_policy_only"}:
        return "identity_provenance_anchor_indeterminate", {}
    if policy == "allow_local_policy_only":
        return "identity_provenance_anchor_valid", {}

    raw = str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_json", "") or "").strip()
    encoded_hash = str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_hash_sha256", "") or "").strip().lower()
    if not raw and not encoded_hash:
        return "identity_provenance_anchor_indeterminate", {}
    if not raw or not encoded_hash or not _SHA256_HEX_RE.match(encoded_hash):
        return "identity_provenance_anchor_indeterminate", {}
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != encoded_hash:
        return "identity_provenance_anchor_invalid", {}
    try:
        evidence_obj = json.loads(raw)
    except Exception:
        return "identity_provenance_anchor_indeterminate", {}
    if not isinstance(evidence_obj, dict):
        return "identity_provenance_anchor_indeterminate", {}
    e_bundle_id = str(evidence_obj.get("bundle_id", "") or "").strip()
    e_evidence_hash = str(evidence_obj.get("evidence_hash_sha256", "") or "").strip().lower()
    if e_bundle_id != bundle_id or e_evidence_hash != evidence_hash:
        return "identity_provenance_anchor_invalid", {}
    root_anchor_id = str(evidence_obj.get("root_anchor_id", "") or "").strip()
    source = str(evidence_obj.get("source", "") or "").strip()
    signature = str(evidence_obj.get("signature", "") or "").strip().lower()
    scheme = str(evidence_obj.get("signature_scheme", "") or "").strip().lower()
    if not root_anchor_id or not source or not signature or not scheme:
        return "identity_provenance_anchor_indeterminate", {}
    if root_anchor_id not in _trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_anchors(config):
        return "identity_provenance_anchor_invalid", {}
    if not (source.startswith("https://") or source.startswith("ext://")):
        return "identity_provenance_anchor_invalid", {}
    if not _SHA256_HEX_RE.match(signature):
        return "identity_provenance_anchor_indeterminate", {}
    signed_payload = {k: v for k, v in evidence_obj.items() if k not in {"signature", "signature_scheme"}}
    signed_hash = hashlib.sha256(json.dumps(signed_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    expected = _expected_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_signature(scheme, root_anchor_id, source, signed_hash)
    if expected is None:
        return "identity_provenance_anchor_indeterminate", {}
    if expected != signature:
        return "identity_provenance_anchor_invalid", {}
    records = evidence_obj.get("records")
    if not isinstance(records, list) or not records:
        return "identity_provenance_anchor_indeterminate", {}
    by_anchor: dict[str, dict[str, Any]] = {}
    for row in records:
        if not isinstance(row, dict):
            return "identity_provenance_anchor_indeterminate", {}
        provenance_anchor_id = str(row.get("provenance_anchor_id", "") or "").strip()
        lifecycle_state = str(row.get("lifecycle_state", "") or "").strip().lower()
        revoked_before_utc = str(row.get("revoked_before_utc", "") or "").strip()
        if not provenance_anchor_id or lifecycle_state not in {"active", "retired", "revoked"}:
            return "identity_provenance_anchor_indeterminate", {}
        normalized = {
            "provenance_anchor_id": provenance_anchor_id,
            "lifecycle_state": lifecycle_state,
            "revoked_before_utc": revoked_before_utc,
        }
        prior = by_anchor.get(provenance_anchor_id)
        if prior is not None and prior != normalized:
            return "identity_provenance_anchor_invalid", {}
        by_anchor[provenance_anchor_id] = normalized
    return "identity_provenance_anchor_valid", by_anchor


def _retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_state(
    provenance_anchor_id: str,
    remediation_ts: datetime | None,
    config: StrategyConfig,
    identity_provenance_anchor_state: str,
    identity_provenance_anchor_records: dict[str, dict[str, Any]] | None,
) -> str:
    if identity_provenance_anchor_state == "identity_provenance_anchor_invalid":
        return "identity_provenance_anchor_invalid"
    if identity_provenance_anchor_state == "identity_provenance_anchor_indeterminate":
        return "identity_provenance_anchor_indeterminate"

    trusted = _trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchors(config)
    if provenance_anchor_id not in trusted:
        return "identity_provenance_anchor_invalid"

    if identity_provenance_anchor_records:
        rec = identity_provenance_anchor_records.get(provenance_anchor_id)
        if rec is None:
            return "identity_provenance_anchor_indeterminate"
        lifecycle_state = str(rec.get("lifecycle_state", "") or "").strip().lower()
        if lifecycle_state not in {"active", "retired", "revoked"}:
            return "identity_provenance_anchor_indeterminate"
        if lifecycle_state in {"retired", "revoked"}:
            cutoff_raw = str(rec.get("revoked_before_utc", "") or "").strip()
            if cutoff_raw:
                try:
                    cutoff_dt = datetime.fromisoformat(cutoff_raw)
                except Exception:
                    return "identity_provenance_anchor_indeterminate"
                if cutoff_dt.tzinfo is None:
                    cutoff_dt = cutoff_dt.replace(tzinfo=timezone.utc)
                else:
                    cutoff_dt = cutoff_dt.astimezone(timezone.utc)
                if remediation_ts is None:
                    return "identity_provenance_anchor_indeterminate"
                if remediation_ts >= cutoff_dt:
                    return "identity_provenance_anchor_revoked"
            else:
                return "identity_provenance_anchor_revoked"

    revoked = _revoked_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchors(config)
    lifecycle_policy = str(getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_lifecycle_policy", "reject_all_revoked") or "reject_all_revoked").lower().strip()
    if lifecycle_policy not in {"reject_all_revoked", "allow_pre_revocation_only"}:
        return "identity_provenance_anchor_indeterminate"
    if provenance_anchor_id in revoked:
        if lifecycle_policy == "reject_all_revoked":
            return "identity_provenance_anchor_revoked"
        cutoff = _revoked_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_cutoff_utc(config, provenance_anchor_id)
        if cutoff is None or remediation_ts is None:
            return "identity_provenance_anchor_indeterminate"
        if remediation_ts >= cutoff:
            return "identity_provenance_anchor_revoked"

    rotation_policy = str(getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_rotation_policy", "allow_historical_non_active") or "allow_historical_non_active").lower().strip()
    active = _active_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchors(config)
    if rotation_policy == "require_active_anchor_lineage":
        if not active:
            return "identity_provenance_anchor_indeterminate"
        if provenance_anchor_id not in active:
            return "identity_provenance_anchor_revoked"
    elif rotation_policy != "allow_historical_non_active":
        return "identity_provenance_anchor_indeterminate"

    return "identity_provenance_anchor_valid"


def _expected_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_signature(
    scheme: str,
    provenance_anchor_id: str,
    source: str,
    dataset_hash: str,
) -> str | None:
    normalized_scheme = str(scheme or "").strip().lower()
    if normalized_scheme != "sha256_governance_anchor_identity_bind_v1":
        return None
    message = "|".join([
        normalized_scheme,
        str(provenance_anchor_id).strip().lower(),
        str(source).strip().lower(),
        str(dataset_hash).strip().lower(),
    ])
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _parse_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance(
    entry: JournalEntry,
    bundle_id: str,
    evidence_hash: str,
    remediation_ts: datetime | None,
    identity_provenance_anchor_state: str,
    identity_provenance_anchor_records: dict[str, dict[str, Any]] | None,
    config: StrategyConfig,
) -> tuple[str, dict[str, dict[str, Any]]]:
    policy = str(getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_policy", "allow_local_policy_only") or "allow_local_policy_only").lower().strip()
    if policy not in {"require_anchored_identity_provenance", "allow_local_policy_only"}:
        return "governance_anchor_indeterminate", {}
    if policy == "allow_local_policy_only":
        return "governance_anchor_valid", {}

    raw = str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_json", "") or "").strip()
    encoded_hash = str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_hash_sha256", "") or "").strip().lower()
    if not raw and not encoded_hash:
        return "governance_anchor_indeterminate", {}
    if not raw or not encoded_hash or not _SHA256_HEX_RE.match(encoded_hash):
        return "governance_anchor_indeterminate", {}
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != encoded_hash:
        return "governance_anchor_invalid", {}
    try:
        evidence_obj = json.loads(raw)
    except Exception:
        return "governance_anchor_indeterminate", {}
    if not isinstance(evidence_obj, dict):
        return "governance_anchor_indeterminate", {}
    e_bundle_id = str(evidence_obj.get("bundle_id", "") or "").strip()
    e_evidence_hash = str(evidence_obj.get("evidence_hash_sha256", "") or "").strip().lower()
    if e_bundle_id != bundle_id or e_evidence_hash != evidence_hash:
        return "governance_anchor_invalid", {}

    provenance_anchor_id = str(evidence_obj.get("provenance_anchor_id", "") or "").strip()
    source = str(evidence_obj.get("source", "") or "").strip()
    signature = str(evidence_obj.get("signature", "") or "").strip().lower()
    scheme = str(evidence_obj.get("signature_scheme", "") or "").strip().lower()
    if not provenance_anchor_id or not source or not signature or not scheme:
        return "governance_anchor_indeterminate", {}
    provenance_anchor_state = _retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_state(
        provenance_anchor_id,
        remediation_ts,
        config,
        identity_provenance_anchor_state,
        identity_provenance_anchor_records,
    )
    if provenance_anchor_state == "identity_provenance_anchor_revoked":
        return "governance_anchor_source_revoked", {}
    if provenance_anchor_state == "identity_provenance_anchor_indeterminate":
        return "governance_anchor_source_indeterminate", {}
    if provenance_anchor_state == "identity_provenance_anchor_invalid":
        return "governance_anchor_invalid", {}
    if provenance_anchor_id not in _trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_anchors(config):
        return "governance_anchor_invalid", {}
    if not (source.startswith("https://") or source.startswith("ext://")):
        return "governance_anchor_invalid", {}
    if not _SHA256_HEX_RE.match(signature):
        return "governance_anchor_indeterminate", {}
    signed_payload = {k: v for k, v in evidence_obj.items() if k not in {"signature", "signature_scheme"}}
    signed_hash = hashlib.sha256(json.dumps(signed_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    expected = _expected_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_signature(scheme, provenance_anchor_id, source, signed_hash)
    if expected is None:
        return "governance_anchor_indeterminate", {}
    if expected != signature:
        return "governance_anchor_invalid", {}

    records = evidence_obj.get("records")
    if not isinstance(records, list) or not records:
        return "governance_anchor_indeterminate", {}
    by_anchor: dict[str, dict[str, Any]] = {}
    for row in records:
        if not isinstance(row, dict):
            return "governance_anchor_indeterminate", {}
        governance_anchor_id = str(row.get("governance_anchor_id", "") or "").strip()
        lifecycle_state = str(row.get("lifecycle_state", "") or "").strip().lower()
        revoked_before_utc = str(row.get("revoked_before_utc", "") or "").strip()
        if not governance_anchor_id or lifecycle_state not in {"active", "retired", "revoked"}:
            return "governance_anchor_indeterminate", {}
        normalized = {
            "governance_anchor_id": governance_anchor_id,
            "lifecycle_state": lifecycle_state,
            "revoked_before_utc": revoked_before_utc,
        }
        prior = by_anchor.get(governance_anchor_id)
        if prior is not None and prior != normalized:
            return "governance_anchor_invalid", {}
        by_anchor[governance_anchor_id] = normalized
    return "governance_anchor_valid", by_anchor


def _retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_state(
    governance_anchor_id: str,
    remediation_ts: datetime | None,
    config: StrategyConfig,
    identity_provenance_state: str,
    identity_provenance_records: dict[str, dict[str, Any]] | None,
) -> str:
    if identity_provenance_state == "governance_anchor_source_revoked":
        return "governance_anchor_revoked"
    if identity_provenance_state == "governance_anchor_source_indeterminate":
        return "governance_anchor_indeterminate"
    if identity_provenance_state == "governance_anchor_invalid":
        return "governance_anchor_invalid"
    if identity_provenance_state == "governance_anchor_indeterminate":
        return "governance_anchor_indeterminate"

    trusted = _trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchors(config)
    if governance_anchor_id not in trusted:
        return "governance_anchor_invalid"

    if identity_provenance_records:
        rec = identity_provenance_records.get(governance_anchor_id)
        if rec is None:
            return "governance_anchor_indeterminate"
        lifecycle_state = str(rec.get("lifecycle_state", "") or "").strip().lower()
        if lifecycle_state not in {"active", "retired", "revoked"}:
            return "governance_anchor_indeterminate"
        if lifecycle_state in {"retired", "revoked"}:
            cutoff_raw = str(rec.get("revoked_before_utc", "") or "").strip()
            if cutoff_raw:
                try:
                    cutoff_dt = datetime.fromisoformat(cutoff_raw)
                except Exception:
                    return "governance_anchor_indeterminate"
                if cutoff_dt.tzinfo is None:
                    cutoff_dt = cutoff_dt.replace(tzinfo=timezone.utc)
                else:
                    cutoff_dt = cutoff_dt.astimezone(timezone.utc)
                if remediation_ts is None:
                    return "governance_anchor_indeterminate"
                if remediation_ts >= cutoff_dt:
                    return "governance_anchor_revoked"
            else:
                return "governance_anchor_revoked"

    revoked = _revoked_retrieval_receipt_attestor_key_lifecycle_governance_anchors(config)
    lifecycle_policy = str(getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_lifecycle_policy", "reject_all_revoked") or "reject_all_revoked").lower().strip()
    if lifecycle_policy not in {"reject_all_revoked", "allow_pre_revocation_only"}:
        return "governance_anchor_indeterminate"
    if governance_anchor_id in revoked:
        if lifecycle_policy == "reject_all_revoked":
            return "governance_anchor_revoked"
        cutoff = _revoked_retrieval_receipt_attestor_key_lifecycle_governance_anchor_cutoff_utc(config, governance_anchor_id)
        if cutoff is None or remediation_ts is None:
            return "governance_anchor_indeterminate"
        if remediation_ts >= cutoff:
            return "governance_anchor_revoked"

    rotation_policy = str(getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_rotation_policy", "allow_historical_non_active") or "allow_historical_non_active").lower().strip()
    active = _active_retrieval_receipt_attestor_key_lifecycle_governance_anchors(config)
    if rotation_policy == "require_active_anchor_lineage":
        if not active:
            return "governance_anchor_indeterminate"
        if governance_anchor_id not in active:
            return "governance_anchor_revoked"
    elif rotation_policy != "allow_historical_non_active":
        return "governance_anchor_indeterminate"

    return "governance_anchor_valid"


def _expected_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_signature(
    scheme: str,
    governance_anchor_id: str,
    source: str,
    dataset_hash: str,
) -> str | None:
    normalized_scheme = str(scheme or "").strip().lower()
    if normalized_scheme != "sha256_anchor_governance_dataset_bind_v1":
        return None
    message = "|".join([
        normalized_scheme,
        str(governance_anchor_id).strip().lower(),
        str(source).strip().lower(),
        str(dataset_hash).strip().lower(),
    ])
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _parse_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset(
    entry: JournalEntry,
    bundle_id: str,
    evidence_hash: str,
    remediation_ts: datetime | None,
    identity_provenance_state: str,
    identity_provenance_records: dict[str, dict[str, Any]] | None,
    config: StrategyConfig,
) -> tuple[str, dict[tuple[str, str], dict[str, Any]]]:
    policy = str(getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_policy", "allow_local_policy_only") or "allow_local_policy_only").lower().strip()
    if policy not in {"require_anchored_dataset", "allow_local_policy_only"}:
        return "governance_dataset_indeterminate", {}
    if policy == "allow_local_policy_only":
        return "governance_dataset_anchored", {}

    raw = str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_json", "") or "").strip()
    encoded_hash = str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_hash_sha256", "") or "").strip().lower()
    if not raw and not encoded_hash:
        return "governance_dataset_indeterminate", {}
    if not raw or not encoded_hash or not _SHA256_HEX_RE.match(encoded_hash):
        return "governance_dataset_indeterminate", {}
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != encoded_hash:
        return "governance_dataset_invalid", {}
    try:
        evidence_obj = json.loads(raw)
    except Exception:
        return "governance_dataset_indeterminate", {}
    if not isinstance(evidence_obj, dict):
        return "governance_dataset_indeterminate", {}
    e_bundle_id = str(evidence_obj.get("bundle_id", "") or "").strip()
    e_evidence_hash = str(evidence_obj.get("evidence_hash_sha256", "") or "").strip().lower()
    if e_bundle_id != bundle_id or e_evidence_hash != evidence_hash:
        return "governance_dataset_invalid", {}

    governance_anchor_id = str(evidence_obj.get("governance_anchor_id", "") or "").strip()
    source = str(evidence_obj.get("source", "") or "").strip()
    signature = str(evidence_obj.get("signature", "") or "").strip().lower()
    scheme = str(evidence_obj.get("signature_scheme", "") or "").strip().lower()
    if not governance_anchor_id or not source or not signature or not scheme:
        return "governance_dataset_indeterminate", {}
    governance_anchor_state = _retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_state(
        governance_anchor_id,
        remediation_ts,
        config,
        identity_provenance_state,
        identity_provenance_records,
    )
    if governance_anchor_state == "governance_anchor_revoked":
        return "governance_dataset_anchor_revoked", {}
    if governance_anchor_state == "governance_anchor_indeterminate":
        return "governance_dataset_anchor_indeterminate", {}
    if governance_anchor_state == "governance_anchor_invalid":
        return "governance_dataset_invalid", {}
    if governance_anchor_id not in _trusted_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_anchors(config):
        return "governance_dataset_invalid", {}
    if not (source.startswith("https://") or source.startswith("ext://")):
        return "governance_dataset_invalid", {}
    if not _SHA256_HEX_RE.match(signature):
        return "governance_dataset_indeterminate", {}

    signed_payload = {k: v for k, v in evidence_obj.items() if k not in {"signature", "signature_scheme"}}
    signed_hash = hashlib.sha256(json.dumps(signed_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    expected = _expected_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_signature(scheme, governance_anchor_id, source, signed_hash)
    if expected is None:
        return "governance_dataset_indeterminate", {}
    if expected != signature:
        return "governance_dataset_invalid", {}

    records = evidence_obj.get("records")
    if not isinstance(records, list) or not records:
        return "governance_dataset_indeterminate", {}
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in records:
        if not isinstance(row, dict):
            return "governance_dataset_indeterminate", {}
        anchor_id = str(row.get("anchor_id", "") or "").strip()
        anchor_key_instance_id = str(row.get("anchor_key_instance_id", "") or "").strip()
        lifecycle_state = str(row.get("lifecycle_state", "") or "").strip().lower()
        revoked_before_utc = str(row.get("revoked_before_utc", "") or "").strip()
        if not anchor_id or not anchor_key_instance_id or lifecycle_state not in {"active", "retired", "revoked"}:
            return "governance_dataset_indeterminate", {}
        rec = {
            "anchor_id": anchor_id,
            "anchor_key_instance_id": anchor_key_instance_id,
            "lifecycle_state": lifecycle_state,
            "revoked_before_utc": revoked_before_utc,
        }
        key = (anchor_id, anchor_key_instance_id)
        prior = by_key.get(key)
        if prior is not None and prior != rec:
            return "governance_dataset_invalid", {}
        by_key[key] = rec
    return "governance_dataset_anchored", by_key


def _retrieval_receipt_attestor_key_lifecycle_anchor_key_state(
    anchor_id: str,
    anchor_key_instance_id: str,
    remediation_ts: datetime | None,
    config: StrategyConfig,
    governance_dataset_state: str,
    governance_dataset_records: dict[tuple[str, str], dict[str, Any]] | None,
) -> str:
    governance_policy = str(getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_anchor_key_governance_policy", "allow_ungoverned_anchor_keys") or "allow_ungoverned_anchor_keys").lower().strip()
    if governance_policy not in {"require_governed_anchor_keys", "allow_ungoverned_anchor_keys"}:
        return "anchor_key_indeterminate"
    if governance_policy == "allow_ungoverned_anchor_keys" and not anchor_key_instance_id:
        return "anchor_key_valid"
    if not anchor_key_instance_id:
        return "anchor_key_indeterminate"

    if governance_dataset_state == "governance_dataset_invalid":
        return "anchor_key_governance_dataset_invalid"
    if governance_dataset_state == "governance_dataset_anchor_revoked":
        return "anchor_key_governance_anchor_revoked"
    if governance_dataset_state == "governance_dataset_anchor_indeterminate":
        return "anchor_key_governance_anchor_indeterminate"
    if governance_dataset_state == "governance_dataset_indeterminate":
        return "anchor_key_governance_dataset_indeterminate"
    if governance_dataset_state == "governance_dataset_anchored" and governance_dataset_records:
        record = governance_dataset_records.get((anchor_id, anchor_key_instance_id))
        if record is None:
            return "anchor_key_governance_dataset_indeterminate"
        lifecycle_state = str(record.get("lifecycle_state", "") or "").strip().lower()
        if lifecycle_state not in {"active", "retired", "revoked"}:
            return "anchor_key_governance_dataset_indeterminate"
        if lifecycle_state in {"retired", "revoked"}:
            cutoff_raw = str(record.get("revoked_before_utc", "") or "").strip()
            if cutoff_raw:
                try:
                    cutoff_dt = datetime.fromisoformat(cutoff_raw)
                except Exception:
                    return "anchor_key_governance_dataset_indeterminate"
                if cutoff_dt.tzinfo is None:
                    cutoff_dt = cutoff_dt.replace(tzinfo=timezone.utc)
                else:
                    cutoff_dt = cutoff_dt.astimezone(timezone.utc)
                if remediation_ts is None:
                    return "anchor_key_governance_dataset_indeterminate"
                if remediation_ts >= cutoff_dt:
                    return "anchor_key_revoked"
            else:
                return "anchor_key_revoked"

    revoked = _revoked_retrieval_receipt_attestor_key_lifecycle_anchor_keys(config)
    lifecycle_policy = str(getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_anchor_key_lifecycle_policy", "reject_all_revoked") or "reject_all_revoked").lower().strip()
    if lifecycle_policy not in {"reject_all_revoked", "allow_pre_revocation_only"}:
        return "anchor_key_indeterminate"
    if anchor_key_instance_id in revoked:
        if lifecycle_policy == "reject_all_revoked":
            return "anchor_key_revoked"
        cutoff = _revoked_retrieval_receipt_attestor_key_lifecycle_anchor_key_cutoff_utc(config, anchor_key_instance_id)
        if cutoff is None or remediation_ts is None:
            return "anchor_key_indeterminate"
        if remediation_ts >= cutoff:
            return "anchor_key_revoked"

    rotation_policy = str(getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_anchor_key_rotation_policy", "allow_historical_non_active") or "allow_historical_non_active").lower().strip()
    active = _active_retrieval_receipt_attestor_key_lifecycle_anchor_keys_for_anchor(config, anchor_id)
    if rotation_policy == "require_active_key_lineage":
        if not active:
            return "anchor_key_indeterminate"
        if anchor_key_instance_id not in active:
            return "anchor_key_revoked"
    elif rotation_policy != "allow_historical_non_active":
        return "anchor_key_indeterminate"

    return "anchor_key_valid"


def _expected_retrieval_receipt_attestor_key_lifecycle_signature(
    scheme: str,
    anchor_id: str,
    anchor_key_instance_id: str,
    source: str,
    evidence_hash: str,
) -> str | None:
    normalized_scheme = str(scheme or "").strip().lower()
    if normalized_scheme != "sha256_key_lifecycle_bind_v1":
        return None
    message = "|".join(
        [
            normalized_scheme,
            str(anchor_id).strip().lower(),
            str(anchor_key_instance_id).strip().lower(),
            str(source).strip().lower(),
            str(evidence_hash).strip().lower(),
        ]
    )
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _parse_retrieval_receipt_attestor_key_lifecycle_evidence(
    entry: JournalEntry,
    bundle_id: str,
    evidence_hash: str,
    remediation_ts: datetime | None,
    governance_dataset_state: str,
    governance_dataset_records: dict[tuple[str, str], dict[str, Any]] | None,
    config: StrategyConfig,
) -> tuple[str, dict[tuple[str, str], dict[str, Any]]]:
    raw = str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_json", "") or "").strip()
    encoded_hash = str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_hash_sha256", "") or "").strip().lower()
    if not raw and not encoded_hash:
        return "lifecycle_evidence_indeterminate", {}
    if not raw or not encoded_hash or not _SHA256_HEX_RE.match(encoded_hash):
        return "lifecycle_evidence_indeterminate", {}
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != encoded_hash:
        return "lifecycle_evidence_invalid", {}
    try:
        evidence_obj = json.loads(raw)
    except Exception:
        return "lifecycle_evidence_indeterminate", {}
    if not isinstance(evidence_obj, dict):
        return "lifecycle_evidence_indeterminate", {}

    e_bundle_id = str(evidence_obj.get("bundle_id", "") or "").strip()
    e_evidence_hash = str(evidence_obj.get("evidence_hash_sha256", "") or "").strip().lower()
    if e_bundle_id != bundle_id or e_evidence_hash != evidence_hash:
        return "lifecycle_evidence_invalid", {}

    anchor_id = str(evidence_obj.get("anchor_id", "") or "").strip()
    anchor_key_instance_id = str(evidence_obj.get("anchor_key_instance_id", "") or "").strip()
    source = str(evidence_obj.get("source", "") or "").strip()
    signature = str(evidence_obj.get("signature", "") or "").strip().lower()
    scheme = str(evidence_obj.get("signature_scheme", "") or "").strip().lower()
    if not anchor_id or not source or not signature or not scheme:
        return "lifecycle_evidence_indeterminate", {}
    if anchor_id not in _trusted_retrieval_receipt_attestor_key_lifecycle_anchors(config):
        return "lifecycle_evidence_invalid", {}
    if not (source.startswith("https://") or source.startswith("ext://")):
        return "lifecycle_evidence_invalid", {}
    if not _SHA256_HEX_RE.match(signature):
        return "lifecycle_evidence_indeterminate", {}
    anchor_key_state = _retrieval_receipt_attestor_key_lifecycle_anchor_key_state(
        anchor_id,
        anchor_key_instance_id,
        remediation_ts,
        config,
        governance_dataset_state,
        governance_dataset_records,
    )
    if anchor_key_state == "anchor_key_revoked":
        return "lifecycle_evidence_anchor_key_revoked", {}
    if anchor_key_state == "anchor_key_indeterminate":
        return "lifecycle_evidence_anchor_key_indeterminate", {}
    if anchor_key_state == "anchor_key_governance_dataset_invalid":
        return "lifecycle_evidence_anchor_key_governance_dataset_invalid", {}
    if anchor_key_state == "anchor_key_governance_dataset_indeterminate":
        return "lifecycle_evidence_anchor_key_governance_dataset_indeterminate", {}
    if anchor_key_state == "anchor_key_governance_anchor_revoked":
        return "lifecycle_evidence_anchor_key_governance_anchor_revoked", {}
    if anchor_key_state == "anchor_key_governance_anchor_indeterminate":
        return "lifecycle_evidence_anchor_key_governance_anchor_indeterminate", {}
    signed_payload = {
        k: v
        for k, v in evidence_obj.items()
        if k not in {"signature", "signature_scheme"}
    }
    signed_hash = hashlib.sha256(json.dumps(signed_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    expected = _expected_retrieval_receipt_attestor_key_lifecycle_signature(scheme, anchor_id, anchor_key_instance_id, source, signed_hash)
    if expected is None:
        return "lifecycle_evidence_indeterminate", {}
    if expected != signature:
        return "lifecycle_evidence_invalid", {}

    records = evidence_obj.get("records")
    if not isinstance(records, list) or not records:
        return "lifecycle_evidence_indeterminate", {}
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in records:
        if not isinstance(row, dict):
            return "lifecycle_evidence_indeterminate", {}
        attestor_id = str(row.get("attestor_id", "") or "").strip()
        key_instance_id = str(row.get("key_instance_id", "") or "").strip()
        lifecycle_state = str(row.get("lifecycle_state", "") or "").strip().lower()
        revoked_before_utc = str(row.get("revoked_before_utc", "") or "").strip()
        if not attestor_id or not key_instance_id or lifecycle_state not in {"active", "revoked"}:
            return "lifecycle_evidence_indeterminate", {}
        evidence_key = (attestor_id, key_instance_id)
        normalized = {
            "attestor_id": attestor_id,
            "key_instance_id": key_instance_id,
            "lifecycle_state": lifecycle_state,
            "revoked_before_utc": revoked_before_utc,
        }
        prior = by_key.get(evidence_key)
        if prior is not None and prior != normalized:
            return "lifecycle_evidence_invalid", {}
        by_key[evidence_key] = normalized
    return "lifecycle_evidence_anchored", by_key


def _retrieval_receipt_attestor_key_instance_state(
    attestor_id: str,
    key_instance_id: str,
    remediation_ts: datetime | None,
    config: StrategyConfig,
    lifecycle_evidence_state: str,
    lifecycle_evidence: dict[str, Any] | None,
) -> str:
    if not key_instance_id:
        return "receipt_attestor_key_instance_indeterminate"

    lifecycle_policy = str(getattr(config, "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_policy", "allow_local_policy_only") or "allow_local_policy_only").lower().strip()
    if lifecycle_policy not in {"require_anchored_evidence", "allow_local_policy_only"}:
        return "receipt_attestor_key_instance_indeterminate"
    if lifecycle_policy == "require_anchored_evidence":
        if lifecycle_evidence_state == "lifecycle_evidence_anchor_key_governance_dataset_invalid":
            return "receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_dataset_invalid"
        if lifecycle_evidence_state == "lifecycle_evidence_anchor_key_governance_dataset_indeterminate":
            return "receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_dataset_indeterminate"
        if lifecycle_evidence_state == "lifecycle_evidence_anchor_key_governance_anchor_revoked":
            return "receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_anchor_revoked"
        if lifecycle_evidence_state == "lifecycle_evidence_anchor_key_governance_anchor_indeterminate":
            return "receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_anchor_indeterminate"
        if lifecycle_evidence_state == "lifecycle_evidence_anchor_key_revoked":
            return "receipt_attestor_key_instance_lifecycle_evidence_anchor_key_revoked"
        if lifecycle_evidence_state == "lifecycle_evidence_anchor_key_indeterminate":
            return "receipt_attestor_key_instance_lifecycle_evidence_anchor_key_indeterminate"
        if lifecycle_evidence_state == "lifecycle_evidence_invalid":
            return "receipt_attestor_key_instance_lifecycle_evidence_invalid"
        if lifecycle_evidence_state != "lifecycle_evidence_anchored" or lifecycle_evidence is None:
            return "receipt_attestor_key_instance_lifecycle_evidence_indeterminate"
        anchored_state = str(lifecycle_evidence.get("lifecycle_state", "") or "").strip().lower()
        if anchored_state not in {"active", "revoked"}:
            return "receipt_attestor_key_instance_lifecycle_evidence_indeterminate"
        if anchored_state == "revoked":
            cutoff_raw = str(lifecycle_evidence.get("revoked_before_utc", "") or "").strip()
            if cutoff_raw:
                try:
                    cutoff_dt = datetime.fromisoformat(cutoff_raw)
                except Exception:
                    return "receipt_attestor_key_instance_lifecycle_evidence_indeterminate"
                if cutoff_dt.tzinfo is None:
                    cutoff_dt = cutoff_dt.replace(tzinfo=timezone.utc)
                else:
                    cutoff_dt = cutoff_dt.astimezone(timezone.utc)
                if remediation_ts is None:
                    return "receipt_attestor_key_instance_lifecycle_evidence_indeterminate"
                if remediation_ts >= cutoff_dt:
                    return "receipt_attestor_key_instance_revoked"
            else:
                return "receipt_attestor_key_instance_revoked"

    revoked = _revoked_retrieval_receipt_attestor_key_instances(config)
    lifecycle_policy = str(getattr(config, "remediation_retrieval_receipt_attestor_lifecycle_policy", "reject_all_revoked") or "reject_all_revoked").lower().strip()
    if lifecycle_policy not in {"reject_all_revoked", "allow_pre_revocation_only"}:
        return "receipt_attestor_key_instance_indeterminate"

    if key_instance_id in revoked:
        if lifecycle_policy == "reject_all_revoked":
            return "receipt_attestor_key_instance_revoked"
        cutoff = _revoked_retrieval_receipt_attestor_key_instance_cutoff_utc(config, key_instance_id)
        if cutoff is None or remediation_ts is None:
            return "receipt_attestor_key_instance_indeterminate"
        if remediation_ts >= cutoff:
            return "receipt_attestor_key_instance_revoked"

    rotation_policy = str(getattr(config, "remediation_retrieval_receipt_attestor_key_rotation_policy", "allow_historical_non_active") or "allow_historical_non_active").lower().strip()
    active = _active_retrieval_receipt_key_instances_for_attestor(config, attestor_id)
    if rotation_policy == "require_active_key_lineage":
        if not active:
            return "receipt_attestor_key_instance_indeterminate"
        if key_instance_id not in active:
            return "receipt_attestor_key_instance_revoked"
    elif rotation_policy == "allow_historical_non_active":
        pass
    else:
        return "receipt_attestor_key_instance_indeterminate"
    return "receipt_attestor_key_instance_valid"


def _retrieval_receipt_attestor_lifecycle_state(
    attestor_id: str,
    remediation_ts: datetime | None,
    config: StrategyConfig,
) -> str:
    revoked = _revoked_retrieval_receipt_attestors(config)
    lifecycle_policy = str(getattr(config, "remediation_retrieval_receipt_attestor_lifecycle_policy", "reject_all_revoked") or "reject_all_revoked").lower().strip()
    if lifecycle_policy not in {"reject_all_revoked", "allow_pre_revocation_only"}:
        return "receipt_attestor_indeterminate"

    if attestor_id in revoked:
        if lifecycle_policy == "reject_all_revoked":
            return "receipt_attestor_revoked"
        cutoff = _revoked_retrieval_receipt_attestor_cutoff_utc(config, attestor_id)
        if cutoff is None or remediation_ts is None:
            return "receipt_attestor_indeterminate"
        if remediation_ts >= cutoff:
            return "receipt_attestor_revoked"

    rotation_policy = str(getattr(config, "remediation_retrieval_receipt_attestor_rotation_policy", "allow_historical_non_active") or "allow_historical_non_active").lower().strip()
    active = _active_retrieval_receipt_attestors(config)
    if rotation_policy == "require_active_attestor_lineage":
        if not active:
            return "receipt_attestor_indeterminate"
        if attestor_id not in active:
            return "receipt_attestor_revoked"
    elif rotation_policy == "allow_historical_non_active":
        pass
    else:
        return "receipt_attestor_indeterminate"

    return "receipt_attestor_valid"


def _retrieval_receipt_state(
    row: dict[str, Any],
    bundle_id: str,
    evidence_hash: str,
    remediation_ts: datetime | None,
    config: StrategyConfig,
    lifecycle_evidence_state: str = "lifecycle_evidence_indeterminate",
    lifecycle_evidence_by_key: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> str:
    policy = str(getattr(config, "remediation_retrieval_receipt_auth_policy", "allow_unsigned_receipts") or "allow_unsigned_receipts").lower().strip()
    if policy not in {"require_authentic_receipts", "allow_unsigned_receipts"}:
        return "retrieval_receipt_indeterminate"

    attestor_id = str(row.get("retrieval_receipt_attestor_id", "") or "").strip()
    key_instance_id = str(row.get("retrieval_receipt_attestor_key_instance_id", "") or "").strip()
    source = str(row.get("retrieval_receipt_source", "") or "").strip()
    signature = str(row.get("retrieval_receipt_signature", "") or "").strip().lower()
    scheme = str(row.get("retrieval_receipt_signature_scheme", "") or "").strip().lower()
    role = str(row.get("role", "") or "").strip().lower()
    path = str(row.get("path", "") or "").strip()
    artifact_sha = str(row.get("sha256", "") or "").strip().lower()
    content_sha = str(row.get("content_sha256", "") or "").strip().lower()
    receipt_sha = str(row.get("retrieval_receipt_sha256", "") or "").strip().lower()

    has_auth_fields = any([attestor_id, key_instance_id, source, signature, scheme])
    if policy == "allow_unsigned_receipts" and not has_auth_fields:
        return "retrieval_receipt_authentic"

    if not attestor_id or not key_instance_id or not source or not signature or not scheme:
        return "retrieval_receipt_indeterminate"
    if not (source.startswith("https://") or source.startswith("ext://")):
        return "retrieval_receipt_invalid"
    if not _SHA256_HEX_RE.match(signature):
        return "retrieval_receipt_indeterminate"

    trusted = _trusted_retrieval_receipt_attestors(config)
    if attestor_id not in trusted:
        return "retrieval_receipt_invalid"

    lifecycle_state = _retrieval_receipt_attestor_lifecycle_state(attestor_id, remediation_ts, config)
    if lifecycle_state == "receipt_attestor_revoked":
        return "retrieval_receipt_attestor_revoked"
    if lifecycle_state == "receipt_attestor_indeterminate":
        return "retrieval_receipt_attestor_indeterminate"

    key_evidence = None
    if lifecycle_evidence_by_key is not None:
        key_evidence = lifecycle_evidence_by_key.get((attestor_id, key_instance_id))
    key_state = _retrieval_receipt_attestor_key_instance_state(attestor_id, key_instance_id, remediation_ts, config, lifecycle_evidence_state, key_evidence)
    if key_state == "receipt_attestor_key_instance_revoked":
        return "retrieval_receipt_attestor_key_instance_revoked"
    if key_state == "receipt_attestor_key_instance_indeterminate":
        return "retrieval_receipt_attestor_key_instance_indeterminate"
    if key_state == "receipt_attestor_key_instance_lifecycle_evidence_invalid":
        return "retrieval_receipt_attestor_key_instance_lifecycle_evidence_invalid"
    if key_state == "receipt_attestor_key_instance_lifecycle_evidence_indeterminate":
        return "retrieval_receipt_attestor_key_instance_lifecycle_evidence_indeterminate"
    if key_state == "receipt_attestor_key_instance_lifecycle_evidence_anchor_key_revoked":
        return "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_revoked"
    if key_state == "receipt_attestor_key_instance_lifecycle_evidence_anchor_key_indeterminate":
        return "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_indeterminate"
    if key_state == "receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_dataset_invalid":
        return "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_dataset_invalid"
    if key_state == "receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_dataset_indeterminate":
        return "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_dataset_indeterminate"
    if key_state == "receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_anchor_revoked":
        return "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_anchor_revoked"
    if key_state == "receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_anchor_indeterminate":
        return "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_anchor_indeterminate"

    expected = _expected_retrieval_receipt_signature(
        scheme,
        attestor_id,
        key_instance_id,
        source,
        bundle_id,
        evidence_hash,
        role,
        path,
        artifact_sha,
        content_sha,
        receipt_sha,
    )
    if expected is None:
        return "retrieval_receipt_indeterminate"
    if signature != expected:
        return "retrieval_receipt_invalid"
    return "retrieval_receipt_authentic"


def _derive_receipt_bundle_state(receipt_states: list[str]) -> str:
    if not receipt_states:
        return "retrieval_receipt_invalid"
    if any(state == "retrieval_receipt_attestor_key_instance_indeterminate" for state in receipt_states):
        return "retrieval_receipt_attestor_key_instance_indeterminate"
    if any(state == "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_anchor_indeterminate" for state in receipt_states):
        return "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_anchor_indeterminate"
    if any(state == "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_anchor_revoked" for state in receipt_states):
        return "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_anchor_revoked"
    if any(state == "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_dataset_indeterminate" for state in receipt_states):
        return "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_dataset_indeterminate"
    if any(state == "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_dataset_invalid" for state in receipt_states):
        return "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_dataset_invalid"
    if any(state == "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_indeterminate" for state in receipt_states):
        return "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_indeterminate"
    if any(state == "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_revoked" for state in receipt_states):
        return "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_revoked"
    if any(state == "retrieval_receipt_attestor_key_instance_lifecycle_evidence_indeterminate" for state in receipt_states):
        return "retrieval_receipt_attestor_key_instance_lifecycle_evidence_indeterminate"
    if any(state == "retrieval_receipt_attestor_key_instance_lifecycle_evidence_invalid" for state in receipt_states):
        return "retrieval_receipt_attestor_key_instance_lifecycle_evidence_invalid"
    if any(state == "retrieval_receipt_attestor_key_instance_revoked" for state in receipt_states):
        return "retrieval_receipt_attestor_key_instance_revoked"
    if any(state == "retrieval_receipt_attestor_indeterminate" for state in receipt_states):
        return "retrieval_receipt_attestor_indeterminate"
    if any(state == "retrieval_receipt_attestor_revoked" for state in receipt_states):
        return "retrieval_receipt_attestor_revoked"
    if any(state == "retrieval_receipt_indeterminate" for state in receipt_states):
        return "retrieval_receipt_indeterminate"
    if any(state == "retrieval_receipt_invalid" for state in receipt_states):
        return "retrieval_receipt_invalid"
    return "retrieval_receipt_authentic"


def _manual_remediation_retrieval_error(entry: JournalEntry, config: StrategyConfig) -> str | None:
    policy = str(getattr(config, "remediation_evidence_retrieval_policy", "allow_manifest_without_retrieval_proofs") or "allow_manifest_without_retrieval_proofs").lower().strip()
    proofs_raw = str(getattr(entry, "remediation_evidence_retrieval_proofs_json", "") or "").strip()
    proofs_hash = str(getattr(entry, "remediation_evidence_retrieval_proofs_hash_sha256", "") or "").strip().lower()
    if policy not in {"require_retrieval_proofs", "allow_manifest_without_retrieval_proofs"}:
        return "manual_remediation_retrieval_policy_invalid"
    if policy == "allow_manifest_without_retrieval_proofs" and not proofs_raw and not proofs_hash:
        return None
    if not proofs_raw or not proofs_hash:
        return "manual_remediation_retrieval_indeterminate"
    if not _SHA256_HEX_RE.match(proofs_hash):
        return "manual_remediation_retrieval_indeterminate"

    manifest_raw = str(getattr(entry, "remediation_evidence_manifest_json", "") or "").strip()
    if not manifest_raw:
        return "manual_remediation_retrieval_indeterminate"
    try:
        manifest_obj = json.loads(manifest_raw)
    except Exception:
        return "manual_remediation_retrieval_indeterminate"
    if not isinstance(manifest_obj, dict):
        return "manual_remediation_retrieval_indeterminate"
    artifacts = manifest_obj.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return "manual_remediation_retrieval_indeterminate"
    required_keys: set[tuple[str, str, str]] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            return "manual_remediation_retrieval_indeterminate"
        if bool(item.get("required", False)) is not True:
            continue
        role = str(item.get("role", "") or "").strip().lower()
        path = str(item.get("path", "") or "").strip()
        sha = str(item.get("sha256", "") or "").strip().lower()
        if not role or not path or not _SHA256_HEX_RE.match(sha):
            return "manual_remediation_retrieval_indeterminate"
        required_keys.add((role, path, sha))
    if not required_keys:
        return "manual_remediation_retrieval_invalid"

    try:
        proofs_obj = json.loads(proofs_raw)
    except Exception:
        return "manual_remediation_retrieval_indeterminate"
    if not isinstance(proofs_obj, dict):
        return "manual_remediation_retrieval_indeterminate"
    bundle_id = str(getattr(entry, "remediation_bundle_id", "") or "").strip()
    evidence_hash = str(getattr(entry, "remediation_evidence_hash_sha256", "") or "").strip().lower()
    p_bundle_id = str(proofs_obj.get("bundle_id", "") or "").strip()
    p_evidence_hash = str(proofs_obj.get("evidence_hash_sha256", "") or "").strip().lower()
    if p_bundle_id != bundle_id or p_evidence_hash != evidence_hash:
        return "manual_remediation_retrieval_invalid"

    retrievals = proofs_obj.get("retrievals")
    if not isinstance(retrievals, list):
        return "manual_remediation_retrieval_indeterminate"
    retrieval_map: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    normalized: list[dict[str, Any]] = []
    for row in retrievals:
        if not isinstance(row, dict):
            return "manual_remediation_retrieval_indeterminate"
        role = str(row.get("role", "") or "").strip().lower()
        path = str(row.get("path", "") or "").strip()
        sha = str(row.get("sha256", "") or "").strip().lower()
        available = row.get("available", None)
        content_sha = str(row.get("content_sha256", "") or "").strip().lower()
        receipt = str(row.get("retrieval_receipt_sha256", "") or "").strip().lower()
        if not role or not path or not _SHA256_HEX_RE.match(sha) or not _SHA256_HEX_RE.match(content_sha):
            return "manual_remediation_retrieval_indeterminate"
        if not isinstance(available, bool):
            return "manual_remediation_retrieval_indeterminate"
        if not _SHA256_HEX_RE.match(receipt):
            return "manual_remediation_retrieval_indeterminate"
        key = (role, path, sha)
        attestor_id = str(row.get("retrieval_receipt_attestor_id", "") or "").strip()
        receipt_key_instance_id = str(row.get("retrieval_receipt_attestor_key_instance_id", "") or "").strip()
        receipt_source = str(row.get("retrieval_receipt_source", "") or "").strip()
        receipt_signature = str(row.get("retrieval_receipt_signature", "") or "").strip().lower()
        receipt_scheme = str(row.get("retrieval_receipt_signature_scheme", "") or "").strip().lower()
        retrieval_map.setdefault(key, []).append(
            {
                "available": available,
                "content_sha256": content_sha,
                "retrieval_receipt_sha256": receipt,
                "retrieval_receipt_attestor_id": attestor_id,
                "retrieval_receipt_attestor_key_instance_id": receipt_key_instance_id,
                "retrieval_receipt_source": receipt_source,
                "retrieval_receipt_signature": receipt_signature,
                "retrieval_receipt_signature_scheme": receipt_scheme,
                "role": role,
                "path": path,
                "sha256": sha,
            }
        )
        normalized.append(
            {
                "role": role,
                "path": path,
                "sha256": sha,
                "available": True,
                "content_sha256": content_sha,
                "retrieval_receipt_sha256": receipt,
                "retrieval_receipt_attestor_id": attestor_id,
                "retrieval_receipt_attestor_key_instance_id": receipt_key_instance_id,
                "retrieval_receipt_source": receipt_source,
                "retrieval_receipt_signature": receipt_signature,
                "retrieval_receipt_signature_scheme": receipt_scheme,
            }
        )

    artifact_states = [_retrieval_artifact_state(required_key, retrieval_map.get(required_key, [])) for required_key in sorted(required_keys)]
    bundle_state = _derive_retrieval_bundle_state(artifact_states)
    if bundle_state == "retrieval_indeterminate_bundle":
        return "manual_remediation_retrieval_indeterminate"
    if bundle_state == "retrieval_invalid_bundle":
        return "manual_remediation_retrieval_invalid"

    remediation_ts = datetime.fromisoformat(_normalized_timestamp_value(getattr(entry, "remediation_timestamp_utc", None))) if getattr(entry, "remediation_timestamp_utc", None) else None
    identity_provenance_anchor_state, identity_provenance_anchor_records = _parse_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance(entry, p_bundle_id, p_evidence_hash, config)
    governance_anchor_identity_state, governance_anchor_identity_records = _parse_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance(
        entry,
        p_bundle_id,
        p_evidence_hash,
        remediation_ts,
        identity_provenance_anchor_state,
        identity_provenance_anchor_records,
        config,
    )
    governance_dataset_state, governance_dataset_records = _parse_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset(
        entry,
        p_bundle_id,
        p_evidence_hash,
        remediation_ts,
        governance_anchor_identity_state,
        governance_anchor_identity_records,
        config,
    )
    lifecycle_evidence_state, lifecycle_evidence_by_key = _parse_retrieval_receipt_attestor_key_lifecycle_evidence(
        entry,
        p_bundle_id,
        p_evidence_hash,
        remediation_ts,
        governance_dataset_state,
        governance_dataset_records,
        config,
    )
    receipt_states = [
        _retrieval_receipt_state(
            retrieval_map.get(required_key, [{}])[0],
            p_bundle_id,
            p_evidence_hash,
            remediation_ts,
            config,
            lifecycle_evidence_state,
            lifecycle_evidence_by_key,
        )
        for required_key in sorted(required_keys)
    ]
    receipt_bundle_state = _derive_receipt_bundle_state(receipt_states)
    if receipt_bundle_state == "retrieval_receipt_attestor_key_instance_indeterminate":
        return "manual_remediation_retrieval_receipt_attestor_key_instance_indeterminate"
    if receipt_bundle_state == "retrieval_receipt_attestor_key_instance_revoked":
        return "manual_remediation_retrieval_receipt_attestor_key_instance_revoked"
    if receipt_bundle_state == "retrieval_receipt_attestor_key_instance_lifecycle_evidence_indeterminate":
        return "manual_remediation_retrieval_receipt_attestor_key_instance_lifecycle_evidence_indeterminate"
    if receipt_bundle_state == "retrieval_receipt_attestor_key_instance_lifecycle_evidence_invalid":
        return "manual_remediation_retrieval_receipt_attestor_key_instance_lifecycle_evidence_invalid"
    if receipt_bundle_state == "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_revoked":
        return "manual_remediation_retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_revoked"
    if receipt_bundle_state == "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_indeterminate":
        return "manual_remediation_retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_indeterminate"
    if receipt_bundle_state == "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_dataset_invalid":
        return "manual_remediation_retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_dataset_invalid"
    if receipt_bundle_state == "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_dataset_indeterminate":
        return "manual_remediation_retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_dataset_indeterminate"
    if receipt_bundle_state == "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_anchor_revoked":
        return "manual_remediation_retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_anchor_revoked"
    if receipt_bundle_state == "retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_anchor_indeterminate":
        return "manual_remediation_retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_anchor_indeterminate"
    if receipt_bundle_state == "retrieval_receipt_attestor_indeterminate":
        return "manual_remediation_retrieval_receipt_attestor_indeterminate"
    if receipt_bundle_state == "retrieval_receipt_attestor_revoked":
        return "manual_remediation_retrieval_receipt_attestor_revoked"
    if receipt_bundle_state == "retrieval_receipt_indeterminate":
        return "manual_remediation_retrieval_receipt_indeterminate"
    if receipt_bundle_state == "retrieval_receipt_invalid":
        return "manual_remediation_retrieval_receipt_invalid"

    canonical_obj = {
        "bundle_id": p_bundle_id,
        "evidence_hash_sha256": p_evidence_hash,
        "retrievals": sorted(
            normalized,
            key=lambda r: (r["role"], r["path"], r["sha256"], r["content_sha256"], r["retrieval_receipt_sha256"], r["retrieval_receipt_attestor_id"], r["retrieval_receipt_attestor_key_instance_id"], r["retrieval_receipt_source"], r["retrieval_receipt_signature"], r["retrieval_receipt_signature_scheme"]),
        ),
    }
    canonical_text = json.dumps(canonical_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    canonical_hash = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    if canonical_hash != proofs_hash:
        return "manual_remediation_retrieval_invalid"
    return None




def _trusted_remediation_signers(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_trusted_signer_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_remediation_signers(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_revoked_signer_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_signer_cutoff_utc(config: StrategyConfig, signer_id: str) -> datetime | None:
    mapping = getattr(config, "remediation_signer_revoked_before_utc", None) or {}
    value = mapping.get(str(signer_id))
    if value is None:
        return None
    return datetime.fromisoformat(str(value)).astimezone(timezone.utc) if datetime.fromisoformat(str(value)).tzinfo else datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)


def _revoked_key_instances(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_key_instance_revoked_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_key_instance_cutoff_utc(config: StrategyConfig, key_instance_id: str) -> datetime | None:
    mapping = getattr(config, "remediation_key_instance_revoked_before_utc", None) or {}
    value = mapping.get(str(key_instance_id))
    if value is None:
        return None
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _active_key_instances_for_signer(config: StrategyConfig, signer_id: str) -> set[str]:
    mapping = getattr(config, "remediation_signer_active_key_instances", None) or {}
    vals = mapping.get(str(signer_id), ())
    if isinstance(vals, str):
        return {vals.strip()} if vals.strip() else set()
    return {str(v).strip() for v in vals if str(v).strip()}


def _trusted_issuers(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_trusted_issuer_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_issuers(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_revoked_issuer_ids", ())
    if isinstance(raw, str):
        return {raw.strip()} if raw.strip() else set()
    return {str(v).strip() for v in raw if str(v).strip()}


def _revoked_issuer_cutoff_utc(config: StrategyConfig, issuer_id: str) -> datetime | None:
    mapping = getattr(config, "remediation_issuer_revoked_before_utc", None) or {}
    value = mapping.get(str(issuer_id))
    if value is None:
        return None
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _key_instance_issuer_binding(config: StrategyConfig, key_instance_id: str) -> str | None:
    mapping = getattr(config, "remediation_key_instance_issuer_bindings", None) or {}
    value = mapping.get(str(key_instance_id))
    if value is None:
        return None
    return str(value).strip()


def _trusted_root_fingerprints(config: StrategyConfig) -> set[str]:
    raw = getattr(config, "remediation_trusted_root_fingerprints", ())
    if isinstance(raw, str):
        values = [raw]
    else:
        values = list(raw)
    return {str(v).strip().lower() for v in values if str(v).strip()}


def _issuer_root_fingerprint(config: StrategyConfig, issuer_id: str) -> str | None:
    mapping = getattr(config, "remediation_issuer_root_fingerprints", None)
    if mapping is None:
        mapping = {"ops-root-ca": "f" * 64}
    value = mapping.get(str(issuer_id))
    if value is None:
        return None
    return str(value).strip().lower()


def _coerced_int_config(config: StrategyConfig, field_name: str, default: int) -> int:
    raw = getattr(config, field_name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _parse_certificate_objects(entry: JournalEntry) -> dict[str, dict] | None:
    raw = str(getattr(entry, "remediation_certificate_objects", "") or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    if not isinstance(obj, list) or not obj:
        return None
    store: dict[str, dict] = {}
    for row in obj:
        if not isinstance(row, dict):
            return None
        fp = str(row.get("fingerprint_sha256", "") or "").strip().lower()
        parent_fp = str(row.get("issuer_fingerprint_sha256", "") or "").strip().lower()
        n_hex = str(row.get("public_key_n_hex", "") or "").strip().lower()
        e_raw = row.get("public_key_e")
        if not _SHA256_HEX_RE.match(fp):
            return None
        if parent_fp and not _SHA256_HEX_RE.match(parent_fp):
            return None
        if not n_hex or any(ch not in "0123456789abcdef" for ch in n_hex):
            return None
        try:
            e_int = int(e_raw)
            n_int = int(n_hex, 16)
        except Exception:
            return None
        if e_int <= 1 or n_int <= 1:
            return None
        if fp in store:
            return None
        store[fp] = {
            "fingerprint_sha256": fp,
            "issuer_fingerprint_sha256": parent_fp,
            "public_key_n": n_int,
            "public_key_e": e_int,
            "serial_number_hex": str(row.get("serial_number_hex", "") or "").strip().lower(),
            "subject_cn": str(row.get("subject_cn", "") or "").strip(),
            "issuer_cn": str(row.get("issuer_cn", "") or "").strip(),
            "signature_algorithm": str(row.get("signature_algorithm", "") or "").strip().lower(),
            "not_before_utc": str(row.get("not_before_utc", "") or "").strip(),
            "not_after_utc": str(row.get("not_after_utc", "") or "").strip(),
            "basic_constraints_ca": row.get("basic_constraints_ca", None),
            "key_usage_cert_sign": row.get("key_usage_cert_sign", None),
        }
    return store


def _certificate_semantic_error(
    entry: JournalEntry,
    certificate_store: dict[str, dict],
    path_nodes: list[str],
    effective_scheme: str,
) -> str | None:
    remediation_ts = _entry_remediation_utc(entry)
    if remediation_ts is None:
        return "manual_remediation_certificate_semantically_indeterminate"

    expected_alg = "rsa_raw_sha256" if effective_scheme == "inprocess_rsa_raw_sha256_v1" else ""
    if not expected_alg:
        return "manual_remediation_certificate_semantically_indeterminate"

    for idx, node in enumerate(path_nodes):
        cert_obj = certificate_store.get(node)
        if cert_obj is None:
            return "manual_remediation_certificate_semantically_indeterminate"

        serial_hex = str(cert_obj.get("serial_number_hex", "") or "").strip().lower()
        if not serial_hex or any(ch not in "0123456789abcdef" for ch in serial_hex):
            return "manual_remediation_certificate_semantically_indeterminate"

        subject_cn = str(cert_obj.get("subject_cn", "") or "").strip()
        issuer_cn = str(cert_obj.get("issuer_cn", "") or "").strip()
        if not subject_cn or not issuer_cn:
            return "manual_remediation_certificate_semantically_indeterminate"

        signature_algorithm = str(cert_obj.get("signature_algorithm", "") or "").strip().lower()
        if not signature_algorithm:
            return "manual_remediation_certificate_semantically_indeterminate"
        if signature_algorithm != expected_alg:
            return "manual_remediation_certificate_semantically_invalid"

        not_before_raw = str(cert_obj.get("not_before_utc", "") or "").strip()
        not_after_raw = str(cert_obj.get("not_after_utc", "") or "").strip()
        if not not_before_raw or not not_after_raw:
            return "manual_remediation_certificate_semantically_indeterminate"
        try:
            not_before = datetime.fromisoformat(not_before_raw)
            not_after = datetime.fromisoformat(not_after_raw)
        except Exception:
            return "manual_remediation_certificate_semantically_indeterminate"
        if not_before.tzinfo is None:
            not_before = not_before.replace(tzinfo=timezone.utc)
        else:
            not_before = not_before.astimezone(timezone.utc)
        if not_after.tzinfo is None:
            not_after = not_after.replace(tzinfo=timezone.utc)
        else:
            not_after = not_after.astimezone(timezone.utc)
        if not_before >= not_after:
            return "manual_remediation_certificate_semantically_invalid"
        if remediation_ts < not_before or remediation_ts > not_after:
            return "manual_remediation_certificate_semantically_invalid"

        if idx < len(path_nodes) - 1:
            parent_fp = path_nodes[idx + 1]
            parent_obj = certificate_store.get(parent_fp)
            if parent_obj is None:
                return "manual_remediation_certificate_semantically_indeterminate"
            parent_subject_cn = str(parent_obj.get("subject_cn", "") or "").strip()
            if not parent_subject_cn:
                return "manual_remediation_certificate_semantically_indeterminate"
            if issuer_cn != parent_subject_cn:
                return "manual_remediation_certificate_semantically_invalid"

    for parent_fp in path_nodes[1:]:
        parent_obj = certificate_store.get(parent_fp)
        if parent_obj is None:
            return "manual_remediation_certificate_semantically_indeterminate"
        parent_is_ca = parent_obj.get("basic_constraints_ca", None)
        parent_can_sign = parent_obj.get("key_usage_cert_sign", None)
        if parent_is_ca is None or parent_can_sign is None:
            return "manual_remediation_certificate_semantically_indeterminate"
        if bool(parent_is_ca) is not True or bool(parent_can_sign) is not True:
            return "manual_remediation_certificate_semantically_invalid"

    return None


def _verify_inprocess_rsa_raw_sha256_signature(n: int, e: int, message: str, signature_hex: str) -> bool | None:
    sig = str(signature_hex or "").strip().lower()
    if not sig or any(ch not in "0123456789abcdef" for ch in sig):
        return None
    try:
        sig_int = int(sig, 16)
    except Exception:
        return None
    if sig_int <= 0 or sig_int >= n:
        return False
    digest_int = int(hashlib.sha256(message.encode("utf-8")).hexdigest(), 16)
    verified = pow(sig_int, e, n)
    return verified == (digest_int % n)


def _der_read_tlv(data: bytes, offset: int) -> tuple[int, int, bytes, int] | None:
    if offset >= len(data):
        return None
    tag = data[offset]
    offset += 1
    if offset >= len(data):
        return None
    first_len = data[offset]
    offset += 1
    if first_len & 0x80:
        nbytes = first_len & 0x7F
        if nbytes <= 0 or nbytes > 4 or offset + nbytes > len(data):
            return None
        length = int.from_bytes(data[offset:offset + nbytes], "big")
        offset += nbytes
    else:
        length = first_len
    if length < 0 or offset + length > len(data):
        return None
    value = data[offset:offset + length]
    return tag, length, value, offset + length


def _der_decode_oid(value: bytes) -> str | None:
    if not value:
        return None
    first = value[0]
    arcs = [first // 40, first % 40]
    cur = 0
    for b in value[1:]:
        cur = (cur << 7) | (b & 0x7F)
        if not (b & 0x80):
            arcs.append(cur)
            cur = 0
    if cur != 0:
        return None
    return ".".join(str(a) for a in arcs)


def _der_decode_time(tag: int, value: bytes) -> datetime | None:
    try:
        text = value.decode("ascii")
    except Exception:
        return None
    if tag == 0x17:  # UTCTime YYMMDDHHMMSSZ
        if len(text) != 13 or not text.endswith("Z"):
            return None
        year = int(text[0:2])
        year += 2000 if year < 50 else 1900
        month = int(text[2:4])
        day = int(text[4:6])
        hour = int(text[6:8])
        minute = int(text[8:10])
        second = int(text[10:12])
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    if tag == 0x18:  # GeneralizedTime YYYYMMDDHHMMSSZ
        if len(text) != 15 or not text.endswith("Z"):
            return None
        year = int(text[0:4])
        month = int(text[4:6])
        day = int(text[6:8])
        hour = int(text[8:10])
        minute = int(text[10:12])
        second = int(text[12:14])
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return None


def _der_extract_cn(name_der: bytes) -> str | None:
    rdn_seq = _der_read_tlv(name_der, 0)
    if rdn_seq is not None and rdn_seq[0] == 0x30 and rdn_seq[3] == len(name_der):
        data = rdn_seq[2]
    else:
        data = name_der
    off = 0
    cn_oid = "2.5.4.3"
    while off < len(data):
        set_tlv = _der_read_tlv(data, off)
        if set_tlv is None or set_tlv[0] != 0x31:
            return None
        attr = _der_read_tlv(set_tlv[2], 0)
        if attr and attr[0] == 0x30:
            oid_tlv = _der_read_tlv(attr[2], 0)
            if oid_tlv and oid_tlv[0] == 0x06:
                oid = _der_decode_oid(oid_tlv[2])
                val_tlv = _der_read_tlv(attr[2], oid_tlv[3])
                if oid == cn_oid and val_tlv is not None:
                    try:
                        return val_tlv[2].decode("utf-8", errors="ignore")
                    except Exception:
                        return None
        off = set_tlv[3]
    return None


def _verify_pkcs1v15_sha256_signature(n: int, e: int, tbs: bytes, signature: bytes) -> bool:
    if n <= 1 or e <= 1:
        return False
    sig_int = int.from_bytes(signature, "big")
    if sig_int <= 0 or sig_int >= n:
        return False
    k = (n.bit_length() + 7) // 8
    em = pow(sig_int, e, n).to_bytes(k, "big")
    digest = hashlib.sha256(tbs).digest()
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + digest
    if len(em) < len(digest_info) + 11:
        return False
    if not (em[0] == 0x00 and em[1] == 0x01):
        return False
    sep = em.find(b"\x00", 2)
    if sep < 10:
        return False
    if any(b != 0xFF for b in em[2:sep]):
        return False
    return em[sep + 1:] == digest_info


def _parse_der_certificate_chain(entry: JournalEntry, path_nodes: list[str]) -> tuple[list[dict[str, Any]] | None, str | None]:
    raw = str(getattr(entry, "remediation_certificate_der_hex_chain", "") or "").strip()
    if not raw:
        return None, "manual_remediation_certificate_der_tbs_indeterminate"
    try:
        items = json.loads(raw)
    except Exception:
        return None, "manual_remediation_certificate_der_tbs_indeterminate"
    if not isinstance(items, list) or len(items) != len(path_nodes):
        return None, "manual_remediation_certificate_der_tbs_indeterminate"

    parsed: list[dict[str, Any]] = []
    for idx, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            return None, "manual_remediation_certificate_der_tbs_indeterminate"
        try:
            der = bytes.fromhex(item.strip())
        except Exception:
            return None, "manual_remediation_certificate_der_tbs_indeterminate"
        fp = hashlib.sha256(der).hexdigest()
        if fp != path_nodes[idx]:
            return None, "manual_remediation_certificate_der_tbs_invalid"
        cert = _der_read_tlv(der, 0)
        if cert is None or cert[0] != 0x30 or cert[3] != len(der):
            return None, "manual_remediation_certificate_der_tbs_indeterminate"
        cert_seq = cert[2]
        tbs_tlv = _der_read_tlv(cert_seq, 0)
        tbs_full = cert_seq[0:tbs_tlv[3]] if tbs_tlv is not None else b""
        sig_alg_tlv = _der_read_tlv(cert_seq, tbs_tlv[3] if tbs_tlv else 0)
        sig_val_tlv = _der_read_tlv(cert_seq, sig_alg_tlv[3] if sig_alg_tlv else 0)
        if tbs_tlv is None or sig_alg_tlv is None or sig_val_tlv is None:
            return None, "manual_remediation_certificate_der_tbs_indeterminate"
        if tbs_tlv[0] != 0x30 or sig_alg_tlv[0] != 0x30 or sig_val_tlv[0] != 0x03:
            return None, "manual_remediation_certificate_der_tbs_indeterminate"
        if not sig_val_tlv[2] or sig_val_tlv[2][0] != 0:
            return None, "manual_remediation_certificate_der_tbs_indeterminate"

        tbs = tbs_tlv[2]
        tbs_off = 0
        first = _der_read_tlv(tbs, tbs_off)
        if first is None:
            return None, "manual_remediation_certificate_der_tbs_indeterminate"
        if first[0] == 0xA0:
            tbs_off = first[3]
            first = _der_read_tlv(tbs, tbs_off)
            if first is None:
                return None, "manual_remediation_certificate_der_tbs_indeterminate"
        serial_tlv = first
        sig_tbs_tlv = _der_read_tlv(tbs, serial_tlv[3])
        issuer_tlv = _der_read_tlv(tbs, sig_tbs_tlv[3] if sig_tbs_tlv else 0)
        validity_tlv = _der_read_tlv(tbs, issuer_tlv[3] if issuer_tlv else 0)
        subject_tlv = _der_read_tlv(tbs, validity_tlv[3] if validity_tlv else 0)
        spki_tlv = _der_read_tlv(tbs, subject_tlv[3] if subject_tlv else 0)
        if None in {serial_tlv, sig_tbs_tlv, issuer_tlv, validity_tlv, subject_tlv, spki_tlv}:
            return None, "manual_remediation_certificate_der_tbs_indeterminate"
        if serial_tlv[0] != 0x02 or sig_tbs_tlv[0] != 0x30 or issuer_tlv[0] != 0x30 or validity_tlv[0] != 0x30 or subject_tlv[0] != 0x30 or spki_tlv[0] != 0x30:
            return None, "manual_remediation_certificate_der_tbs_indeterminate"

        sig_oid_tlv = _der_read_tlv(sig_alg_tlv[2], 0)
        tbs_sig_oid_tlv = _der_read_tlv(sig_tbs_tlv[2], 0)
        if sig_oid_tlv is None or tbs_sig_oid_tlv is None or sig_oid_tlv[0] != 0x06 or tbs_sig_oid_tlv[0] != 0x06:
            return None, "manual_remediation_certificate_der_tbs_indeterminate"
        sig_oid = _der_decode_oid(sig_oid_tlv[2])
        tbs_sig_oid = _der_decode_oid(tbs_sig_oid_tlv[2])
        if sig_oid is None or tbs_sig_oid is None:
            return None, "manual_remediation_certificate_der_tbs_indeterminate"
        if sig_oid != tbs_sig_oid:
            return None, "manual_remediation_certificate_der_tbs_invalid"

        vt1 = _der_read_tlv(validity_tlv[2], 0)
        vt2 = _der_read_tlv(validity_tlv[2], vt1[3] if vt1 else 0)
        if vt1 is None or vt2 is None:
            return None, "manual_remediation_certificate_der_tbs_indeterminate"
        not_before = _der_decode_time(vt1[0], vt1[2])
        not_after = _der_decode_time(vt2[0], vt2[2])
        if not_before is None or not_after is None:
            return None, "manual_remediation_certificate_der_tbs_indeterminate"

        issuer_cn = _der_extract_cn(issuer_tlv[2])
        subject_cn = _der_extract_cn(subject_tlv[2])
        if not issuer_cn or not subject_cn:
            return None, "manual_remediation_certificate_der_tbs_indeterminate"

        spki_alg = _der_read_tlv(spki_tlv[2], 0)
        spki_bit = _der_read_tlv(spki_tlv[2], spki_alg[3] if spki_alg else 0)
        if spki_alg is None or spki_bit is None or spki_alg[0] != 0x30 or spki_bit[0] != 0x03 or not spki_bit[2] or spki_bit[2][0] != 0:
            return None, "manual_remediation_certificate_der_tbs_indeterminate"
        spki_oid_tlv = _der_read_tlv(spki_alg[2], 0)
        if spki_oid_tlv is None or spki_oid_tlv[0] != 0x06 or _der_decode_oid(spki_oid_tlv[2]) != "1.2.840.113549.1.1.1":
            return None, "manual_remediation_certificate_der_tbs_indeterminate"
        rsa_key = _der_read_tlv(spki_bit[2][1:], 0)
        if rsa_key is None or rsa_key[0] != 0x30:
            return None, "manual_remediation_certificate_der_tbs_indeterminate"
        mod_tlv = _der_read_tlv(rsa_key[2], 0)
        exp_tlv = _der_read_tlv(rsa_key[2], mod_tlv[3] if mod_tlv else 0)
        if mod_tlv is None or exp_tlv is None or mod_tlv[0] != 0x02 or exp_tlv[0] != 0x02:
            return None, "manual_remediation_certificate_der_tbs_indeterminate"
        modulus = int.from_bytes(mod_tlv[2], "big", signed=False)
        exponent = int.from_bytes(exp_tlv[2], "big", signed=False)

        basic_ca: bool | None = None
        key_cert_sign: bool | None = None
        ext_cursor = spki_tlv[3]
        while ext_cursor < len(tbs):
            ext_tlv = _der_read_tlv(tbs, ext_cursor)
            if ext_tlv is None:
                return None, "manual_remediation_certificate_der_tbs_indeterminate"
            ext_cursor = ext_tlv[3]
            if ext_tlv[0] != 0xA3:
                continue
            ext_seq = _der_read_tlv(ext_tlv[2], 0)
            if ext_seq is None or ext_seq[0] != 0x30:
                return None, "manual_remediation_certificate_der_tbs_indeterminate"
            eo = 0
            while eo < len(ext_seq[2]):
                one_ext = _der_read_tlv(ext_seq[2], eo)
                if one_ext is None or one_ext[0] != 0x30:
                    return None, "manual_remediation_certificate_der_tbs_indeterminate"
                eo = one_ext[3]
                oid_tlv = _der_read_tlv(one_ext[2], 0)
                if oid_tlv is None or oid_tlv[0] != 0x06:
                    return None, "manual_remediation_certificate_der_tbs_indeterminate"
                oid = _der_decode_oid(oid_tlv[2])
                rest_off = oid_tlv[3]
                critical = False
                next_tlv = _der_read_tlv(one_ext[2], rest_off)
                if next_tlv is None:
                    return None, "manual_remediation_certificate_der_tbs_indeterminate"
                if next_tlv[0] == 0x01:
                    critical = next_tlv[2] != b"\x00"
                    rest_off = next_tlv[3]
                    next_tlv = _der_read_tlv(one_ext[2], rest_off)
                    if next_tlv is None:
                        return None, "manual_remediation_certificate_der_tbs_indeterminate"
                if next_tlv[0] != 0x04:
                    return None, "manual_remediation_certificate_der_tbs_indeterminate"
                ext_value = next_tlv[2]
                if oid == "2.5.29.19":
                    bc = _der_read_tlv(ext_value, 0)
                    if bc and bc[0] == 0x30:
                        bc_bool = _der_read_tlv(bc[2], 0)
                        basic_ca = bool(bc_bool and bc_bool[0] == 0x01 and bc_bool[2] != b"\x00")
                elif oid == "2.5.29.15":
                    ku = _der_read_tlv(ext_value, 0)
                    if ku and ku[0] == 0x03 and len(ku[2]) >= 2:
                        bits = ku[2][1:]
                        first = bits[0] if bits else 0
                        key_cert_sign = bool(first & (1 << 2))
                elif critical:
                    return None, "manual_remediation_certificate_critical_extension_unsupported"

        parsed.append(
            {
                "fingerprint": fp,
                "serial_hex": serial_tlv[2].hex(),
                "issuer_cn": issuer_cn,
                "subject_cn": subject_cn,
                "sig_oid": sig_oid,
                "not_before": not_before,
                "not_after": not_after,
                "modulus": modulus,
                "exponent": exponent,
                "signature": sig_val_tlv[2][1:],
                "tbs": tbs_full,
                "basic_ca": basic_ca,
                "key_cert_sign": key_cert_sign,
            }
        )
    return parsed, None


def _validate_der_tbs_chain(entry: JournalEntry, path_nodes: list[str]) -> str | None:
    parsed, parse_error = _parse_der_certificate_chain(entry, path_nodes)
    if parse_error is not None:
        return parse_error
    if parsed is None:
        return "manual_remediation_certificate_der_tbs_indeterminate"
    remediation_ts = _entry_remediation_utc(entry)
    if remediation_ts is None:
        return "manual_remediation_certificate_der_tbs_indeterminate"

    supported_sig_oid = "1.2.840.113549.1.1.11"
    for idx, cert in enumerate(parsed):
        if cert["sig_oid"] != supported_sig_oid:
            return "manual_remediation_certificate_der_tbs_indeterminate"
        if cert["not_before"] >= cert["not_after"]:
            return "manual_remediation_certificate_der_tbs_invalid"
        if remediation_ts < cert["not_before"] or remediation_ts > cert["not_after"]:
            return "manual_remediation_certificate_der_tbs_invalid"
        if idx < len(parsed) - 1:
            parent = parsed[idx + 1]
            if cert["issuer_cn"] != parent["subject_cn"]:
                return "manual_remediation_certificate_der_tbs_invalid"
            if parent.get("basic_ca") is not True or parent.get("key_cert_sign") is not True:
                return "manual_remediation_certificate_der_tbs_invalid"
            ok = _verify_pkcs1v15_sha256_signature(parent["modulus"], parent["exponent"], cert["tbs"], cert["signature"])
            if not ok:
                return "manual_remediation_certificate_der_tbs_invalid"
        else:
            ok = _verify_pkcs1v15_sha256_signature(cert["modulus"], cert["exponent"], cert["tbs"], cert["signature"])
            if not ok:
                return "manual_remediation_certificate_der_tbs_invalid"
    return None


def _manual_remediation_certificate_link_error(
    entry: JournalEntry,
    config: StrategyConfig,
    chain_id: str,
    path_nodes: list[str],
) -> str | None:
    policy = str(getattr(config, "remediation_certificate_link_policy", "allow_metadata_only") or "allow_metadata_only").lower().strip()
    sigs_raw = str(getattr(entry, "remediation_certificate_link_signatures", "") or "").strip()
    scheme = str(getattr(entry, "remediation_certificate_link_signature_scheme", "") or "").strip().lower()
    default_scheme = str(getattr(config, "remediation_certificate_link_signature_scheme", "inprocess_rsa_raw_sha256_v1") or "inprocess_rsa_raw_sha256_v1").strip().lower()

    if policy == "allow_metadata_only" and not sigs_raw and not scheme:
        return None
    if policy not in {"require_verified_links", "allow_metadata_only"}:
        return "manual_remediation_certificate_link_policy_invalid"

    effective_scheme = scheme or default_scheme
    if not effective_scheme:
        return "manual_remediation_certificate_link_internally_indeterminate"

    if effective_scheme == "der_tbs_rsa_sha256_v1":
        return _validate_der_tbs_chain(entry, path_nodes)

    if not sigs_raw:
        return "manual_remediation_certificate_link_internally_indeterminate"

    try:
        sigs = json.loads(sigs_raw)
    except Exception:
        return "manual_remediation_certificate_link_internally_indeterminate"
    if not isinstance(sigs, list):
        return "manual_remediation_certificate_link_internally_indeterminate"

    if len(path_nodes) < 2 or len(sigs) != len(path_nodes) - 1:
        return "manual_remediation_certificate_link_internally_indeterminate"

    certificate_store = _parse_certificate_objects(entry)
    if certificate_store is None:
        return "manual_remediation_certificate_link_internally_indeterminate"

    semantic_error = _certificate_semantic_error(entry, certificate_store, path_nodes, effective_scheme)
    if semantic_error is not None:
        return semantic_error

    for idx, node in enumerate(path_nodes):
        cert_obj = certificate_store.get(node)
        if cert_obj is None:
            return "manual_remediation_certificate_link_internally_indeterminate"
        if idx == len(path_nodes) - 1:
            continue
        expected_parent = path_nodes[idx + 1]
        if cert_obj.get("issuer_fingerprint_sha256", "") != expected_parent:
            return "manual_remediation_certificate_link_internally_invalid"

    for idx, raw_sig in enumerate(sigs):
        signature = str(raw_sig or "").strip()
        if not signature:
            return "manual_remediation_certificate_link_internally_indeterminate"
        child = path_nodes[idx]
        parent = path_nodes[idx + 1]

        if effective_scheme != "inprocess_rsa_raw_sha256_v1":
            return "manual_remediation_certificate_link_internally_indeterminate"

        parent_obj = certificate_store.get(parent)
        if parent_obj is None:
            return "manual_remediation_certificate_link_internally_indeterminate"
        message = "|".join([effective_scheme, str(chain_id).strip().lower(), parent, child])
        verified = _verify_inprocess_rsa_raw_sha256_signature(
            int(parent_obj["public_key_n"]),
            int(parent_obj["public_key_e"]),
            message,
            signature,
        )
        if verified is None:
            return "manual_remediation_certificate_link_internally_indeterminate"
        if verified is False:
            return "manual_remediation_certificate_link_internally_invalid"

    return None


def _manual_remediation_certificate_path_error(
    entry: JournalEntry,
    config: StrategyConfig,
    issuer_id: str,
    chain_id: str,
    leaf_fingerprint: str,
) -> str | None:
    policy = str(getattr(config, "remediation_certificate_path_policy", "require_verified_path") or "require_verified_path").lower().strip()
    path_raw = str(getattr(entry, "remediation_certificate_path_fingerprints", "") or "").strip()
    anchor = str(getattr(entry, "remediation_certificate_path_anchor_fingerprint_sha256", "") or "").strip().lower()

    if policy == "allow_metadata_only" and not path_raw and not anchor:
        return None
    if policy not in {"require_verified_path", "allow_metadata_only"}:
        return "manual_remediation_certificate_path_policy_invalid"

    if not path_raw or not anchor:
        return "manual_remediation_certificate_path_indeterminate"
    if not _SHA256_HEX_RE.match(anchor):
        return "manual_remediation_certificate_path_invalid"

    try:
        nodes = json.loads(path_raw)
    except Exception:
        return "manual_remediation_certificate_path_indeterminate"
    if not isinstance(nodes, list) or not nodes:
        return "manual_remediation_certificate_path_indeterminate"

    normalized = [str(v).strip().lower() for v in nodes]
    if any(not _SHA256_HEX_RE.match(v) for v in normalized):
        return "manual_remediation_certificate_path_invalid"

    min_depth = max(2, _coerced_int_config(config, "remediation_certificate_path_min_depth", 2) or 2)
    max_depth = max(min_depth, _coerced_int_config(config, "remediation_certificate_path_max_depth", 6) or 6)
    if len(normalized) < min_depth or len(normalized) > max_depth:
        return "manual_remediation_certificate_path_depth_invalid"

    if len(set(normalized)) != len(normalized):
        return "manual_remediation_certificate_path_conflict"

    if normalized[0] != leaf_fingerprint:
        return "manual_remediation_certificate_path_conflict"
    if normalized[-1] != anchor:
        return "manual_remediation_certificate_path_conflict"

    trusted_roots = _trusted_root_fingerprints(config)
    if not trusted_roots:
        return "manual_remediation_certificate_path_indeterminate"
    if anchor not in trusted_roots:
        return "manual_remediation_certificate_anchor_untrusted"

    expected_root = _issuer_root_fingerprint(config, issuer_id)
    if expected_root is None:
        return "manual_remediation_certificate_path_indeterminate"
    if expected_root != anchor:
        return "manual_remediation_certificate_path_conflict"

    if not chain_id:
        return "manual_remediation_certificate_path_indeterminate"

    link_error = _manual_remediation_certificate_link_error(entry, config, chain_id, normalized)
    if link_error is not None:
        return link_error

    return None


def _manual_remediation_issuer_error(entry: JournalEntry, config: StrategyConfig) -> str | None:
    issuer_id = str(getattr(entry, "remediation_issuer_id", "") or "").strip()
    chain_id = str(getattr(entry, "remediation_certificate_chain_id", "") or "").strip()
    fingerprint = str(getattr(entry, "remediation_certificate_fingerprint_sha256", "") or "").strip().lower()
    key_instance_id = str(getattr(entry, "remediation_key_instance_id", "") or "").strip()

    if not issuer_id or not chain_id or not fingerprint:
        return "manual_remediation_issuer_provenance_incomplete"
    if not _SHA256_HEX_RE.match(fingerprint):
        return "manual_remediation_certificate_fingerprint_invalid"

    trusted = _trusted_issuers(config)
    if issuer_id not in trusted:
        return "manual_remediation_issuer_untrusted"

    revoked = _revoked_issuers(config)
    if issuer_id in revoked:
        rev_policy = str(getattr(config, "remediation_issuer_revocation_policy", "reject_all_revoked") or "reject_all_revoked").lower().strip()
        if rev_policy == "reject_all_revoked":
            return "manual_remediation_issuer_revoked"
        if rev_policy != "allow_pre_revocation_only":
            return "manual_remediation_issuer_revocation_policy_invalid"
        cutoff = _revoked_issuer_cutoff_utc(config, issuer_id)
        remediation_ts = _entry_remediation_utc(entry)
        if cutoff is None or remediation_ts is None:
            return "manual_remediation_issuer_state_indeterminate"
        if remediation_ts >= cutoff:
            return "manual_remediation_issuer_revoked"

    chain_policy = str(getattr(config, "remediation_issuer_chain_policy", "require_key_instance_binding") or "require_key_instance_binding").lower().strip()
    bound_issuer = _key_instance_issuer_binding(config, key_instance_id)
    if chain_policy == "require_key_instance_binding":
        if not bound_issuer:
            return "manual_remediation_issuer_binding_indeterminate"
        if bound_issuer != issuer_id:
            return "manual_remediation_issuer_binding_conflict"
    elif chain_policy == "allow_unmapped_key_instance":
        if bound_issuer and bound_issuer != issuer_id:
            return "manual_remediation_issuer_binding_conflict"
    else:
        return "manual_remediation_issuer_chain_policy_invalid"

    path_error = _manual_remediation_certificate_path_error(entry, config, issuer_id, chain_id, fingerprint)
    if path_error is not None:
        return path_error

    return None


def _entry_remediation_utc(entry: JournalEntry) -> datetime | None:
    remediation_ts = getattr(entry, "remediation_timestamp_utc", None)
    if remediation_ts is None:
        return None
    ts_value = datetime.fromisoformat(str(remediation_ts))
    if ts_value.tzinfo is None:
        return ts_value.replace(tzinfo=timezone.utc)
    return ts_value.astimezone(timezone.utc)


def _manual_remediation_revocation_error(entry: JournalEntry, config: StrategyConfig) -> str | None:
    signer_id = str(getattr(entry, "remediation_signer_id", "") or "").strip()
    key_instance_id = str(getattr(entry, "remediation_key_instance_id", "") or "").strip()
    if not signer_id:
        return None
    if not key_instance_id:
        return "manual_remediation_key_instance_missing"

    remediation_ts = _entry_remediation_utc(entry)

    revoked_signers = _revoked_remediation_signers(config)
    signer_revoked = signer_id in revoked_signers

    revoked_key_instances = _revoked_key_instances(config)
    key_revoked = key_instance_id in revoked_key_instances

    policy = str(getattr(config, "remediation_revocation_policy", "reject_all_revoked") or "reject_all_revoked").lower().strip()
    if policy not in {"reject_all_revoked", "allow_pre_revocation_only"}:
        return "manual_remediation_revocation_policy_invalid"

    def _revoked_with_cutoff(cutoff: datetime | None) -> str | None:
        if policy == "reject_all_revoked":
            return "manual_remediation_key_instance_revoked"
        if cutoff is None or remediation_ts is None:
            return "manual_remediation_revocation_state_indeterminate"
        if remediation_ts >= cutoff:
            return "manual_remediation_key_instance_revoked"
        return None

    if key_revoked:
        err = _revoked_with_cutoff(_revoked_key_instance_cutoff_utc(config, key_instance_id))
        if err is not None:
            return err

    if signer_revoked:
        err = _revoked_with_cutoff(_revoked_signer_cutoff_utc(config, signer_id))
        if err is not None:
            return err

    rotation_policy = str(getattr(config, "remediation_key_rotation_policy", "require_active_key_lineage") or "require_active_key_lineage").lower().strip()
    active_key_instances = _active_key_instances_for_signer(config, signer_id)
    if rotation_policy == "require_active_key_lineage":
        if not active_key_instances:
            return "manual_remediation_key_lineage_indeterminate"
        if key_instance_id not in active_key_instances:
            return "manual_remediation_key_instance_not_active"
    elif rotation_policy == "allow_historical_non_active":
        pass
    else:
        return "manual_remediation_key_rotation_policy_invalid"

    return None


def _expected_external_signature(scheme: str, signer_id: str, key_instance_id: str, source: str, evidence_hash: str) -> str | None:
    normalized_scheme = str(scheme or "").strip().lower()
    if normalized_scheme != "sha256_signer_key_bind_v1":
        return None
    message = "|".join([normalized_scheme, str(signer_id).strip().lower(), str(key_instance_id).strip().lower(), str(source).strip().lower(), str(evidence_hash).strip().lower()])
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _manual_remediation_external_signature_error(entry: JournalEntry, config: StrategyConfig) -> str | None:
    policy = str(getattr(config, "remediation_external_signature_policy", "require") or "require").lower().strip()
    require_external = policy != "allow_internal_attestation"

    signer_id = str(getattr(entry, "remediation_signer_id", "") or "").strip()
    source = str(getattr(entry, "remediation_external_source", "") or "").strip()
    key_instance_id = str(getattr(entry, "remediation_key_instance_id", "") or "").strip()
    signature = str(getattr(entry, "remediation_signature", "") or "").strip().lower()
    scheme = str(getattr(entry, "remediation_signature_scheme", "") or "").strip().lower()

    has_external_fields = any([signer_id, key_instance_id, source, signature, scheme])
    if not require_external and not has_external_fields:
        return None

    if signer_id and not key_instance_id:
        return "manual_remediation_key_instance_missing"
    if not signer_id or not key_instance_id or not source or not signature or not scheme:
        return "manual_remediation_external_signature_missing"

    trusted = _trusted_remediation_signers(config)
    if signer_id not in trusted:
        return "manual_remediation_signer_untrusted"

    if not (source.startswith("https://") or source.startswith("ext://")):
        return "manual_remediation_source_invalid"

    evidence_hash = str(getattr(entry, "remediation_evidence_hash_sha256", "") or "").strip().lower()
    expected = _expected_external_signature(scheme, signer_id, key_instance_id, source, evidence_hash)
    if expected is None:
        return "manual_remediation_signature_scheme_invalid"
    if signature != expected:
        return "manual_remediation_signature_mismatch"

    revocation_error = _manual_remediation_revocation_error(entry, config)
    if revocation_error is not None:
        return revocation_error

    issuer_error = _manual_remediation_issuer_error(entry, config)
    if issuer_error is not None:
        return issuer_error
    return None
def _has_manual_remediation_metadata(entry: JournalEntry) -> bool:
    fields = [
        getattr(entry, "remediation_status", None),
        getattr(entry, "remediation_bundle_id", None),
        getattr(entry, "remediation_evidence_ref", None),
        getattr(entry, "remediation_parent_lineage_tier", None),
        getattr(entry, "remediation_operator", None),
        getattr(entry, "remediation_reason", None),
        getattr(entry, "remediation_timestamp_utc", None),
    ]
    return any(str(v or "").strip() for v in fields)


def _manual_remediation_provenance_error(entry: JournalEntry, config: StrategyConfig) -> str | None:
    if not _has_manual_remediation_metadata(entry):
        return None

    status = str(getattr(entry, "remediation_status", "") or "").strip().lower()
    if status != "manual_recovered":
        return "manual_remediation_invalid_status"

    required_text = {
        "remediation_bundle_id": getattr(entry, "remediation_bundle_id", None),
        "remediation_evidence_ref": getattr(entry, "remediation_evidence_ref", None),
        "remediation_parent_lineage_tier": getattr(entry, "remediation_parent_lineage_tier", None),
        "remediation_operator": getattr(entry, "remediation_operator", None),
        "remediation_reason": getattr(entry, "remediation_reason", None),
        "remediation_evidence_payload": getattr(entry, "remediation_evidence_payload", None),
        "remediation_evidence_hash_sha256": getattr(entry, "remediation_evidence_hash_sha256", None),
    }
    for _, value in required_text.items():
        if not str(value or "").strip():
            return "manual_remediation_provenance_incomplete"

    if not str(getattr(entry, "position_id", "") or "").strip():
        return "manual_remediation_missing_position_id"

    parent_tier = str(getattr(entry, "remediation_parent_lineage_tier", "") or "").strip()
    if parent_tier not in MANUAL_REMEDIATION_ALLOWED_PARENT_TIERS:
        return "manual_remediation_parent_tier_invalid"

    ts = getattr(entry, "remediation_timestamp_utc", None)
    if ts is None:
        return "manual_remediation_provenance_incomplete"

    try:
        _normalized_timestamp_value(ts)
    except Exception:
        return "manual_remediation_timestamp_invalid"

    evidence_payload = str(getattr(entry, "remediation_evidence_payload", "") or "").strip()
    evidence_hash = str(getattr(entry, "remediation_evidence_hash_sha256", "") or "").strip().lower()
    if not _SHA256_HEX_RE.match(evidence_hash):
        return "manual_remediation_attestation_hash_invalid"
    computed_hash = _attested_payload_sha256(evidence_payload)
    if evidence_hash != computed_hash:
        return "manual_remediation_attestation_hash_mismatch"

    evidence_ref = str(getattr(entry, "remediation_evidence_ref", "") or "").strip().lower()
    if evidence_ref.startswith("sha256:") and evidence_ref != f"sha256:{evidence_hash}":
        return "manual_remediation_attestation_ref_mismatch"

    manifest_error = _manual_remediation_manifest_error(entry, config)
    if manifest_error is not None:
        return manifest_error

    retrieval_error = _manual_remediation_retrieval_error(entry, config)
    if retrieval_error is not None:
        return retrieval_error

    external_error = _manual_remediation_external_signature_error(entry, config)
    if external_error is not None:
        return external_error

    return None


def _is_manual_recovered_entry(entry: JournalEntry) -> bool:
    return str(getattr(entry, "remediation_status", "") or "").strip().lower() == "manual_recovered"

def enforce_low_context_legacy_policy(
    rows: list[RealizedRowValidation],
    policy: str,
) -> tuple[list[RealizedRowValidation], list[RealizedRowValidation]]:
    mode = str(policy or "bound_event_only").lower().strip()
    accepted: list[RealizedRowValidation] = []
    rejected: list[RealizedRowValidation] = []

    for row in rows:
        if row.lineage_trust_tier != "low_context_legacy_event_only":
            accepted.append(row)
            continue

        if mode == "reject":
            rejected.append(
                RealizedRowValidation(
                    "rejected_requires_manual_review",
                    "low_context_legacy_row_rejected_by_policy",
                    None,
                    row.entry,
                    "legacy_lineage_rejected",
                )
            )
            continue
        accepted.append(row)

    return order_accepted_realized_rows(accepted), rejected


def _legacy_lineage_position_id(anchor_key: tuple) -> str:
    return "legacy-lineage|" + "|".join(str(v) for v in anchor_key)


def _sparse_lineage_position_id(anchor_key: tuple) -> str:
    return "sparse-lineage|" + "|".join(str(v) for v in anchor_key)


def _legacy_lineage_anchor_key(row: RealizedRowValidation) -> tuple | None:
    e = row.entry
    if str(getattr(e, "position_id", "") or "").strip():
        return None
    return _lineage_anchor_key_any(e, allow_sparse_timestamp_fallback=False)


def _sparse_lineage_anchor_key(row: RealizedRowValidation) -> tuple | None:
    return _sparse_supportable_anchor(row.entry)


def _legacy_lineage_group_is_deterministic(group: list[RealizedRowValidation]) -> bool:
    if not group:
        return False
    base = group[0].entry
    base_size = float(base.executed_size_btc if base.executed_size_btc is not None else base.position_size_btc)
    base_intended = float(base.position_size_btc)
    for row in group[1:]:
        e = row.entry
        size = float(e.executed_size_btc if e.executed_size_btc is not None else e.position_size_btc)
        if not _within_absolute_tolerance(size, base_size, EXECUTION_NOTIONAL_COHERENCE_TOLERANCE_USD):
            return False
        if not _within_absolute_tolerance(float(e.position_size_btc), base_intended, EXECUTION_NOTIONAL_COHERENCE_TOLERANCE_USD):
            return False

    stages = [r.entry.residual_lifecycle_stage for r in group if r.entry.residual_lifecycle_stage is not None]
    if stages and any(stages[idx] >= stages[idx + 1] for idx in range(len(stages) - 1)):
        return False
    return True


def _normalize_lineage_group(
    anchor: tuple,
    group: list[RealizedRowValidation],
    id_factory,
    tier_label: str,
) -> tuple[list[RealizedRowValidation], list[RealizedRowValidation]]:
    accepted: list[RealizedRowValidation] = []
    rejected: list[RealizedRowValidation] = []

    if len(group) == 1:
        group[0].lineage_trust_tier = "legacy_event_only" if tier_label == "legacy_lineage_normalized" else "sparse_event_only_bounded"
        accepted.append(group[0])
        return accepted, rejected

    ordered = sorted(group, key=_accepted_row_order_key)
    effective_size = float(ordered[0].entry.executed_size_btc if ordered[0].entry.executed_size_btc is not None else ordered[0].entry.position_size_btc)

    if not _legacy_lineage_group_is_deterministic(ordered):
        for r in ordered:
            rejected.append(RealizedRowValidation("rejected_requires_manual_review", "ambiguous_legacy_lineage_non_deterministic_group", None, r.entry, "legacy_lineage_rejected"))
        return accepted, rejected

    if any((r.entry.realized_exit_size_btc is None or r.entry.realized_exit_size_btc <= 0) for r in ordered):
        for r in ordered:
            rejected.append(RealizedRowValidation("rejected_requires_manual_review", "ambiguous_legacy_lineage_missing_exit_size", None, r.entry, "legacy_lineage_rejected"))
        return accepted, rejected

    exit_sum = sum(float(r.entry.realized_exit_size_btc) for r in ordered)
    if exit_sum - effective_size > EXECUTION_NOTIONAL_COHERENCE_TOLERANCE_USD:
        for r in ordered:
            rejected.append(RealizedRowValidation("rejected_requires_manual_review", "ambiguous_legacy_lineage_oversized_exit_sum", None, r.entry, "legacy_lineage_rejected"))
        return accepted, rejected

    if any(str(r.entry.position_status).lower() == "closed" for r in ordered[:-1]):
        for r in ordered:
            rejected.append(RealizedRowValidation("rejected_requires_manual_review", "ambiguous_legacy_lineage_premature_close", None, r.entry, "legacy_lineage_rejected"))
        return accepted, rejected

    synthetic_position_id = id_factory(anchor)
    for idx, r in enumerate(ordered):
        if r.entry.position_id and str(r.entry.position_id) != synthetic_position_id:
            rejected.append(RealizedRowValidation("rejected_requires_manual_review", "ambiguous_legacy_lineage_position_identity_conflict", None, r.entry, "legacy_lineage_rejected"))
            continue
        r.entry.position_id = synthetic_position_id
        if r.entry.position_open_timestamp is None:
            r.entry.position_open_timestamp = r.entry.entry_fill_timestamp or r.entry.timestamp
        if idx < len(ordered) - 1 and str(r.entry.position_status).lower() != "open":
            r.entry.position_status = "open"
        if idx == len(ordered) - 1 and str(r.entry.position_status).lower() not in {"closed", "open"}:
            r.entry.position_status = "closed"
        r.lineage_trust_tier = tier_label
        accepted.append(r)

    return accepted, rejected


def normalize_legacy_position_lineage(
    rows: list[RealizedRowValidation],
    sparse_policy: str = "reconstruct_supportable",
    config: StrategyConfig | None = None,
) -> tuple[list[RealizedRowValidation], list[RealizedRowValidation]]:
    grouped: dict[tuple, list[RealizedRowValidation]] = {}
    sparse_grouped: dict[tuple, list[RealizedRowValidation]] = {}
    accepted: list[RealizedRowValidation] = []
    rejected: list[RealizedRowValidation] = []
    sparse_mode = str(sparse_policy or "reconstruct_supportable").lower().strip()
    cfg = config or StrategyConfig()

    for row in rows:
        remediation_error = _manual_remediation_provenance_error(row.entry, cfg)
        if remediation_error is not None:
            rejection_tier = "manual_remediation_rejected"
            if remediation_error in {"manual_remediation_signer_revoked", "manual_remediation_key_instance_revoked", "manual_remediation_signer_untrusted", "manual_remediation_key_instance_not_active", "manual_remediation_issuer_revoked", "manual_remediation_issuer_untrusted", "manual_remediation_issuer_binding_conflict", "manual_remediation_certificate_anchor_untrusted", "manual_remediation_certificate_path_conflict", "manual_remediation_certificate_link_internally_invalid", "manual_remediation_certificate_semantically_invalid", "manual_remediation_certificate_der_tbs_invalid", "manual_remediation_certificate_critical_extension_unsupported", "manual_remediation_manifest_invalid", "manual_remediation_retrieval_invalid", "manual_remediation_retrieval_receipt_invalid", "manual_remediation_retrieval_receipt_attestor_revoked", "manual_remediation_retrieval_receipt_attestor_key_instance_revoked", "manual_remediation_retrieval_receipt_attestor_key_instance_lifecycle_evidence_invalid", "manual_remediation_retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_revoked", "manual_remediation_retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_dataset_invalid", "manual_remediation_retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_anchor_revoked"}:
                rejection_tier = "manual_remediation_revoked"
            if remediation_error in {"manual_remediation_revocation_state_indeterminate", "manual_remediation_revocation_policy_invalid", "manual_remediation_key_lineage_indeterminate", "manual_remediation_key_rotation_policy_invalid", "manual_remediation_key_instance_missing", "manual_remediation_issuer_state_indeterminate", "manual_remediation_issuer_binding_indeterminate", "manual_remediation_issuer_chain_policy_invalid", "manual_remediation_issuer_revocation_policy_invalid", "manual_remediation_issuer_provenance_incomplete", "manual_remediation_certificate_fingerprint_invalid", "manual_remediation_certificate_path_indeterminate", "manual_remediation_certificate_path_invalid", "manual_remediation_certificate_path_depth_invalid", "manual_remediation_certificate_path_policy_invalid", "manual_remediation_certificate_link_internally_indeterminate", "manual_remediation_certificate_link_policy_invalid", "manual_remediation_certificate_semantically_indeterminate", "manual_remediation_certificate_der_tbs_indeterminate", "manual_remediation_manifest_indeterminate", "manual_remediation_manifest_policy_invalid", "manual_remediation_retrieval_indeterminate", "manual_remediation_retrieval_policy_invalid", "manual_remediation_retrieval_receipt_indeterminate", "manual_remediation_retrieval_receipt_attestor_indeterminate", "manual_remediation_retrieval_receipt_attestor_key_instance_indeterminate", "manual_remediation_retrieval_receipt_attestor_key_instance_lifecycle_evidence_indeterminate", "manual_remediation_retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_indeterminate", "manual_remediation_retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_dataset_indeterminate", "manual_remediation_retrieval_receipt_attestor_key_instance_lifecycle_evidence_anchor_key_governance_anchor_indeterminate"}:
                rejection_tier = "manual_remediation_indeterminate"
            rejected.append(
                RealizedRowValidation(
                    "rejected_requires_manual_review",
                    remediation_error,
                    None,
                    row.entry,
                    rejection_tier,
                )
            )
            continue

        if _is_manual_recovered_entry(row.entry):
            policy = str(getattr(cfg, "remediation_external_signature_policy", "require") or "require").lower().strip()
            row.lineage_trust_tier = "manual_remediated_attested_lineage" if policy == "require" or str(getattr(row.entry, "remediation_signature", "") or "").strip() else "manual_remediated_internal_attested_lineage"
            accepted.append(row)
            continue

        if str(getattr(row.entry, "position_id", "") or "").strip():
            row.lineage_trust_tier = "native_position_lineage"
            accepted.append(row)
            continue

        anchor = _legacy_lineage_anchor_key(row)
        if anchor is not None:
            row.lineage_trust_tier = "legacy_event_only"
            has_lifecycle_evidence = row.entry.realized_exit_size_btc is not None and row.entry.residual_lifecycle_stage is not None
            if has_lifecycle_evidence:
                grouped.setdefault(anchor, []).append(row)
            else:
                accepted.append(row)
            continue

        sparse_anchor = _sparse_lineage_anchor_key(row)
        if sparse_anchor is not None:
            has_lifecycle_evidence = row.entry.realized_exit_size_btc is not None and row.entry.residual_lifecycle_stage is not None
            if sparse_mode == "reconstruct_supportable" and has_lifecycle_evidence:
                row.lineage_trust_tier = "sparse_event_only_bounded"
                sparse_grouped.setdefault(sparse_anchor, []).append(row)
            elif sparse_mode in {"reconstruct_supportable", "bound_event_only"}:
                row.lineage_trust_tier = "sparse_event_only_bounded"
                accepted.append(row)
            else:
                rejected.append(
                    RealizedRowValidation(
                        "rejected_requires_manual_review",
                        "sparse_supportable_row_rejected_by_policy",
                        None,
                        row.entry,
                        "legacy_lineage_rejected",
                    )
                )
            continue

        row.lineage_trust_tier = "low_context_legacy_event_only"
        accepted.append(row)

    for anchor, group in sorted(grouped.items(), key=lambda kv: kv[0]):
        a, r = _normalize_lineage_group(anchor, group, _legacy_lineage_position_id, "legacy_lineage_normalized")
        accepted.extend(a)
        rejected.extend(r)

    for anchor, group in sorted(sparse_grouped.items(), key=lambda kv: kv[0]):
        a, r = _normalize_lineage_group(anchor, group, _sparse_lineage_position_id, "sparse_lineage_normalized")
        accepted.extend(a)
        rejected.extend(r)

    return order_accepted_realized_rows(accepted), rejected


def enforce_mixed_lineage_tier_coherence(rows: list[RealizedRowValidation]) -> tuple[list[RealizedRowValidation], list[RealizedRowValidation]]:
    stronger_anchors: set[tuple] = set()
    for row in rows:
        if row.lineage_trust_tier in {
            "native_position_lineage",
            "legacy_lineage_normalized",
            "sparse_lineage_normalized",
            "manual_remediated_attested_lineage",
            "manual_remediated_internal_attested_lineage",
        }:
            anchor = _lineage_anchor_key_any(row.entry)
            if anchor is not None:
                stronger_anchors.add(anchor)

    accepted: list[RealizedRowValidation] = []
    rejected: list[RealizedRowValidation] = []
    for row in rows:
        if row.lineage_trust_tier not in {"legacy_event_only", "sparse_event_only_bounded"}:
            accepted.append(row)
            continue
        anchor = _lineage_anchor_key_any(row.entry)
        if anchor is not None and anchor in stronger_anchors:
            rejected.append(
                RealizedRowValidation(
                    "rejected_requires_manual_review",
                    "ambiguous_mixed_lineage_tier_anchor_conflict",
                    None,
                    row.entry,
                    "legacy_lineage_rejected",
                )
            )
            continue
        accepted.append(row)

    return order_accepted_realized_rows(accepted), rejected


def enforce_manual_remediation_idempotency(rows: list[RealizedRowValidation]) -> tuple[list[RealizedRowValidation], list[RealizedRowValidation]]:
    grouped: dict[str, list[RealizedRowValidation]] = {}
    passthrough: list[RealizedRowValidation] = []
    rejected: list[RealizedRowValidation] = []

    for row in rows:
        if row.lineage_trust_tier not in {"manual_remediated_attested_lineage", "manual_remediated_internal_attested_lineage"}:
            passthrough.append(row)
            continue
        bundle_id = str(getattr(row.entry, "remediation_bundle_id", "") or "").strip()
        if not bundle_id:
            rejected.append(
                RealizedRowValidation(
                    "rejected_requires_manual_review",
                    "manual_remediation_provenance_incomplete",
                    None,
                    row.entry,
                    "manual_remediation_rejected",
                )
            )
            continue
        grouped.setdefault(bundle_id, []).append(row)

    accepted = list(passthrough)
    for _, group in sorted(grouped.items(), key=lambda kv: kv[0]):
        identity_groups: dict[tuple, list[RealizedRowValidation]] = {}
        attestation_hashes: set[str] = set()
        for row in group:
            identity_groups.setdefault(_full_persisted_identity_key(row), []).append(row)
            attestation_hashes.add("|".join([
                str(getattr(row.entry, "remediation_evidence_hash_sha256", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_evidence_manifest_json", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_evidence_manifest_hash_sha256", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_evidence_retrieval_proofs_json", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_evidence_retrieval_proofs_hash_sha256", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_json", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_hash_sha256", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_anchor_key_instance_id", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_json", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_hash_sha256", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_json", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_hash_sha256", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_json", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_hash_sha256", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_signer_id", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_key_instance_id", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_issuer_id", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_certificate_chain_id", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_certificate_fingerprint_sha256", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_certificate_path_fingerprints", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_certificate_path_anchor_fingerprint_sha256", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_certificate_objects", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_certificate_der_hex_chain", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_external_source", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_signature", "") or "").strip().lower(),
                str(getattr(row.entry, "remediation_signature_scheme", "") or "").strip().lower(),
            ]))
        if len(attestation_hashes) != 1:
            for row in group:
                rejected.append(
                    RealizedRowValidation(
                        "rejected_requires_manual_review",
                        "manual_remediation_attestation_conflict",
                        None,
                        row.entry,
                        "manual_remediation_rejected",
                    )
                )
            continue
        if len(identity_groups) == 1:
            ordered = sorted(group, key=_accepted_row_order_key)
            accepted.append(ordered[0])
            for duplicate in ordered[1:]:
                rejected.append(
                    RealizedRowValidation(
                        "rejected_requires_manual_review",
                        "duplicate_manual_remediation_bundle",
                        None,
                        duplicate.entry,
                        "manual_remediation_rejected",
                    )
                )
            continue

        for row in group:
            rejected.append(
                RealizedRowValidation(
                    "rejected_requires_manual_review",
                    "ambiguous_manual_remediation_bundle_conflict",
                    None,
                    row.entry,
                    "manual_remediation_rejected",
                )
            )

    return order_accepted_realized_rows(accepted), rejected


def _entry_stable_key(entry: JournalEntry) -> tuple[Any, ...]:
    return (
        str(entry.timestamp),
        str(entry.market_regime),
        str(entry.decision),
        str(entry.entry),
        str(entry.stop),
        str(entry.target),
        str(entry.actual_outcome),
        entry.actual_exit_price,
        entry.actual_pnl_usd,
        str(entry.actual_pnl_basis),
        entry.executed_entry_price,
        entry.executed_notional_usd,
        str(getattr(entry, "remediation_status", "") or ""),
        str(getattr(entry, "remediation_bundle_id", "") or ""),
        str(getattr(entry, "remediation_evidence_ref", "") or ""),
        str(getattr(entry, "remediation_evidence_manifest_json", "") or ""),
        str(getattr(entry, "remediation_evidence_manifest_hash_sha256", "") or ""),
        str(getattr(entry, "remediation_evidence_retrieval_proofs_json", "") or ""),
        str(getattr(entry, "remediation_evidence_retrieval_proofs_hash_sha256", "") or ""),
        str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_json", "") or ""),
        str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_hash_sha256", "") or ""),
        str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_anchor_key_instance_id", "") or ""),
        str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_json", "") or ""),
        str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_hash_sha256", "") or ""),
        str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_json", "") or ""),
        str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_hash_sha256", "") or ""),
        str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_json", "") or ""),
        str(getattr(entry, "remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_hash_sha256", "") or ""),
        str(getattr(entry, "remediation_parent_lineage_tier", "") or ""),
        str(getattr(entry, "remediation_operator", "") or ""),
        str(getattr(entry, "remediation_reason", "") or ""),
        str(getattr(entry, "remediation_timestamp_utc", "") or ""),
        str(getattr(entry, "remediation_evidence_payload", "") or ""),
        str(getattr(entry, "remediation_evidence_hash_sha256", "") or ""),
        str(getattr(entry, "remediation_external_source", "") or ""),
        str(getattr(entry, "remediation_signer_id", "") or ""),
        str(getattr(entry, "remediation_key_instance_id", "") or ""),
        str(getattr(entry, "remediation_key_epoch", "") or ""),
        str(getattr(entry, "remediation_key_serial", "") or ""),
        str(getattr(entry, "remediation_issuer_id", "") or ""),
        str(getattr(entry, "remediation_certificate_chain_id", "") or ""),
        str(getattr(entry, "remediation_certificate_fingerprint_sha256", "") or ""),
        str(getattr(entry, "remediation_certificate_path_fingerprints", "") or ""),
        str(getattr(entry, "remediation_certificate_path_anchor_fingerprint_sha256", "") or ""),
        str(getattr(entry, "remediation_certificate_objects", "") or ""),
        str(getattr(entry, "remediation_certificate_der_hex_chain", "") or ""),
        str(getattr(entry, "remediation_certificate_link_signatures", "") or ""),
        str(getattr(entry, "remediation_certificate_link_signature_scheme", "") or ""),
        str(getattr(entry, "remediation_signature", "") or ""),
        str(getattr(entry, "remediation_signature_scheme", "") or ""),
    )


def migrate_legacy_history(
    input_path: str,
    output_path: str,
    ambiguous_path: str,
    config: StrategyConfig,
) -> LegacyMigrationReport:
    source = Path(input_path)
    out = Path(output_path)
    amb = Path(ambiguous_path)

    if not source.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("", encoding="utf-8")
        amb.parent.mkdir(parents=True, exist_ok=True)
        amb.write_text("", encoding="utf-8")
        return LegacyMigrationReport(0, 0, 0, 0, 0)

    rows: list[dict] = []
    entries: list[JournalEntry] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rows.append(row)
        entries.append(JournalEntry(**row))

    had_position_id_by_obj_id = {id(entry): bool(str(getattr(entry, "position_id", "") or "").strip()) for entry in entries}

    realized_validations: list[RealizedRowValidation] = []
    for entry in entries:
        result = validate_realized_row(entry, config)
        if result.status in {"accepted_net", "accepted_gross_normalized"}:
            realized_validations.append(result)

    normalized, lineage_rejections = normalize_legacy_position_lineage(
        order_accepted_realized_rows(realized_validations),
        sparse_policy=getattr(config, "runtime_sparse_history_policy", "reconstruct_supportable"),
        config=config,
    )
    normalized, mixed_rejections = enforce_mixed_lineage_tier_coherence(normalized)
    normalized, remediation_rejections = enforce_manual_remediation_idempotency(normalized)
    normalized, low_context_rejections = enforce_low_context_legacy_policy(
        normalized,
        getattr(config, "runtime_low_context_legacy_policy", "bound_event_only"),
    )
    lineage_rejected = lineage_rejections + mixed_rejections + remediation_rejections + low_context_rejections

    normalized_by_key: dict[tuple[Any, ...], RealizedRowValidation] = {}
    for row in normalized:
        key = _entry_stable_key(row.entry)
        normalized_by_key.setdefault(key, row)

    upgraded = 0
    migrated_entries: list[JournalEntry] = []
    for entry in entries:
        key = _entry_stable_key(entry)
        normalized_row = normalized_by_key.get(key)
        if normalized_row is None:
            migrated_entries.append(entry)
            continue

        upgraded_entry = normalized_row.entry
        if not had_position_id_by_obj_id.get(id(entry), False) and str(getattr(upgraded_entry, "position_id", "") or "").strip():
            upgraded += 1
        migrated_entries.append(upgraded_entry)

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for entry in migrated_entries:
            f.write(json.dumps(asdict(entry), default=str) + "\n")

    amb.parent.mkdir(parents=True, exist_ok=True)
    with amb.open("w", encoding="utf-8") as f:
        for rejected in lineage_rejected:
            f.write(
                json.dumps(
                    {
                        "reason": rejected.reason,
                        "lineage_trust_tier": rejected.lineage_trust_tier,
                        "entry": asdict(rejected.entry),
                    },
                    default=str,
                )
                + "\n"
            )

    low_context_bounded_rows = len([r for r in normalized if r.lineage_trust_tier == "low_context_legacy_event_only"])
    low_context_rejected_rows = len([r for r in lineage_rejected if r.reason in {"low_context_legacy_row_rejected_by_policy", "sparse_supportable_row_rejected_by_policy"}])
    sparse_reconstructed_rows = len([r for r in normalized if r.lineage_trust_tier == "sparse_lineage_normalized"])
    sparse_bounded_rows = len([r for r in normalized if r.lineage_trust_tier == "sparse_event_only_bounded"])

    return LegacyMigrationReport(
        total_rows=len(entries),
        realized_rows=len(realized_validations),
        upgraded_rows=upgraded,
        ambiguous_rows=len(lineage_rejected),
        unchanged_rows=max(0, len(entries) - upgraded),
        low_context_bounded_rows=low_context_bounded_rows,
        low_context_rejected_rows=low_context_rejected_rows,
        sparse_reconstructed_rows=sparse_reconstructed_rows,
        sparse_bounded_rows=sparse_bounded_rows,
    )



def _accepted_duplicate_event_key(row: RealizedRowValidation) -> tuple:
    e = row.entry
    return (
        _normalized_timestamp_value(e.timestamp),
        str(e.decision),
        str(e.actual_outcome),
        float(e.position_size_btc),
        float(e.executed_entry_price),
        float(e.executed_notional_usd),
        float(e.actual_exit_price),
        str(e.entry),
        str(e.stop),
        str(e.target),
    )


def _full_persisted_identity_key(row: RealizedRowValidation) -> tuple:
    e = row.entry
    return (
        str(e.timestamp),
        str(e.market_regime),
        str(e.decision),
        str(e.entry),
        str(e.stop),
        str(e.target),
        e.projected_rr,
        e.confidence,
        str(e.reasoning),
        str(e.what_would_have_made_this_stand_aside),
        e.position_size_btc,
        e.intended_position_size_btc,
        e.executed_size_btc,
        e.unfilled_size_btc,
        e.entry_fill_fraction,
        e.target_exit_size_btc,
        e.stop_exit_size_btc,
        e.residual_position_size_btc,
        e.realized_exit_size_btc,
        e.exit_fill_count,
        e.residual_lifecycle_stage,
        e.residual_last_event_ts,
        e.entry_fill_timestamp,
        e.realized_gross_so_far_usd,
        e.position_id,
        e.position_open_timestamp,
        e.position_status,
        e.position_close_timestamp,
        e.executed_entry_price,
        e.executed_notional_usd,
        str(e.actual_outcome),
        e.actual_exit_price,
        e.actual_pnl_usd,
        str(e.actual_pnl_basis),
        e.actual_pnl_pct,
        e.held_duration_hours,
        e.remediation_status,
        e.remediation_bundle_id,
        e.remediation_evidence_ref,
        e.remediation_evidence_manifest_json,
        e.remediation_evidence_manifest_hash_sha256,
        e.remediation_evidence_retrieval_proofs_json,
        e.remediation_evidence_retrieval_proofs_hash_sha256,
        e.remediation_retrieval_receipt_attestor_key_lifecycle_evidence_json,
        e.remediation_retrieval_receipt_attestor_key_lifecycle_evidence_hash_sha256,
        e.remediation_retrieval_receipt_attestor_key_lifecycle_evidence_anchor_key_instance_id,
        e.remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_json,
        e.remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_hash_sha256,
        e.remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_json,
        e.remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_hash_sha256,
        e.remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_json,
        e.remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_hash_sha256,
        e.remediation_parent_lineage_tier,
        e.remediation_operator,
        e.remediation_reason,
        e.remediation_timestamp_utc,
        e.remediation_evidence_payload,
        e.remediation_evidence_hash_sha256,
        e.remediation_external_source,
        e.remediation_signer_id,
        e.remediation_key_instance_id,
        e.remediation_key_epoch,
        e.remediation_key_serial,
        e.remediation_issuer_id,
        e.remediation_certificate_chain_id,
        e.remediation_certificate_fingerprint_sha256,
        e.remediation_certificate_path_fingerprints,
        e.remediation_certificate_path_anchor_fingerprint_sha256,
        e.remediation_signature,
        e.remediation_signature_scheme,
        str(e.review_note),
    )


def deduplicate_accepted_realized_rows(rows: list[RealizedRowValidation]) -> tuple[list[RealizedRowValidation], list[RealizedRowValidation]]:
    grouped: dict[tuple, list[RealizedRowValidation]] = {}
    for row in rows:
        grouped.setdefault(_accepted_duplicate_event_key(row), []).append(row)

    unique: list[RealizedRowValidation] = []
    duplicates: list[RealizedRowValidation] = []

    for _, group in sorted(grouped.items(), key=lambda kv: kv[0]):
        if len(group) == 1:
            unique.append(group[0])
            continue

        identity_groups: dict[tuple, list[RealizedRowValidation]] = {}
        for row in group:
            identity_groups.setdefault(_full_persisted_identity_key(row), []).append(row)

        if len(identity_groups) == 1:
            canonical = sorted(group, key=_accepted_row_order_key)[0]
            unique.append(canonical)
            for row in group:
                if row is canonical:
                    continue
                duplicates.append(
                    RealizedRowValidation(
                        "rejected_requires_manual_review",
                        "duplicate_realized_event",
                        None,
                        row.entry,
                    )
                )
            continue

        for row in group:
            duplicates.append(
                RealizedRowValidation(
                    "rejected_requires_manual_review",
                    "ambiguous_duplicate_realized_event",
                    None,
                    row.entry,
                )
            )

    return unique, duplicates

def _accepted_identity_sufficiency_key(row: RealizedRowValidation) -> tuple:
    e = row.entry
    return (
        _normalized_timestamp_value(e.timestamp),
        str(e.decision),
        float(e.executed_entry_price),
        float(e.actual_exit_price),
        float(e.position_size_btc),
        str(e.actual_outcome),
        str(e.actual_pnl_basis),
        float(e.executed_notional_usd),
    )


def enforce_accepted_identity_sufficiency(rows: list[RealizedRowValidation]) -> tuple[list[RealizedRowValidation], list[RealizedRowValidation]]:
    grouped: dict[tuple, list[RealizedRowValidation]] = {}
    for row in rows:
        grouped.setdefault(_accepted_identity_sufficiency_key(row), []).append(row)

    accepted: list[RealizedRowValidation] = []
    rejected: list[RealizedRowValidation] = []

    for _, group in sorted(grouped.items(), key=lambda kv: kv[0]):
        if len(group) == 1:
            accepted.append(group[0])
            continue

        for row in group:
            rejected.append(
                RealizedRowValidation(
                    "rejected_requires_manual_review",
                    "insufficient_identity_fields",
                    None,
                    row.entry,
                )
            )

    return accepted, rejected


def load_validated_realized_rows(path: str, config: StrategyConfig) -> tuple[list[RealizedRowValidation], list[RealizedRowValidation]]:
    p = Path(path)
    if not p.exists():
        return [], []

    accepted: list[RealizedRowValidation] = []
    rejected: list[RealizedRowValidation] = []

    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        entry = JournalEntry(**row)
        result = validate_realized_row(entry, config)
        if result.status in {"accepted_net", "accepted_gross_normalized"}:
            accepted.append(result)
        elif is_realized_row(entry):
            rejected.append(result)

    accepted = order_accepted_realized_rows(accepted)
    accepted, lineage_rejections = normalize_legacy_position_lineage(
        accepted,
        sparse_policy=getattr(config, "runtime_sparse_history_policy", "reconstruct_supportable"),
        config=config,
    )
    accepted, mixed_tier_rejections = enforce_mixed_lineage_tier_coherence(accepted)
    accepted, remediation_rejections = enforce_manual_remediation_idempotency(accepted)
    accepted, low_context_rejections = enforce_low_context_legacy_policy(
        accepted,
        getattr(config, "runtime_low_context_legacy_policy", "bound_event_only"),
    )
    accepted, duplicate_rejections = deduplicate_accepted_realized_rows(accepted)
    accepted, identity_rejections = enforce_accepted_identity_sufficiency(accepted)
    rejected.extend(lineage_rejections)
    rejected.extend(mixed_tier_rejections)
    rejected.extend(low_context_rejections)
    rejected.extend(remediation_rejections)
    rejected.extend(duplicate_rejections)
    rejected.extend(identity_rejections)
    return accepted, rejected


def build_journal_entry(result: DecisionResult) -> JournalEntry:
    if result.hard_rule_failures:
        stand_aside_hint = "; ".join(result.hard_rule_failures)
    elif result.confidence_score < 0.55:
        stand_aside_hint = "Confidence below threshold; wait for clearer trend/structure/volume alignment"
    else:
        stand_aside_hint = "N/A - setup passed hard-rules and score threshold"

    decision = result.final_decision.value
    executed_entry = None
    if result.entry_zone is not None and result.final_decision in (FinalDecision.PAPER_LONG, FinalDecision.PAPER_SHORT):
        executed_entry = conservative_entry(result.entry_zone, result.final_decision)

    notional = abs((executed_entry or 0.0) * result.position_size_btc)

    return JournalEntry(
        timestamp=result.timestamp,
        market_regime=result.market_regime.value,
        decision=decision,
        entry=str(result.entry_zone),
        stop=str(result.stop_loss),
        target=str(result.target),
        projected_rr=result.risk_reward_ratio,
        confidence=round(result.confidence_score, 3),
        reasoning=" | ".join(result.reasoning),
        what_would_have_made_this_stand_aside=stand_aside_hint,
        position_size_btc=result.position_size_btc,
        intended_position_size_btc=result.position_size_btc,
        executed_size_btc=result.position_size_btc if executed_entry is not None and result.position_size_btc > 0 else None,
        unfilled_size_btc=0.0 if executed_entry is not None and result.position_size_btc > 0 else None,
        entry_fill_fraction=1.0 if executed_entry is not None and result.position_size_btc > 0 else None,
        target_exit_size_btc=None,
        stop_exit_size_btc=None,
        residual_position_size_btc=result.position_size_btc if executed_entry is not None and result.position_size_btc > 0 else None,
        realized_exit_size_btc=0.0 if executed_entry is not None and result.position_size_btc > 0 else None,
        exit_fill_count=0 if executed_entry is not None and result.position_size_btc > 0 else None,
        residual_lifecycle_stage=0 if executed_entry is not None and result.position_size_btc > 0 else None,
        residual_last_event_ts=None,
        entry_fill_timestamp=result.timestamp if executed_entry is not None and result.position_size_btc > 0 else None,
        realized_gross_so_far_usd=0.0 if executed_entry is not None and result.position_size_btc > 0 else None,
        position_id=build_position_id(result.timestamp, decision, str(result.entry_zone)),
        position_open_timestamp=result.timestamp if executed_entry is not None and result.position_size_btc > 0 else None,
        position_status="open" if executed_entry is not None and result.position_size_btc > 0 else None,
        position_close_timestamp=None,
        executed_entry_price=executed_entry,
        executed_notional_usd=notional if notional > 0 else None,
    )


def append_journal_jsonl(entry: JournalEntry, path: str = "journal/journal.jsonl") -> Path:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(entry), default=str) + "\n")
    return out_path
