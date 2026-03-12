import hashlib
import json
from datetime import datetime

from btc_rinse_repeat_v1.config import StrategyConfig
from btc_rinse_repeat_v1.review import review_journal


def _attested_hash(payload: str) -> str:
    canonical = json.dumps(json.loads(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _external_sig(signer_id: str, key_instance_id: str, source: str, evidence_hash: str, scheme: str = "sha256_signer_key_bind_v1") -> str:
    message = "|".join([scheme, signer_id.lower(), key_instance_id.lower(), source.lower(), evidence_hash.lower()])
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def test_review_summary_uses_net_pnl_without_double_friction(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    rows = [
        {
            "timestamp": str(datetime.utcnow()),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(1,2)",
            "stop": "0.5",
            "target": "3",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "A",
            "what_would_have_made_this_stand_aside": "none",
            "position_size_btc": 1.0,
            "executed_entry_price": 1.5,
            "executed_notional_usd": 1.5,
            "actual_outcome": "win",
            "actual_exit_price": 3.0,
            "actual_pnl_usd": 1.5,
            "actual_pnl_basis": "net",
            "actual_pnl_pct": 1.0,
            "held_duration_hours": 4.0,
            "review_note": "good",
        },
        {
            "timestamp": str(datetime.utcnow()),
            "market_regime": "Trend Down",
            "decision": "Paper Short",
            "entry": "(1,2)",
            "stop": "2.5",
            "target": "0.5",
            "projected_rr": 1.5,
            "confidence": 0.7,
            "reasoning": "B",
            "what_would_have_made_this_stand_aside": "none",
            "position_size_btc": 1.0,
            "executed_entry_price": 1.5,
            "executed_notional_usd": 1.5,
            "actual_outcome": "loss",
            "actual_exit_price": 2.5,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
            "actual_pnl_pct": -0.5,
            "held_duration_hours": 2.0,
            "review_note": "bad",
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    summary = review_journal(str(path), StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0))
    assert summary["total_decisions"] == 2
    assert summary["decision_counts"]["Paper Long"] == 1
    assert summary["win_rate"] == 50.0
    assert summary["average_realized_pnl_usd"] == 0.25


def test_review_summary_reports_open_residual_exposure(tmp_path) -> None:
    path = tmp_path / "journal_residual.jsonl"
    row = {
        "timestamp": str(datetime.utcnow()),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100,100)",
        "stop": "95",
        "target": "105",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "A",
        "what_would_have_made_this_stand_aside": "none",
        "position_size_btc": 2.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 200.0,
        "actual_outcome": "open",
        "residual_position_size_btc": 1.25,
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    summary = review_journal(str(path), StrategyConfig())
    assert summary["open_residual_rows"] == 1
    assert summary["open_residual_exposure_btc"] == 1.25


def test_review_summary_reports_position_identity_counts(tmp_path) -> None:
    path = tmp_path / "journal_positions.jsonl"
    rows = [
        {
            "timestamp": str(datetime.utcnow()),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(100,100)",
            "stop": "95",
            "target": "105",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "A",
            "what_would_have_made_this_stand_aside": "none",
            "position_size_btc": 1.0,
            "position_id": "pos-1",
            "position_status": "open",
            "residual_position_size_btc": 0.5,
            "executed_entry_price": 100.0,
            "executed_notional_usd": 100.0,
            "actual_outcome": "open",
        },
        {
            "timestamp": str(datetime.utcnow()),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(100,100)",
            "stop": "95",
            "target": "105",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "A",
            "what_would_have_made_this_stand_aside": "none",
            "position_size_btc": 1.0,
            "position_id": "pos-2",
            "position_status": "closed",
            "residual_position_size_btc": 0.0,
            "executed_entry_price": 100.0,
            "executed_notional_usd": 100.0,
            "actual_outcome": "win",
            "actual_exit_price": 105.0,
            "actual_pnl_usd": 5.0,
            "actual_pnl_basis": "net",
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    summary = review_journal(str(path), StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0))
    assert summary["unique_position_count"] == 2
    assert summary["open_position_count"] == 1
    assert summary["closed_position_count"] == 1


def test_review_summary_reports_legacy_row_tier_metrics(tmp_path) -> None:
    path = tmp_path / "journal_legacy_mix.jsonl"
    rows = [
        {
            "timestamp": str(datetime.utcnow()),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(100,100)",
            "stop": "95",
            "target": "105",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "A",
            "what_would_have_made_this_stand_aside": "none",
            "position_size_btc": 1.0,
            "executed_entry_price": 100.0,
            "executed_notional_usd": 100.0,
            "actual_outcome": "win",
            "actual_exit_price": 105.0,
            "actual_pnl_usd": 5.0,
            "actual_pnl_basis": "net",
        },
        {
            "timestamp": str(datetime.utcnow()),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(100,100)",
            "stop": "95",
            "target": "105",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "A",
            "what_would_have_made_this_stand_aside": "none",
            "position_size_btc": 1.0,
            "position_id": "pos-1",
            "position_status": "open",
            "residual_position_size_btc": 0.5,
            "executed_entry_price": 100.0,
            "executed_notional_usd": 100.0,
            "actual_outcome": "open",
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    summary = review_journal(str(path), StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0))
    assert summary["legacy_realized_rows"] == 1
    assert summary["legacy_open_rows"] == 0


def test_review_summary_reports_manual_remediated_lineage_rows(tmp_path) -> None:
    path = tmp_path / "journal_manual_remediated.jsonl"
    ts = datetime.utcnow()
    row = {
        "timestamp": str(ts),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100,100)",
        "stop": "95",
        "target": "105",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual",
        "what_would_have_made_this_stand_aside": "none",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-review",
        "position_status": "closed",
        "position_open_timestamp": str(ts),
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
        "actual_outcome": "loss",
        "actual_exit_price": 99.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-review",
        "remediation_evidence_ref": "sha256:" + _attested_hash("{\"ticket\":66,\"proof\":\"review\"}"),
        "remediation_evidence_payload": "{\"ticket\":66,\"proof\":\"review\"}",
        "remediation_evidence_hash_sha256": _attested_hash("{\"ticket\":66,\"proof\":\"review\"}"),
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": str(ts),
        "remediation_external_source": "ext://review-proof",
        "remediation_signer_id": "ops-ledger",
        "remediation_key_instance_id": "ops-ledger:key-1",
        "remediation_key_epoch": 1,
        "remediation_key_serial": "k1",
        "remediation_issuer_id": "ops-root-ca",
        "remediation_certificate_chain_id": "ops-chain-v1",
        "remediation_certificate_fingerprint_sha256": "a" * 64,
        "remediation_certificate_path_fingerprints": json.dumps(["a" * 64, "e" * 64, "f" * 64]),
        "remediation_certificate_path_anchor_fingerprint_sha256": "f" * 64,
        "remediation_signature_scheme": "sha256_signer_key_bind_v1",
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://review-proof", _attested_hash("{\"ticket\":66,\"proof\":\"review\"}")),
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    summary = review_journal(str(path), StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0))
    assert summary["manual_remediated_attested_lineage_rows"] == 1


def test_review_summary_excludes_invalid_attestation_remediation(tmp_path) -> None:
    path = tmp_path / "journal_manual_invalid_attestation.jsonl"
    ts = datetime.utcnow()
    row = {
        "timestamp": str(ts),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100,100)",
        "stop": "95",
        "target": "105",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual",
        "what_would_have_made_this_stand_aside": "none",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-review-bad",
        "position_status": "closed",
        "position_open_timestamp": str(ts),
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
        "actual_outcome": "loss",
        "actual_exit_price": 99.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-review-bad",
        "remediation_evidence_ref": "sha256:" + ("0" * 64),
        "remediation_evidence_payload": '{"ticket":99,"proof":"tampered"}',
        "remediation_evidence_hash_sha256": "0" * 64,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": str(ts),
        "remediation_external_source": "ext://review-proof",
        "remediation_signer_id": "ops-ledger",
        "remediation_key_instance_id": "ops-ledger:key-1",
        "remediation_key_epoch": 1,
        "remediation_key_serial": "k1",
        "remediation_issuer_id": "ops-root-ca",
        "remediation_certificate_chain_id": "ops-chain-v1",
        "remediation_certificate_fingerprint_sha256": "a" * 64,
        "remediation_certificate_path_fingerprints": json.dumps(["a" * 64, "e" * 64, "f" * 64]),
        "remediation_certificate_path_anchor_fingerprint_sha256": "f" * 64,
        "remediation_signature_scheme": "sha256_signer_key_bind_v1",
        "remediation_signature": "0" * 64,
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    summary = review_journal(str(path), StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0))
    assert summary["manual_remediated_attested_lineage_rows"] == 0
    assert summary["rejected_realized_rows"] == 1


def test_review_summary_reports_internal_attested_lineage_when_policy_allows(tmp_path) -> None:
    path = tmp_path / "journal_manual_internal_allowed.jsonl"
    ts = datetime.utcnow()
    payload = '{"ticket":122,"proof":"review"}'
    ev_hash = _attested_hash(payload)
    row = {
        "timestamp": str(ts),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100,100)",
        "stop": "95",
        "target": "105",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual",
        "what_would_have_made_this_stand_aside": "none",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-review-int",
        "position_status": "closed",
        "position_open_timestamp": str(ts),
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
        "actual_outcome": "loss",
        "actual_exit_price": 99.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-review-int",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": str(ts),
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    summary = review_journal(str(path), StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0, remediation_external_signature_policy="allow_internal_attestation"))
    assert summary["manual_remediated_internal_attested_lineage_rows"] == 1


def test_review_summary_reports_revoked_and_indeterminate_remediation_rows(tmp_path) -> None:
    path = tmp_path / "journal_manual_revocation_mix.jsonl"
    ts = datetime.utcnow()
    payload = '{"ticket":401,"proof":"review"}'
    ev_hash = _attested_hash(payload)
    base = {
        "timestamp": str(ts),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100,100)",
        "stop": "95",
        "target": "105",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual",
        "what_would_have_made_this_stand_aside": "none",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-review-revoke",
        "position_status": "closed",
        "position_open_timestamp": str(ts),
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
        "actual_outcome": "loss",
        "actual_exit_price": 99.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-review-revoke",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": str(ts),
        "remediation_external_source": "ext://review-proof",
        "remediation_signer_id": "ops-ledger",
        "remediation_key_instance_id": "ops-ledger:key-1",
        "remediation_key_epoch": 1,
        "remediation_key_serial": "k1",
        "remediation_issuer_id": "ops-root-ca",
        "remediation_certificate_chain_id": "ops-chain-v1",
        "remediation_certificate_fingerprint_sha256": "a" * 64,
        "remediation_certificate_path_fingerprints": json.dumps(["a" * 64, "e" * 64, "f" * 64]),
        "remediation_certificate_path_anchor_fingerprint_sha256": "f" * 64,
        "remediation_signature_scheme": "sha256_signer_key_bind_v1",
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://review-proof", ev_hash),
    }
    path.write_text(json.dumps(base) + "\n", encoding="utf-8")

    summary_revoked = review_journal(
        str(path),
        StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0, remediation_revoked_signer_ids=("ops-ledger",)),
    )
    assert summary_revoked["manual_remediation_revoked_rows"] == 1

    summary_indeterminate = review_journal(
        str(path),
        StrategyConfig(
            taker_fee_rate=0.0,
            slippage_rate=0.0,
            remediation_revoked_signer_ids=("ops-ledger",),
            remediation_revocation_policy="allow_pre_revocation_only",
        ),
    )
    assert summary_indeterminate["manual_remediation_indeterminate_rows"] == 1

    summary_issuer_revoked = review_journal(
        str(path),
        StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0, remediation_revoked_issuer_ids=("ops-root-ca",)),
    )
    assert summary_issuer_revoked["manual_remediation_revoked_rows"] == 1
