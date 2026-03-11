import hashlib
import json
import subprocess
from datetime import datetime, timezone

from btc_rinse_repeat_v1.config import StrategyConfig
from btc_rinse_repeat_v1.journal import load_validated_realized_rows, migrate_legacy_history
from btc_rinse_repeat_v1.main import _apply_realized_outcomes_to_kill_switch
from btc_rinse_repeat_v1.models import KillSwitchState
from btc_rinse_repeat_v1.review import review_journal




def _attested_hash(payload: str) -> str:
    canonical = json.dumps(json.loads(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _external_sig(signer_id: str, key_instance_id: str, source: str, evidence_hash: str, scheme: str = "sha256_signer_key_bind_v1") -> str:
    message = "|".join([scheme, signer_id.lower(), key_instance_id.lower(), source.lower(), evidence_hash.lower()])
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _write_rows(path, rows):
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_offline_migration_normalizes_deterministic_legacy_lineage(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    ts = datetime(2026, 7, 1, tzinfo=timezone.utc)
    source = tmp_path / "legacy.jsonl"
    migrated = tmp_path / "migrated.jsonl"
    ambiguous = tmp_path / "ambiguous.jsonl"

    rows = [
        {
            "timestamp": ts.isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(100,100)",
            "stop": "95",
            "target": "105",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "a",
            "what_would_have_made_this_stand_aside": "a",
            "position_size_btc": 1.0,
            "executed_size_btc": 1.0,
            "entry_fill_timestamp": ts.isoformat(),
            "realized_exit_size_btc": 0.5,
            "residual_lifecycle_stage": 1,
            "position_status": "open",
            "executed_entry_price": 100.0,
            "executed_notional_usd": 100.0,
            "actual_outcome": "loss",
            "actual_exit_price": 99.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
        },
        {
            "timestamp": ts.replace(hour=1).isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(100,100)",
            "stop": "95",
            "target": "105",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "b",
            "what_would_have_made_this_stand_aside": "b",
            "position_size_btc": 1.0,
            "executed_size_btc": 1.0,
            "entry_fill_timestamp": ts.isoformat(),
            "realized_exit_size_btc": 0.5,
            "residual_lifecycle_stage": 2,
            "position_status": "closed",
            "executed_entry_price": 100.0,
            "executed_notional_usd": 100.0,
            "actual_outcome": "loss",
            "actual_exit_price": 99.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
        },
    ]
    _write_rows(source, rows)

    report = migrate_legacy_history(str(source), str(migrated), str(ambiguous), cfg)
    assert report.upgraded_rows == 2
    assert report.ambiguous_rows == 0

    accepted, rejected = load_validated_realized_rows(str(migrated), cfg)
    assert not rejected
    assert len({r.entry.position_id for r in accepted}) == 1
    assert all(str(r.entry.position_id).startswith("legacy-lineage|") for r in accepted)


def test_offline_migration_isolates_ambiguous_history_and_is_idempotent(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    ts = datetime(2026, 7, 2, tzinfo=timezone.utc)
    source = tmp_path / "legacy_ambig.jsonl"
    migrated = tmp_path / "migrated_ambig.jsonl"
    ambiguous = tmp_path / "ambiguous_ambig.jsonl"

    rows = [
        {
            "timestamp": ts.isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(200,200)",
            "stop": "190",
            "target": "210",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "a",
            "what_would_have_made_this_stand_aside": "a",
            "position_size_btc": 1.0,
            "executed_size_btc": 1.0,
            "entry_fill_timestamp": ts.isoformat(),
            "realized_exit_size_btc": 0.5,
            "residual_lifecycle_stage": 3,
            "position_status": "open",
            "executed_entry_price": 200.0,
            "executed_notional_usd": 200.0,
            "actual_outcome": "loss",
            "actual_exit_price": 199.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
        },
        {
            "timestamp": ts.replace(hour=1).isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(200,200)",
            "stop": "190",
            "target": "210",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "b",
            "what_would_have_made_this_stand_aside": "b",
            "position_size_btc": 1.0,
            "executed_size_btc": 1.0,
            "entry_fill_timestamp": ts.isoformat(),
            "realized_exit_size_btc": 0.5,
            "residual_lifecycle_stage": 2,
            "position_status": "closed",
            "executed_entry_price": 200.0,
            "executed_notional_usd": 200.0,
            "actual_outcome": "loss",
            "actual_exit_price": 199.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
        },
    ]
    _write_rows(source, rows)

    report1 = migrate_legacy_history(str(source), str(migrated), str(ambiguous), cfg)
    assert report1.ambiguous_rows == 2

    first_output = migrated.read_text(encoding="utf-8")
    first_ambiguous = ambiguous.read_text(encoding="utf-8")

    report2 = migrate_legacy_history(str(migrated), str(migrated), str(ambiguous), cfg)
    assert report2.upgraded_rows == 0
    assert report2.ambiguous_rows == 2
    assert migrated.read_text(encoding="utf-8") == first_output
    assert ambiguous.read_text(encoding="utf-8") == first_ambiguous


def test_migrated_mixed_dataset_stays_coherent_across_runtime_and_review(tmp_path) -> None:
    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        runtime_legacy_row_policy="reject",
        runtime_low_context_legacy_policy="reject",
        runtime_sparse_history_policy="reject",
        kill_switch_consecutive_losses=10,
    )
    ts = datetime(2026, 7, 3, tzinfo=timezone.utc)
    source = tmp_path / "mixed_source.jsonl"
    migrated = tmp_path / "mixed_migrated.jsonl"
    ambiguous = tmp_path / "mixed_ambiguous.jsonl"

    rows = [
        {
            "timestamp": ts.isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(50,50)",
            "stop": "45",
            "target": "55",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "native",
            "what_would_have_made_this_stand_aside": "n",
            "position_size_btc": 1.0,
            "position_id": "pos-native",
            "position_status": "closed",
            "executed_entry_price": 50.0,
            "executed_notional_usd": 50.0,
            "actual_outcome": "loss",
            "actual_exit_price": 49.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
        },
        {
            "timestamp": ts.replace(hour=1).isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(60,60)",
            "stop": "55",
            "target": "65",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "legacy-a",
            "what_would_have_made_this_stand_aside": "a",
            "position_size_btc": 1.0,
            "executed_size_btc": 1.0,
            "entry_fill_timestamp": ts.replace(hour=1).isoformat(),
            "realized_exit_size_btc": 0.5,
            "residual_lifecycle_stage": 1,
            "position_status": "open",
            "executed_entry_price": 60.0,
            "executed_notional_usd": 60.0,
            "actual_outcome": "loss",
            "actual_exit_price": 59.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
        },
        {
            "timestamp": ts.replace(hour=2).isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(60,60)",
            "stop": "55",
            "target": "65",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "legacy-b",
            "what_would_have_made_this_stand_aside": "b",
            "position_size_btc": 1.0,
            "executed_size_btc": 1.0,
            "entry_fill_timestamp": ts.replace(hour=1).isoformat(),
            "realized_exit_size_btc": 0.5,
            "residual_lifecycle_stage": 2,
            "position_status": "closed",
            "executed_entry_price": 60.0,
            "executed_notional_usd": 60.0,
            "actual_outcome": "loss",
            "actual_exit_price": 59.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
        },
        {
            "timestamp": ts.replace(hour=3).isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(70,70)",
            "stop": "65",
            "target": "75",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "event-only",
            "what_would_have_made_this_stand_aside": "e",
            "position_size_btc": 1.0,
            "executed_entry_price": 70.0,
            "executed_notional_usd": 70.0,
            "actual_outcome": "loss",
            "actual_exit_price": 69.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
        },
    ]
    _write_rows(source, rows)

    report = migrate_legacy_history(str(source), str(migrated), str(ambiguous), cfg)
    assert report.upgraded_rows == 2

    accepted, _ = load_validated_realized_rows(str(migrated), cfg)
    ks = KillSwitchState()
    ks, _ = _apply_realized_outcomes_to_kill_switch(
        ks,
        None,
        accepted,
        cfg,
        processed_closed_position_ids=set(),
        processed_legacy_event_keys=set(),
    )
    # native closed + normalized closed apply; normalized open + event-only(policy reject) are excluded
    assert ks.consecutive_losses == 2

    summary = review_journal(str(migrated), cfg)
    assert summary["runtime_legacy_row_policy"] == "reject"
    assert summary["legacy_lineage_normalized_rows"] == 0
    assert summary["native_position_lineage_rows"] >= 3
    assert summary["replay_excluded_legacy_event_only_rows"] == 0
    assert summary["sparse_event_only_bounded_rows"] == 0
    assert summary["replay_excluded_sparse_bounded_rows"] == 0


def test_low_context_policy_reject_isolated_offline_and_runtime_review_aligned(tmp_path) -> None:
    ts = datetime(2026, 7, 4, tzinfo=timezone.utc)
    source = tmp_path / "low_context_source.jsonl"
    migrated = tmp_path / "low_context_migrated.jsonl"
    ambiguous = tmp_path / "low_context_ambiguous.jsonl"
    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        runtime_legacy_row_policy="upgrade_deterministic",
        runtime_low_context_legacy_policy="reject",
        runtime_sparse_history_policy="reject",
        kill_switch_consecutive_losses=10,
    )

    rows = [
        {
            "timestamp": ts.isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(80,80)",
            "stop": "75",
            "target": "85",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "legacy-low-context",
            "what_would_have_made_this_stand_aside": "l",
            "position_size_btc": 1.0,
            "executed_entry_price": 80.0,
            "executed_notional_usd": 80.0,
            "actual_outcome": "loss",
            "actual_exit_price": 79.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
        },
        {
            "timestamp": ts.replace(hour=1).isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(90,90)",
            "stop": "85",
            "target": "95",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "native",
            "what_would_have_made_this_stand_aside": "n",
            "position_size_btc": 1.0,
            "position_id": "pos-native",
            "position_status": "closed",
            "executed_entry_price": 90.0,
            "executed_notional_usd": 90.0,
            "actual_outcome": "loss",
            "actual_exit_price": 89.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
        },
    ]
    _write_rows(source, rows)

    report = migrate_legacy_history(str(source), str(migrated), str(ambiguous), cfg)
    assert report.low_context_rejected_rows == 1
    assert report.low_context_bounded_rows == 0
    assert report.sparse_bounded_rows == 0
    assert report.sparse_reconstructed_rows == 0

    accepted, rejected = load_validated_realized_rows(str(migrated), cfg)
    assert len([r for r in accepted if r.lineage_trust_tier == "sparse_event_only_bounded"]) == 0
    assert len([r for r in rejected if r.reason == "sparse_supportable_row_rejected_by_policy"]) == 1

    ks = KillSwitchState()
    ks, _ = _apply_realized_outcomes_to_kill_switch(
        ks,
        None,
        accepted,
        cfg,
        processed_closed_position_ids=set(),
        processed_legacy_event_keys=set(),
    )
    assert ks.consecutive_losses == 1

    summary = review_journal(str(migrated), cfg)
    assert summary["runtime_sparse_history_policy"] == "reject"
    assert summary["replay_excluded_low_context_rows"] == 1
    assert summary["replay_excluded_sparse_bounded_rows"] == 0


def test_low_context_policy_bound_keeps_row_explicitly_bounded(tmp_path) -> None:
    ts = datetime(2026, 7, 5, tzinfo=timezone.utc)
    source = tmp_path / "low_context_bound_source.jsonl"
    migrated = tmp_path / "low_context_bound_migrated.jsonl"
    ambiguous = tmp_path / "low_context_bound_ambiguous.jsonl"
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0, runtime_low_context_legacy_policy="bound_event_only")

    rows = [
        {
            "timestamp": ts.isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(120,120)",
            "stop": "115",
            "target": "125",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "low-context",
            "what_would_have_made_this_stand_aside": "l",
            "position_size_btc": 1.0,
            "executed_entry_price": 120.0,
            "executed_notional_usd": 120.0,
            "actual_outcome": "loss",
            "actual_exit_price": 119.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
        },
    ]
    _write_rows(source, rows)

    report = migrate_legacy_history(str(source), str(migrated), str(ambiguous), cfg)
    assert report.low_context_bounded_rows == 0
    assert report.low_context_rejected_rows == 0
    assert report.sparse_bounded_rows == 1

    accepted, rejected = load_validated_realized_rows(str(migrated), cfg)
    assert not rejected
    assert len(accepted) == 1
    assert accepted[0].lineage_trust_tier == "sparse_event_only_bounded"

    summary = review_journal(str(migrated), cfg)
    assert summary["sparse_event_only_bounded_rows"] == 1
    assert summary["replay_excluded_sparse_bounded_rows"] == 0


def test_sparse_supportable_rows_reconstruct_when_policy_enabled(tmp_path) -> None:
    ts = datetime(2026, 7, 6, tzinfo=timezone.utc)
    source = tmp_path / "sparse_source.jsonl"
    migrated = tmp_path / "sparse_migrated.jsonl"
    ambiguous = tmp_path / "sparse_ambiguous.jsonl"
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0, runtime_sparse_history_policy="reconstruct_supportable")

    rows = [
        {
            "timestamp": ts.isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(130,130)",
            "stop": "125",
            "target": "135",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "s1",
            "what_would_have_made_this_stand_aside": "s1",
            "position_size_btc": 1.0,
            "realized_exit_size_btc": 0.5,
            "residual_lifecycle_stage": 1,
            "position_status": "open",
            "executed_entry_price": 130.0,
            "executed_notional_usd": 130.0,
            "actual_outcome": "loss",
            "actual_exit_price": 129.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
        },
        {
            "timestamp": ts.replace(hour=1).isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(130,130)",
            "stop": "125",
            "target": "135",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "s2",
            "what_would_have_made_this_stand_aside": "s2",
            "position_size_btc": 1.0,
            "realized_exit_size_btc": 0.5,
            "residual_lifecycle_stage": 2,
            "position_status": "closed",
            "executed_entry_price": 130.0,
            "executed_notional_usd": 130.0,
            "actual_outcome": "loss",
            "actual_exit_price": 129.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
        },
    ]
    _write_rows(source, rows)

    report = migrate_legacy_history(str(source), str(migrated), str(ambiguous), cfg)
    assert report.upgraded_rows == 2
    assert report.sparse_reconstructed_rows == 2

    accepted, rejected = load_validated_realized_rows(str(migrated), cfg)
    assert not rejected
    assert len({r.entry.position_id for r in accepted}) == 1
    assert all(str(r.entry.position_id).startswith("sparse-lineage|") for r in accepted)


def test_sparse_supportable_rows_rejected_by_policy(tmp_path) -> None:
    ts = datetime(2026, 7, 7, tzinfo=timezone.utc)
    source = tmp_path / "sparse_reject_source.jsonl"
    migrated = tmp_path / "sparse_reject_migrated.jsonl"
    ambiguous = tmp_path / "sparse_reject_ambig.jsonl"
    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        runtime_sparse_history_policy="reject",
        runtime_low_context_legacy_policy="bound_event_only",
    )
    rows = [
        {
            "timestamp": ts.isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(150,150)",
            "stop": "145",
            "target": "155",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "sr",
            "what_would_have_made_this_stand_aside": "sr",
            "position_size_btc": 1.0,
            "executed_entry_price": 150.0,
            "executed_notional_usd": 150.0,
            "actual_outcome": "loss",
            "actual_exit_price": 149.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
        },
    ]
    _write_rows(source, rows)

    report = migrate_legacy_history(str(source), str(migrated), str(ambiguous), cfg)
    assert report.low_context_rejected_rows == 1

    accepted, rejected = load_validated_realized_rows(str(migrated), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "sparse_supportable_row_rejected_by_policy"]) == 1

    summary = review_journal(str(migrated), cfg)
    assert summary["runtime_sparse_history_policy"] == "reject"
    assert summary["replay_excluded_sparse_bounded_rows"] == 0
    assert summary["rejected_sparse_supportable_rows"] == 1


def test_manual_remediation_requires_complete_provenance(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    path = tmp_path / "manual_missing.jsonl"
    ts = datetime(2026, 8, 2, tzinfo=timezone.utc)
    rows = [
        {
            "timestamp": ts.isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(100,100)",
            "stop": "95",
            "target": "105",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "manual-remediation",
            "what_would_have_made_this_stand_aside": "n/a",
            "position_size_btc": 1.0,
            "position_id": "manual-pos-1",
            "position_status": "closed",
            "executed_entry_price": 100.0,
            "executed_notional_usd": 100.0,
            "actual_outcome": "loss",
            "actual_exit_price": 99.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
            "remediation_status": "manual_recovered",
            "remediation_bundle_id": "bundle-1",
        }
    ]
    _write_rows(path, rows)

    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "manual_remediation_provenance_incomplete"]) == 1


def test_manual_remediation_rows_are_idempotent_and_tiered(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    path = tmp_path / "manual_idempotent.jsonl"
    ts = datetime(2026, 8, 3, tzinfo=timezone.utc)
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(110,110)",
        "stop": "105",
        "target": "115",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-2",
        "position_status": "closed",
        "position_open_timestamp": ts.isoformat(),
        "executed_entry_price": 110.0,
        "executed_notional_usd": 110.0,
        "actual_outcome": "loss",
        "actual_exit_price": 109.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-2",
        "remediation_evidence_ref": "sha256:" + _attested_hash("{\"ticket\":22,\"proof\":\"exchange-export\"}"),
        "remediation_evidence_payload": "{\"ticket\":22,\"proof\":\"exchange-export\"}",
        "remediation_evidence_hash_sha256": _attested_hash("{\"ticket\":22,\"proof\":\"exchange-export\"}"),
        "remediation_parent_lineage_tier": "sparse_event_only_bounded",
        "remediation_operator": "auditor",
        "remediation_reason": "exchange export verified",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
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
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", _attested_hash("{\"ticket\":22,\"proof\":\"exchange-export\"}")),
    }
    _write_rows(path, [row, dict(row)])

    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert len(accepted) == 1
    assert accepted[0].lineage_trust_tier == "manual_remediated_attested_lineage"
    assert len([r for r in rejected if r.reason == "duplicate_manual_remediation_bundle"]) == 1


def test_manual_remediation_rejects_attestation_hash_mismatch(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    path = tmp_path / "manual_hash_mismatch.jsonl"
    ts = datetime(2026, 8, 5, tzinfo=timezone.utc)
    payload = "{\"ticket\":33,\"proof\":\"statement\"}"
    rows = [{
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(120,120)",
        "stop": "115",
        "target": "125",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-3",
        "position_status": "closed",
        "executed_entry_price": 120.0,
        "executed_notional_usd": 120.0,
        "actual_outcome": "loss",
        "actual_exit_price": 119.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-3",
        "remediation_evidence_ref": "sha256:" + _attested_hash(payload),
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": "0" * 64,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://broker-export",
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
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://broker-export", "0" * 64),
    }]
    _write_rows(path, rows)
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "manual_remediation_attestation_hash_mismatch"]) == 1


def test_manual_remediation_bundle_conflict_when_attestation_changes(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    path = tmp_path / "manual_conflict.jsonl"
    ts = datetime(2026, 8, 6, tzinfo=timezone.utc)
    payload_a = "{\"ticket\":44,\"proof\":\"snapshot-a\"}"
    payload_b = "{\"ticket\":44,\"proof\":\"snapshot-b\"}"
    base = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(130,130)",
        "stop": "125",
        "target": "135",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-4",
        "position_status": "closed",
        "executed_entry_price": 130.0,
        "executed_notional_usd": 130.0,
        "actual_outcome": "loss",
        "actual_exit_price": 129.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-4",
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
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
    }
    row_a = dict(base, remediation_evidence_ref="sha256:" + _attested_hash(payload_a), remediation_evidence_payload=payload_a, remediation_evidence_hash_sha256=_attested_hash(payload_a), remediation_signature=_external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", _attested_hash(payload_a)))
    row_b = dict(base, remediation_evidence_ref="sha256:" + _attested_hash(payload_b), remediation_evidence_payload=payload_b, remediation_evidence_hash_sha256=_attested_hash(payload_b), remediation_signature=_external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", _attested_hash(payload_b)))
    _write_rows(path, [row_a, row_b])
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "manual_remediation_attestation_conflict"]) == 2


def test_manual_remediation_rejects_untrusted_signer(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    path = tmp_path / "manual_untrusted_signer.jsonl"
    ts = datetime(2026, 8, 8, tzinfo=timezone.utc)
    payload = '{"ticket":88,"proof":"exchange"}'
    ev_hash = _attested_hash(payload)
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(140,140)",
        "stop": "135",
        "target": "145",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-5",
        "position_status": "closed",
        "executed_entry_price": 140.0,
        "executed_notional_usd": 140.0,
        "actual_outcome": "loss",
        "actual_exit_price": 139.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-5",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
        "remediation_signer_id": "unknown-signer",
        "remediation_key_instance_id": "unknown:key-9",
        "remediation_key_epoch": 9,
        "remediation_key_serial": "k9",
        "remediation_issuer_id": "ops-root-ca",
        "remediation_certificate_chain_id": "ops-chain-v1",
        "remediation_certificate_fingerprint_sha256": "a" * 64,
        "remediation_certificate_path_fingerprints": json.dumps(["a" * 64, "e" * 64, "f" * 64]),
        "remediation_certificate_path_anchor_fingerprint_sha256": "f" * 64,
        "remediation_signature_scheme": "sha256_signer_key_bind_v1",
        "remediation_signature": _external_sig("unknown-signer", "unknown:key-9", "ext://exchange-archive", ev_hash),
    }
    _write_rows(path, [row])
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "manual_remediation_signer_untrusted"]) == 1


def test_manual_remediation_internal_only_allowed_by_policy(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0, remediation_external_signature_policy="allow_internal_attestation")
    path = tmp_path / "manual_internal_allowed.jsonl"
    ts = datetime(2026, 8, 9, tzinfo=timezone.utc)
    payload = '{"ticket":89,"proof":"exchange"}'
    ev_hash = _attested_hash(payload)
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(150,150)",
        "stop": "145",
        "target": "155",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-6",
        "position_status": "closed",
        "executed_entry_price": 150.0,
        "executed_notional_usd": 150.0,
        "actual_outcome": "loss",
        "actual_exit_price": 149.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-6",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
    }
    _write_rows(path, [row])
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert rejected == []
    assert len(accepted) == 1
    assert accepted[0].lineage_trust_tier == "manual_remediated_internal_attested_lineage"


def test_manual_remediation_rejects_revoked_signer_by_default(tmp_path) -> None:
    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_revoked_signer_ids=("ops-ledger",),
    )
    path = tmp_path / "manual_revoked_default.jsonl"
    ts = datetime(2026, 8, 11, tzinfo=timezone.utc)
    payload = '{"ticket":201,"proof":"archive"}'
    ev_hash = _attested_hash(payload)
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(160,160)",
        "stop": "155",
        "target": "165",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-7",
        "position_status": "closed",
        "executed_entry_price": 160.0,
        "executed_notional_usd": 160.0,
        "actual_outcome": "loss",
        "actual_exit_price": 159.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-7",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
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
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
    }
    _write_rows(path, [row])
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "manual_remediation_key_instance_revoked"]) == 1


def test_manual_remediation_allows_pre_revocation_when_cutoff_configured(tmp_path) -> None:
    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_revoked_signer_ids=("ops-ledger",),
        remediation_signer_revoked_before_utc={"ops-ledger": "2026-08-12T00:00:00+00:00"},
        remediation_revocation_policy="allow_pre_revocation_only",
    )
    path = tmp_path / "manual_pre_revocation.jsonl"
    ts = datetime(2026, 8, 11, tzinfo=timezone.utc)
    payload = '{"ticket":202,"proof":"archive"}'
    ev_hash = _attested_hash(payload)
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(170,170)",
        "stop": "165",
        "target": "175",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-8",
        "position_status": "closed",
        "executed_entry_price": 170.0,
        "executed_notional_usd": 170.0,
        "actual_outcome": "loss",
        "actual_exit_price": 169.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-8",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
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
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
    }
    _write_rows(path, [row])
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert len(accepted) == 1
    assert rejected == []


def test_manual_remediation_revocation_indeterminate_fails_closed(tmp_path) -> None:
    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_revoked_signer_ids=("ops-ledger",),
        remediation_revocation_policy="allow_pre_revocation_only",
    )
    path = tmp_path / "manual_revocation_indeterminate.jsonl"
    ts = datetime(2026, 8, 11, tzinfo=timezone.utc)
    payload = '{"ticket":203,"proof":"archive"}'
    ev_hash = _attested_hash(payload)
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(180,180)",
        "stop": "175",
        "target": "185",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-9",
        "position_status": "closed",
        "executed_entry_price": 180.0,
        "executed_notional_usd": 180.0,
        "actual_outcome": "loss",
        "actual_exit_price": 179.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-9",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
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
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
    }
    _write_rows(path, [row])
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "manual_remediation_revocation_state_indeterminate"]) == 1


def test_manual_remediation_rejects_missing_key_instance_lineage(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    path = tmp_path / "manual_missing_key_instance.jsonl"
    ts = datetime(2026, 8, 13, tzinfo=timezone.utc)
    payload = '{"ticket":501,"proof":"archive"}'
    ev_hash = _attested_hash(payload)
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(190,190)",
        "stop": "185",
        "target": "195",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-10",
        "position_status": "closed",
        "executed_entry_price": 190.0,
        "executed_notional_usd": 190.0,
        "actual_outcome": "loss",
        "actual_exit_price": 189.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-10",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
        "remediation_signer_id": "ops-ledger",
        "remediation_signature_scheme": "sha256_signer_key_bind_v1",
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
    }
    _write_rows(path, [row])
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "manual_remediation_key_instance_missing"]) == 1


def test_manual_remediation_rejects_revoked_key_instance(tmp_path) -> None:
    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_key_instance_revoked_ids=("ops-ledger:key-1",),
    )
    path = tmp_path / "manual_key_revoked.jsonl"
    ts = datetime(2026, 8, 13, tzinfo=timezone.utc)
    payload = '{"ticket":502,"proof":"archive"}'
    ev_hash = _attested_hash(payload)
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(191,191)",
        "stop": "186",
        "target": "196",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-11",
        "position_status": "closed",
        "executed_entry_price": 191.0,
        "executed_notional_usd": 191.0,
        "actual_outcome": "loss",
        "actual_exit_price": 190.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-11",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
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
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
    }
    _write_rows(path, [row])
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "manual_remediation_key_instance_revoked"]) == 1


def test_manual_remediation_rejects_untrusted_issuer(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    path = tmp_path / "manual_untrusted_issuer.jsonl"
    ts = datetime(2026, 8, 15, tzinfo=timezone.utc)
    payload = '{"ticket":701,"proof":"archive"}'
    ev_hash = _attested_hash(payload)
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(200,200)",
        "stop": "195",
        "target": "205",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-12",
        "position_status": "closed",
        "executed_entry_price": 200.0,
        "executed_notional_usd": 200.0,
        "actual_outcome": "loss",
        "actual_exit_price": 199.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-12",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
        "remediation_signer_id": "ops-ledger",
        "remediation_key_instance_id": "ops-ledger:key-1",
        "remediation_key_epoch": 1,
        "remediation_key_serial": "k1",
        "remediation_issuer_id": "unknown-ca",
        "remediation_certificate_chain_id": "unknown-chain",
        "remediation_certificate_fingerprint_sha256": "a" * 64,
        "remediation_certificate_path_fingerprints": json.dumps(["a" * 64, "e" * 64, "f" * 64]),
        "remediation_certificate_path_anchor_fingerprint_sha256": "f" * 64,
        "remediation_signature_scheme": "sha256_signer_key_bind_v1",
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
    }
    _write_rows(path, [row])
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "manual_remediation_issuer_untrusted"]) == 1


def test_manual_remediation_rejects_indeterminate_issuer_binding_under_strict_policy(tmp_path) -> None:
    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_issuer_chain_policy="require_key_instance_binding",
    )
    path = tmp_path / "manual_issuer_binding_indeterminate.jsonl"
    ts = datetime(2026, 8, 16, tzinfo=timezone.utc)
    payload = '{"ticket":702,"proof":"archive"}'
    ev_hash = _attested_hash(payload)
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(201,201)",
        "stop": "196",
        "target": "206",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-13",
        "position_status": "closed",
        "executed_entry_price": 201.0,
        "executed_notional_usd": 201.0,
        "actual_outcome": "loss",
        "actual_exit_price": 200.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-13",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
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
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
    }
    _write_rows(path, [row])
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "manual_remediation_issuer_binding_indeterminate"]) == 1


def test_manual_remediation_rejects_untrusted_certificate_anchor(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    path = tmp_path / "manual_untrusted_anchor.jsonl"
    ts = datetime(2026, 8, 18, tzinfo=timezone.utc)
    payload = '{"ticket":801,"proof":"archive"}'
    ev_hash = _attested_hash(payload)
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(210,210)",
        "stop": "205",
        "target": "215",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-14",
        "position_status": "closed",
        "executed_entry_price": 210.0,
        "executed_notional_usd": 210.0,
        "actual_outcome": "loss",
        "actual_exit_price": 209.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-14",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
        "remediation_signer_id": "ops-ledger",
        "remediation_key_instance_id": "ops-ledger:key-1",
        "remediation_key_epoch": 1,
        "remediation_key_serial": "k1",
        "remediation_issuer_id": "ops-root-ca",
        "remediation_certificate_chain_id": "ops-chain-v1",
        "remediation_certificate_fingerprint_sha256": "a" * 64,
        "remediation_certificate_path_fingerprints": json.dumps(["a" * 64, "e" * 64, "c" * 64]),
        "remediation_certificate_path_anchor_fingerprint_sha256": "c" * 64,
        "remediation_signature_scheme": "sha256_signer_key_bind_v1",
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
    }
    _write_rows(path, [row])
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "manual_remediation_certificate_anchor_untrusted"]) == 1


def test_manual_remediation_rejects_certificate_path_binding_conflict(tmp_path) -> None:
    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": "f" * 64},
    )
    path = tmp_path / "manual_path_conflict.jsonl"
    ts = datetime(2026, 8, 19, tzinfo=timezone.utc)
    payload = '{"ticket":802,"proof":"archive"}'
    ev_hash = _attested_hash(payload)
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(211,211)",
        "stop": "206",
        "target": "216",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-15",
        "position_status": "closed",
        "executed_entry_price": 211.0,
        "executed_notional_usd": 211.0,
        "actual_outcome": "loss",
        "actual_exit_price": 210.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-15",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
        "remediation_signer_id": "ops-ledger",
        "remediation_key_instance_id": "ops-ledger:key-1",
        "remediation_key_epoch": 1,
        "remediation_key_serial": "k1",
        "remediation_issuer_id": "ops-root-ca",
        "remediation_certificate_chain_id": "ops-chain-v1",
        "remediation_certificate_fingerprint_sha256": "a" * 64,
        "remediation_certificate_path_fingerprints": json.dumps(["b" * 64, "e" * 64, "f" * 64]),
        "remediation_certificate_path_anchor_fingerprint_sha256": "f" * 64,
        "remediation_signature_scheme": "sha256_signer_key_bind_v1",
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
    }
    _write_rows(path, [row])
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "manual_remediation_certificate_path_conflict"]) == 1


def test_manual_remediation_certificate_path_depth_config_tolerates_invalid_values(tmp_path) -> None:
    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": "f" * 64},
        remediation_certificate_path_min_depth="invalid",  # type: ignore[arg-type]
        remediation_certificate_path_max_depth="invalid",  # type: ignore[arg-type]
    )
    path = tmp_path / "manual_path_invalid_depth_config.jsonl"
    ts = datetime(2026, 8, 20, tzinfo=timezone.utc)
    payload = '{"ticket":803,"proof":"archive"}'
    ev_hash = _attested_hash(payload)
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(212,212)",
        "stop": "207",
        "target": "217",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-16",
        "position_status": "closed",
        "executed_entry_price": 212.0,
        "executed_notional_usd": 212.0,
        "actual_outcome": "loss",
        "actual_exit_price": 211.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-16",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
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
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
    }
    _write_rows(path, [row])
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert len(accepted) == 1
    assert rejected == []


def _is_probable_prime(n: int) -> bool:
    if n < 2 or n % 2 == 0:
        return n == 2
    d = n - 1
    s = 0
    while d % 2 == 0:
        s += 1
        d //= 2
    for a in (2, 3, 5, 7, 11):
        if a >= n:
            continue
        x = pow(a, d, n)
        if x == 1 or x == n - 1:
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _next_prime(start: int) -> int:
    n = start | 1
    while not _is_probable_prime(n):
        n += 2
    return n


def _issuer_keypair(seed: int) -> tuple[int, int, int]:
    p = _next_prime((1 << 130) + seed * 1009)
    q = _next_prime((1 << 131) + seed * 2003)
    n = p * q
    phi = (p - 1) * (q - 1)
    e = 65537
    d = pow(e, -1, phi)
    return n, e, d


def _cert_link_sig(private_key_d: int, modulus_n: int, chain_id: str, parent_fingerprint: str, child_fingerprint: str, scheme: str = "inprocess_rsa_raw_sha256_v1") -> str:
    message = "|".join([scheme.lower(), chain_id.lower(), parent_fingerprint.lower(), child_fingerprint.lower()])
    digest_int = int(hashlib.sha256(message.encode("utf-8")).hexdigest(), 16)
    sig_int = pow(digest_int % modulus_n, private_key_d, modulus_n)
    return format(sig_int, "x")


def _certificate_objects(
    cert_path: list[str],
    inter1_n: int,
    inter1_e: int,
    root_n: int,
    root_e: int,
    not_before: str,
    not_after: str,
) -> str:
    return json.dumps([
        {
            "fingerprint_sha256": cert_path[0],
            "issuer_fingerprint_sha256": cert_path[1],
            "public_key_n_hex": format(inter1_n, "x"),
            "public_key_e": inter1_e,
            "serial_number_hex": "11",
            "subject_cn": "Leaf Cert",
            "issuer_cn": "Inter Cert",
            "signature_algorithm": "rsa_raw_sha256",
            "not_before_utc": not_before,
            "not_after_utc": not_after,
            "basic_constraints_ca": False,
            "key_usage_cert_sign": False,
        },
        {
            "fingerprint_sha256": cert_path[1],
            "issuer_fingerprint_sha256": cert_path[2],
            "public_key_n_hex": format(inter1_n, "x"),
            "public_key_e": inter1_e,
            "serial_number_hex": "22",
            "subject_cn": "Inter Cert",
            "issuer_cn": "Root Cert",
            "signature_algorithm": "rsa_raw_sha256",
            "not_before_utc": not_before,
            "not_after_utc": not_after,
            "basic_constraints_ca": True,
            "key_usage_cert_sign": True,
        },
        {
            "fingerprint_sha256": cert_path[2],
            "issuer_fingerprint_sha256": cert_path[2],
            "public_key_n_hex": format(root_n, "x"),
            "public_key_e": root_e,
            "serial_number_hex": "33",
            "subject_cn": "Root Cert",
            "issuer_cn": "Root Cert",
            "signature_algorithm": "rsa_raw_sha256",
            "not_before_utc": not_before,
            "not_after_utc": not_after,
            "basic_constraints_ca": True,
            "key_usage_cert_sign": True,
        },
    ])


def _build_der_chain(tmp_path) -> tuple[list[str], list[str]]:
    root_key = tmp_path / "root.key"
    root_pem = tmp_path / "root.pem"
    inter_key = tmp_path / "inter.key"
    inter_csr = tmp_path / "inter.csr"
    inter_pem = tmp_path / "inter.pem"
    leaf_key = tmp_path / "leaf.key"
    leaf_csr = tmp_path / "leaf.csr"
    leaf_pem = tmp_path / "leaf.pem"
    inter_ext = tmp_path / "inter.ext"
    leaf_ext = tmp_path / "leaf.ext"
    inter_ext.write_text("basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n", encoding="utf-8")
    leaf_ext.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n", encoding="utf-8")

    subprocess.run(["openssl", "genrsa", "-out", str(root_key), "1024"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run([
        "openssl", "req", "-x509", "-new", "-key", str(root_key), "-subj", "/CN=Root Cert", "-days", "3650", "-sha256", "-out", str(root_pem),
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["openssl", "genrsa", "-out", str(inter_key), "1024"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["openssl", "req", "-new", "-key", str(inter_key), "-subj", "/CN=Inter Cert", "-out", str(inter_csr)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["openssl", "x509", "-req", "-in", str(inter_csr), "-CA", str(root_pem), "-CAkey", str(root_key), "-CAcreateserial", "-days", "2000", "-sha256", "-out", str(inter_pem), "-extfile", str(inter_ext)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["openssl", "genrsa", "-out", str(leaf_key), "1024"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["openssl", "req", "-new", "-key", str(leaf_key), "-subj", "/CN=Leaf Cert", "-out", str(leaf_csr)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["openssl", "x509", "-req", "-in", str(leaf_csr), "-CA", str(inter_pem), "-CAkey", str(inter_key), "-CAcreateserial", "-days", "1200", "-sha256", "-out", str(leaf_pem), "-extfile", str(leaf_ext)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    der_files = []
    for src, name in [(leaf_pem, "leaf.der"), (inter_pem, "inter.der"), (root_pem, "root.der")]:
        out = tmp_path / name
        subprocess.run(["openssl", "x509", "-in", str(src), "-outform", "DER", "-out", str(out)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        der_files.append(out)
    der_hex = [f.read_bytes().hex() for f in der_files]
    fps = [hashlib.sha256(f.read_bytes()).hexdigest() for f in der_files]
    return fps, der_hex


def test_manual_remediation_accepts_cryptographically_valid_certificate_links_when_required(tmp_path) -> None:
    inter1_n, inter1_e, inter1_d = _issuer_keypair(11)
    root_n, root_e, root_d = _issuer_keypair(13)
    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": "f" * 64},
        remediation_certificate_link_policy="allow_metadata_only",
        
    )
    path = tmp_path / "manual_path_crypto_valid.jsonl"
    ts = datetime(2026, 8, 21, tzinfo=timezone.utc)
    payload = '{"ticket":804,"proof":"archive"}'
    ev_hash = _attested_hash(payload)
    chain_id = "ops-chain-v1"
    cert_path = ["a" * 64, "e" * 64, "f" * 64]
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(213,213)",
        "stop": "208",
        "target": "218",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-17",
        "position_status": "closed",
        "executed_entry_price": 213.0,
        "executed_notional_usd": 213.0,
        "actual_outcome": "loss",
        "actual_exit_price": 212.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-17",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
        "remediation_signer_id": "ops-ledger",
        "remediation_key_instance_id": "ops-ledger:key-1",
        "remediation_key_epoch": 1,
        "remediation_key_serial": "k1",
        "remediation_issuer_id": "ops-root-ca",
        "remediation_certificate_chain_id": chain_id,
        "remediation_certificate_fingerprint_sha256": cert_path[0],
        "remediation_certificate_path_fingerprints": json.dumps(cert_path),
        "remediation_certificate_path_anchor_fingerprint_sha256": cert_path[-1],
        "remediation_certificate_objects": _certificate_objects(
            cert_path,
            inter1_n,
            inter1_e,
            root_n,
            root_e,
            ts.replace(year=2025).isoformat(),
            ts.replace(year=2027).isoformat(),
        ),
        "remediation_certificate_link_signatures": json.dumps([
            _cert_link_sig(inter1_d, inter1_n, chain_id, cert_path[1], cert_path[0]),
            _cert_link_sig(root_d, root_n, chain_id, cert_path[2], cert_path[1]),
        ]),
        "remediation_certificate_link_signature_scheme": "inprocess_rsa_raw_sha256_v1",
        "remediation_signature_scheme": "sha256_signer_key_bind_v1",
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
    }
    _write_rows(path, [row])
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert len(accepted) == 1
    assert rejected == []


def test_manual_remediation_rejects_broken_certificate_link_signature_when_required(tmp_path) -> None:
    inter1_n, inter1_e, _inter1_d = _issuer_keypair(17)
    root_n, root_e, root_d = _issuer_keypair(19)
    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": "f" * 64},
        remediation_certificate_link_policy="require_verified_links",
        
    )
    path = tmp_path / "manual_path_crypto_broken.jsonl"
    ts = datetime(2026, 8, 22, tzinfo=timezone.utc)
    payload = '{"ticket":805,"proof":"archive"}'
    ev_hash = _attested_hash(payload)
    chain_id = "ops-chain-v1"
    cert_path = ["a" * 64, "e" * 64, "f" * 64]
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(214,214)",
        "stop": "209",
        "target": "219",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-18",
        "position_status": "closed",
        "executed_entry_price": 214.0,
        "executed_notional_usd": 214.0,
        "actual_outcome": "loss",
        "actual_exit_price": 213.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-18",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
        "remediation_signer_id": "ops-ledger",
        "remediation_key_instance_id": "ops-ledger:key-1",
        "remediation_key_epoch": 1,
        "remediation_key_serial": "k1",
        "remediation_issuer_id": "ops-root-ca",
        "remediation_certificate_chain_id": chain_id,
        "remediation_certificate_fingerprint_sha256": cert_path[0],
        "remediation_certificate_path_fingerprints": json.dumps(cert_path),
        "remediation_certificate_path_anchor_fingerprint_sha256": cert_path[-1],
        "remediation_certificate_objects": _certificate_objects(
            cert_path,
            inter1_n,
            inter1_e,
            root_n,
            root_e,
            ts.replace(year=2025).isoformat(),
            ts.replace(year=2027).isoformat(),
        ),
        "remediation_certificate_link_signatures": json.dumps([
            _cert_link_sig(root_d, root_n, chain_id, cert_path[2], cert_path[1]),
            _cert_link_sig(root_d, root_n, chain_id, cert_path[2], cert_path[1]),
        ]),
        "remediation_certificate_link_signature_scheme": "inprocess_rsa_raw_sha256_v1",
        "remediation_signature_scheme": "sha256_signer_key_bind_v1",
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
    }
    _write_rows(path, [row])
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "manual_remediation_certificate_link_internally_invalid"]) == 1


def test_manual_remediation_certificate_link_validation_is_deterministic_and_idempotent(tmp_path) -> None:
    inter1_n, inter1_e, inter1_d = _issuer_keypair(23)
    root_n, root_e, root_d = _issuer_keypair(29)
    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": "f" * 64},
        remediation_certificate_link_policy="require_verified_links",
        
    )
    path = tmp_path / "manual_path_crypto_idempotent.jsonl"
    ts = datetime(2026, 8, 23, tzinfo=timezone.utc)
    payload = '{"ticket":806,"proof":"archive"}'
    ev_hash = _attested_hash(payload)
    chain_id = "ops-chain-v1"
    cert_path = ["a" * 64, "e" * 64, "f" * 64]
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(215,215)",
        "stop": "210",
        "target": "220",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-19",
        "position_status": "closed",
        "executed_entry_price": 215.0,
        "executed_notional_usd": 215.0,
        "actual_outcome": "loss",
        "actual_exit_price": 214.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-19",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
        "remediation_signer_id": "ops-ledger",
        "remediation_key_instance_id": "ops-ledger:key-1",
        "remediation_key_epoch": 1,
        "remediation_key_serial": "k1",
        "remediation_issuer_id": "ops-root-ca",
        "remediation_certificate_chain_id": chain_id,
        "remediation_certificate_fingerprint_sha256": cert_path[0],
        "remediation_certificate_path_fingerprints": json.dumps(cert_path),
        "remediation_certificate_path_anchor_fingerprint_sha256": cert_path[-1],
        "remediation_certificate_objects": _certificate_objects(
            cert_path,
            inter1_n,
            inter1_e,
            root_n,
            root_e,
            ts.replace(year=2025).isoformat(),
            ts.replace(year=2027).isoformat(),
        ),
        "remediation_certificate_link_signatures": json.dumps([
            _cert_link_sig(inter1_d, inter1_n, chain_id, cert_path[1], cert_path[0]),
            _cert_link_sig(root_d, root_n, chain_id, cert_path[2], cert_path[1]),
        ]),
        "remediation_certificate_link_signature_scheme": "inprocess_rsa_raw_sha256_v1",
        "remediation_signature_scheme": "sha256_signer_key_bind_v1",
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
    }
    _write_rows(path, [row])
    accepted_a, rejected_a = load_validated_realized_rows(str(path), cfg)
    accepted_b, rejected_b = load_validated_realized_rows(str(path), cfg)
    assert [r.reason for r in rejected_a] == [r.reason for r in rejected_b]
    assert [r.lineage_trust_tier for r in accepted_a] == [r.lineage_trust_tier for r in accepted_b]
    assert [r.entry.position_id for r in accepted_a] == [r.entry.position_id for r in accepted_b]


def test_manual_remediation_der_tbs_validation_is_deterministic_and_idempotent(tmp_path) -> None:
    cert_path, der_chain = _build_der_chain(tmp_path)
    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": cert_path[-1]},
        remediation_certificate_link_policy="require_verified_links",
        remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1",
        remediation_trusted_root_fingerprints=(cert_path[-1],),
    )
    path = tmp_path / "manual_der_tbs_idempotent.jsonl"
    ts = datetime(2026, 11, 1, tzinfo=timezone.utc)
    payload = '{"ticket":880,"proof":"archive"}'
    ev_hash = _attested_hash(payload)
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(250,250)",
        "stop": "245",
        "target": "255",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-der-1",
        "position_status": "closed",
        "executed_entry_price": 250.0,
        "executed_notional_usd": 250.0,
        "actual_outcome": "loss",
        "actual_exit_price": 249.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-der-1",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
        "remediation_signer_id": "ops-ledger",
        "remediation_key_instance_id": "ops-ledger:key-1",
        "remediation_key_epoch": 1,
        "remediation_key_serial": "k1",
        "remediation_issuer_id": "ops-root-ca",
        "remediation_certificate_chain_id": "ops-chain-v3",
        "remediation_certificate_fingerprint_sha256": cert_path[0],
        "remediation_certificate_path_fingerprints": json.dumps(cert_path),
        "remediation_certificate_path_anchor_fingerprint_sha256": cert_path[-1],
        "remediation_certificate_der_hex_chain": json.dumps(der_chain),
        "remediation_certificate_link_signature_scheme": "der_tbs_rsa_sha256_v1",
        "remediation_signature_scheme": "sha256_signer_key_bind_v1",
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
    }
    _write_rows(path, [row])
    accepted_a, rejected_a = load_validated_realized_rows(str(path), cfg)
    accepted_b, rejected_b = load_validated_realized_rows(str(path), cfg)
    assert [r.reason for r in rejected_a] == [r.reason for r in rejected_b]
    assert [r.lineage_trust_tier for r in accepted_a] == [r.lineage_trust_tier for r in accepted_b]
    assert [r.entry.position_id for r in accepted_a] == [r.entry.position_id for r in accepted_b]


def test_manual_remediation_rejects_manifest_membership_conflict(tmp_path) -> None:
    cert_path, der_chain = _build_der_chain(tmp_path)
    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": cert_path[-1]},
        remediation_certificate_link_policy="require_verified_links",
        remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1",
        remediation_trusted_root_fingerprints=(cert_path[-1],),
        remediation_evidence_manifest_policy="require_complete_manifest",
    )
    path = tmp_path / "manual_manifest_conflict.jsonl"
    ts = datetime(2026, 11, 2, tzinfo=timezone.utc)
    payload = '{"ticket":881,"proof":"archive"}'
    ev_hash = _attested_hash(payload)
    manifest_obj = {
        "bundle_id": "bundle-der-2",
        "evidence_hash_sha256": ev_hash,
        "artifacts": [
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "required": True},
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": hashlib.sha256(b"other").hexdigest(), "required": True},
        ],
    }
    manifest_json = json.dumps(manifest_obj, sort_keys=True, separators=(",", ":"))
    manifest_hash = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(251,251)",
        "stop": "246",
        "target": "256",
        "projected_rr": 2.0,
        "confidence": 0.8,
        "reasoning": "manual-remediation",
        "what_would_have_made_this_stand_aside": "n/a",
        "position_size_btc": 1.0,
        "position_id": "manual-pos-der-2",
        "position_status": "closed",
        "executed_entry_price": 251.0,
        "executed_notional_usd": 251.0,
        "actual_outcome": "loss",
        "actual_exit_price": 250.0,
        "actual_pnl_usd": -1.0,
        "actual_pnl_basis": "net",
        "remediation_status": "manual_recovered",
        "remediation_bundle_id": "bundle-der-2",
        "remediation_evidence_ref": "sha256:" + ev_hash,
        "remediation_evidence_manifest_json": manifest_json,
        "remediation_evidence_manifest_hash_sha256": manifest_hash,
        "remediation_evidence_payload": payload,
        "remediation_evidence_hash_sha256": ev_hash,
        "remediation_parent_lineage_tier": "legacy_lineage_rejected",
        "remediation_operator": "auditor",
        "remediation_reason": "manual proof pack",
        "remediation_timestamp_utc": ts.isoformat(),
        "remediation_external_source": "ext://exchange-archive",
        "remediation_signer_id": "ops-ledger",
        "remediation_key_instance_id": "ops-ledger:key-1",
        "remediation_key_epoch": 1,
        "remediation_key_serial": "k1",
        "remediation_issuer_id": "ops-root-ca",
        "remediation_certificate_chain_id": "ops-chain-v3",
        "remediation_certificate_fingerprint_sha256": cert_path[0],
        "remediation_certificate_path_fingerprints": json.dumps(cert_path),
        "remediation_certificate_path_anchor_fingerprint_sha256": cert_path[-1],
        "remediation_certificate_der_hex_chain": json.dumps(der_chain),
        "remediation_certificate_link_signature_scheme": "der_tbs_rsa_sha256_v1",
        "remediation_signature_scheme": "sha256_signer_key_bind_v1",
        "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
    }
    _write_rows(path, [row])
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "manual_remediation_manifest_invalid"]) == 1


def test_migration_review_cohere_for_retrieval_valid_invalid_indeterminate(tmp_path) -> None:
    source = tmp_path / "source_retrieval_coherence.jsonl"
    migrated = tmp_path / "migrated_retrieval_coherence.jsonl"
    ambiguous = tmp_path / "ambiguous_retrieval_coherence.jsonl"
    ts = datetime(2026, 10, 8, tzinfo=timezone.utc)
    cert_path, der_chain = _build_der_chain(tmp_path)

    def _manifest_json_hash(bundle_id: str, evidence_hash: str, artifacts: list[dict]) -> tuple[str, str]:
        payload = {
            "bundle_id": bundle_id,
            "evidence_hash_sha256": evidence_hash,
            "artifacts": sorted(
                [
                    {
                        "role": str(a["role"]).strip().lower(),
                        "path": str(a["path"]).strip(),
                        "sha256": str(a["sha256"]).strip().lower(),
                        "required": bool(a["required"]),
                    }
                    for a in artifacts
                ],
                key=lambda a: (a["role"], a["path"], a["sha256"], int(a["required"])),
            ),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return raw, hashlib.sha256(raw.encode()).hexdigest()

    def _retrieval_json_hash(bundle_id: str, evidence_hash: str, retrievals: list[dict]) -> tuple[str, str]:
        payload = {
            "bundle_id": bundle_id,
            "evidence_hash_sha256": evidence_hash,
            "retrievals": sorted(
                [
                    {
                        "role": str(r["role"]).strip().lower(),
                        "path": str(r["path"]).strip(),
                        "sha256": str(r["sha256"]).strip().lower(),
                        "available": bool(r["available"]),
                        "content_sha256": str(r["content_sha256"]).strip().lower(),
                        "retrieval_receipt_sha256": str(r["retrieval_receipt_sha256"]).strip().lower(),
                        "retrieval_receipt_attestor_id": str(r.get("retrieval_receipt_attestor_id", "") or "").strip(),
                        "retrieval_receipt_attestor_key_instance_id": str(r.get("retrieval_receipt_attestor_key_instance_id", "") or "").strip(),
                        "retrieval_receipt_source": str(r.get("retrieval_receipt_source", "") or "").strip(),
                        "retrieval_receipt_signature": str(r.get("retrieval_receipt_signature", "") or "").strip().lower(),
                        "retrieval_receipt_signature_scheme": str(r.get("retrieval_receipt_signature_scheme", "") or "").strip().lower(),
                    }
                    for r in retrievals
                ],
                key=lambda r: (r["role"], r["path"], r["sha256"], r["content_sha256"], r["retrieval_receipt_sha256"], r["retrieval_receipt_attestor_id"], r["retrieval_receipt_attestor_key_instance_id"], r["retrieval_receipt_source"], r["retrieval_receipt_signature"], r["retrieval_receipt_signature_scheme"]),
            ),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return raw, hashlib.sha256(raw.encode()).hexdigest()

    def _row(position_id: str, minute: int, retrieval_mode: str) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "migration-retrieval"})
        ev_hash = _attested_hash(payload)
        bundle_id = f"bundle-{position_id}"
        cert_sha = hashlib.sha256(b"chain").hexdigest()
        manifest_json, manifest_hash = _manifest_json_hash(
            bundle_id,
            ev_hash,
            [
                {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "required": True},
                {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "required": True},
            ],
        )
        retrievals = [
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "available": True, "content_sha256": ev_hash, "retrieval_receipt_sha256": hashlib.sha256((position_id+"-proof").encode()).hexdigest()},
            {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "available": True, "content_sha256": cert_sha, "retrieval_receipt_sha256": hashlib.sha256((position_id+"-chain").encode()).hexdigest()},
        ]
        if retrieval_mode == "invalid":
            retrievals[1]["content_sha256"] = "0" * 64
        if retrieval_mode == "indeterminate":
            retrievals.append({"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "available": False, "content_sha256": cert_sha, "retrieval_receipt_sha256": hashlib.sha256((position_id+"-conflict").encode()).hexdigest()})
        retrieval_json, retrieval_hash = _retrieval_json_hash(bundle_id, ev_hash, retrievals)
        return {
            "timestamp": ts.replace(minute=minute).isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(220,220)",
            "stop": "215",
            "target": "225",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "manual-remediation",
            "what_would_have_made_this_stand_aside": "n/a",
            "position_size_btc": 1.0,
            "position_id": position_id,
            "position_status": "closed",
            "executed_entry_price": 220.0,
            "executed_notional_usd": 220.0,
            "actual_outcome": "loss",
            "actual_exit_price": 219.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
            "remediation_status": "manual_recovered",
            "remediation_bundle_id": bundle_id,
            "remediation_evidence_ref": "sha256:" + ev_hash,
            "remediation_evidence_manifest_json": manifest_json,
            "remediation_evidence_manifest_hash_sha256": manifest_hash,
            "remediation_evidence_retrieval_proofs_json": retrieval_json,
            "remediation_evidence_retrieval_proofs_hash_sha256": retrieval_hash,
            "remediation_evidence_payload": payload,
            "remediation_evidence_hash_sha256": ev_hash,
            "remediation_parent_lineage_tier": "legacy_lineage_rejected",
            "remediation_operator": "auditor",
            "remediation_reason": "migration coherence",
            "remediation_timestamp_utc": ts.replace(minute=minute).isoformat(),
            "remediation_external_source": "ext://exchange-archive",
            "remediation_signer_id": "ops-ledger",
            "remediation_key_instance_id": "ops-ledger:key-1",
            "remediation_key_epoch": 1,
            "remediation_key_serial": "k1",
            "remediation_issuer_id": "ops-root-ca",
            "remediation_certificate_chain_id": "ops-chain-v2",
            "remediation_certificate_fingerprint_sha256": cert_path[0],
            "remediation_certificate_path_fingerprints": json.dumps(cert_path),
            "remediation_certificate_path_anchor_fingerprint_sha256": cert_path[-1],
            "remediation_certificate_der_hex_chain": json.dumps(der_chain),
            "remediation_certificate_link_signature_scheme": "der_tbs_rsa_sha256_v1",
            "remediation_signature_scheme": "sha256_signer_key_bind_v1",
            "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
        }

    _write_rows(source, [
        _row("migration-retrieval-valid", 0, "valid"),
        _row("migration-retrieval-invalid", 1, "invalid"),
        _row("migration-retrieval-indeterminate", 2, "indeterminate"),
    ])

    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_external_signature_policy="require",
        remediation_trusted_signer_ids=("ops-ledger",),
        remediation_key_rotation_policy="allow_historical_non_active",
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": cert_path[-1]},
        remediation_certificate_link_policy="allow_metadata_only",
        remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1",
        remediation_trusted_root_fingerprints=(cert_path[-1],),
        remediation_evidence_manifest_policy="require_complete_manifest",
        remediation_evidence_retrieval_policy="require_retrieval_proofs",
        remediation_retrieval_receipt_auth_policy="allow_unsigned_receipts",
    )

    accepted_src, rejected_src = load_validated_realized_rows(str(source), cfg)
    report = migrate_legacy_history(str(source), str(migrated), str(ambiguous), cfg)
    accepted_mig, rejected_mig = load_validated_realized_rows(str(migrated), cfg)

    assert report.total_rows == 3
    assert [r.entry.position_id for r in accepted_src] == ["migration-retrieval-valid"]
    assert [r.entry.position_id for r in accepted_mig] == ["migration-retrieval-valid"]
    assert sorted(r.reason for r in rejected_src) == sorted(r.reason for r in rejected_mig)

    summary = review_journal(str(migrated), cfg)
    assert summary["manual_remediated_attested_lineage_rows"] == 1
    assert summary["manual_remediation_revoked_rows"] == 1
    assert summary["manual_remediation_indeterminate_rows"] == 1


def test_migration_review_cohere_for_receipt_attestor_lifecycle_states(tmp_path) -> None:
    source = tmp_path / "source_receipt_attestor_lifecycle.jsonl"
    migrated = tmp_path / "migrated_receipt_attestor_lifecycle.jsonl"
    ambiguous = tmp_path / "ambiguous_receipt_attestor_lifecycle.jsonl"
    ts = datetime(2026, 10, 11, tzinfo=timezone.utc)
    cert_path, der_chain = _build_der_chain(tmp_path)

    def _manifest_json_hash(bundle_id: str, evidence_hash: str, artifacts: list[dict]) -> tuple[str, str]:
        payload = {
            "bundle_id": bundle_id,
            "evidence_hash_sha256": evidence_hash,
            "artifacts": sorted(
                [{"role": str(a["role"]).strip().lower(), "path": str(a["path"]).strip(), "sha256": str(a["sha256"]).strip().lower(), "required": bool(a["required"])} for a in artifacts],
                key=lambda a: (a["role"], a["path"], a["sha256"], int(a["required"])),
            ),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return raw, hashlib.sha256(raw.encode()).hexdigest()

    def _retrieval_json_hash(bundle_id: str, evidence_hash: str, retrievals: list[dict]) -> tuple[str, str]:
        payload = {
            "bundle_id": bundle_id,
            "evidence_hash_sha256": evidence_hash,
            "retrievals": sorted(
                [{
                    "role": str(r["role"]).strip().lower(),
                    "path": str(r["path"]).strip(),
                    "sha256": str(r["sha256"]).strip().lower(),
                    "available": bool(r["available"]),
                    "content_sha256": str(r["content_sha256"]).strip().lower(),
                    "retrieval_receipt_sha256": str(r["retrieval_receipt_sha256"]).strip().lower(),
                    "retrieval_receipt_attestor_id": str(r.get("retrieval_receipt_attestor_id", "") or "").strip(),
                    "retrieval_receipt_attestor_key_instance_id": str(r.get("retrieval_receipt_attestor_key_instance_id", "") or "").strip(),
                    "retrieval_receipt_source": str(r.get("retrieval_receipt_source", "") or "").strip(),
                    "retrieval_receipt_signature": str(r.get("retrieval_receipt_signature", "") or "").strip().lower(),
                    "retrieval_receipt_signature_scheme": str(r.get("retrieval_receipt_signature_scheme", "") or "").strip().lower(),
                } for r in retrievals],
                key=lambda r: (r["role"], r["path"], r["sha256"], r["content_sha256"], r["retrieval_receipt_sha256"], r["retrieval_receipt_attestor_id"], r["retrieval_receipt_attestor_key_instance_id"], r["retrieval_receipt_source"], r["retrieval_receipt_signature"], r["retrieval_receipt_signature_scheme"]),
            ),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return raw, hashlib.sha256(raw.encode()).hexdigest()

    def _receipt_sig(attestor_id: str, key_instance_id: str, bundle_id: str, evidence_hash: str, role: str, path: str, sha: str, receipt_sha: str) -> str:
        msg = "|".join(["sha256_receipt_bind_v1", attestor_id.lower(), key_instance_id.lower(), "ext://receipts", bundle_id, evidence_hash.lower(), role.lower(), path, sha.lower(), sha.lower(), receipt_sha.lower()])
        return hashlib.sha256(msg.encode()).hexdigest()

    def _row(position_id: str, minute: int, attestor_id: str) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "migration-attestor-lifecycle"})
        ev_hash = _attested_hash(payload)
        bundle_id = f"bundle-{position_id}"
        cert_sha = hashlib.sha256(b"chain").hexdigest()
        manifest_json, manifest_hash = _manifest_json_hash(bundle_id, ev_hash, [
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "required": True},
            {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "required": True},
        ])
        retrievals = []
        for role, path, sha, suffix in [
            ("primary_evidence", "files/proof.json", ev_hash, "proof"),
            ("certificate_chain", "certs/chain.der", cert_sha, "chain"),
        ]:
            receipt_sha = hashlib.sha256((position_id+suffix).encode()).hexdigest()
            retrievals.append({
                "role": role,
                "path": path,
                "sha256": sha,
                "available": True,
                "content_sha256": sha,
                "retrieval_receipt_sha256": receipt_sha,
                "retrieval_receipt_attestor_id": attestor_id,
                "retrieval_receipt_attestor_key_instance_id": f"{attestor_id}:key-1",
                "retrieval_receipt_source": "ext://receipts",
                "retrieval_receipt_signature_scheme": "sha256_receipt_bind_v1",
                "retrieval_receipt_signature": _receipt_sig(attestor_id, f"{attestor_id}:key-1", bundle_id, ev_hash, role, path, sha, receipt_sha),
            })
        retrieval_json, retrieval_hash = _retrieval_json_hash(bundle_id, ev_hash, retrievals)
        return {
            "timestamp": ts.replace(minute=minute).isoformat(),
            "market_regime": "Trend Up",
            "decision": "Paper Long",
            "entry": "(220,220)",
            "stop": "215",
            "target": "225",
            "projected_rr": 2.0,
            "confidence": 0.8,
            "reasoning": "manual-remediation",
            "what_would_have_made_this_stand_aside": "n/a",
            "position_size_btc": 1.0,
            "position_id": position_id,
            "position_status": "closed",
            "executed_entry_price": 220.0,
            "executed_notional_usd": 220.0,
            "actual_outcome": "loss",
            "actual_exit_price": 219.0,
            "actual_pnl_usd": -1.0,
            "actual_pnl_basis": "net",
            "remediation_status": "manual_recovered",
            "remediation_bundle_id": bundle_id,
            "remediation_evidence_ref": "sha256:" + ev_hash,
            "remediation_evidence_manifest_json": manifest_json,
            "remediation_evidence_manifest_hash_sha256": manifest_hash,
            "remediation_evidence_retrieval_proofs_json": retrieval_json,
            "remediation_evidence_retrieval_proofs_hash_sha256": retrieval_hash,
            "remediation_evidence_payload": payload,
            "remediation_evidence_hash_sha256": ev_hash,
            "remediation_parent_lineage_tier": "legacy_lineage_rejected",
            "remediation_operator": "auditor",
            "remediation_reason": "migration attestor lifecycle",
            "remediation_timestamp_utc": ts.replace(minute=minute).isoformat(),
            "remediation_external_source": "ext://exchange-archive",
            "remediation_signer_id": "ops-ledger",
            "remediation_key_instance_id": "ops-ledger:key-1",
            "remediation_key_epoch": 1,
            "remediation_key_serial": "k1",
            "remediation_issuer_id": "ops-root-ca",
            "remediation_certificate_chain_id": "ops-chain-v2",
            "remediation_certificate_fingerprint_sha256": cert_path[0],
            "remediation_certificate_path_fingerprints": json.dumps(cert_path),
            "remediation_certificate_path_anchor_fingerprint_sha256": cert_path[-1],
            "remediation_certificate_der_hex_chain": json.dumps(der_chain),
            "remediation_certificate_link_signature_scheme": "der_tbs_rsa_sha256_v1",
            "remediation_signature_scheme": "sha256_signer_key_bind_v1",
            "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://exchange-archive", ev_hash),
        }

    _write_rows(source, [
        _row("mig-attestor-valid", 0, "ops-retrieval-ledger"),
        _row("mig-attestor-revoked", 1, "ops-retrieval-ledger"),
        _row("mig-attestor-indeterminate", 2, "ops-retrieval-legacy"),
    ])

    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_external_signature_policy="require",
        remediation_trusted_signer_ids=("ops-ledger",),
        remediation_key_rotation_policy="allow_historical_non_active",
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": cert_path[-1]},
        remediation_certificate_link_policy="allow_metadata_only",
        remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1",
        remediation_trusted_root_fingerprints=(cert_path[-1],),
        remediation_evidence_manifest_policy="require_complete_manifest",
        remediation_evidence_retrieval_policy="require_retrieval_proofs",
        remediation_retrieval_receipt_auth_policy="require_authentic_receipts",
        remediation_trusted_retrieval_receipt_attestor_ids=("ops-retrieval-ledger", "ops-retrieval-legacy"),
        remediation_revoked_retrieval_receipt_attestor_ids=("ops-retrieval-ledger", "ops-retrieval-legacy"),
        remediation_retrieval_receipt_attestor_lifecycle_policy="allow_pre_revocation_only",
        remediation_retrieval_receipt_attestor_revoked_before_utc={"ops-retrieval-ledger": "2026-10-11T00:01:00+00:00"},
        remediation_trusted_retrieval_receipt_attestor_key_instances={"ops-retrieval-ledger": ("ops-retrieval-ledger:key-1",), "ops-retrieval-legacy": ("ops-retrieval-legacy:key-1",)},
    )

    accepted_src, rejected_src = load_validated_realized_rows(str(source), cfg)
    report = migrate_legacy_history(str(source), str(migrated), str(ambiguous), cfg)
    accepted_mig, rejected_mig = load_validated_realized_rows(str(migrated), cfg)
    assert report.total_rows == 3
    assert [r.entry.position_id for r in accepted_src] == ["mig-attestor-valid"]
    assert [r.entry.position_id for r in accepted_mig] == ["mig-attestor-valid"]
    assert sorted(r.reason for r in rejected_src) == sorted(r.reason for r in rejected_mig)

    summary = review_journal(str(migrated), cfg)
    assert summary["manual_remediated_attested_lineage_rows"] == 1
    assert summary["manual_remediation_revoked_rows"] == 1
    assert summary["manual_remediation_indeterminate_rows"] == 1


def test_migration_review_cohere_for_receipt_attestor_key_instance_states(tmp_path) -> None:
    source = tmp_path / "source_receipt_attestor_key_instance.jsonl"
    migrated = tmp_path / "migrated_receipt_attestor_key_instance.jsonl"
    ambiguous = tmp_path / "ambiguous_receipt_attestor_key_instance.jsonl"
    ts = datetime(2026, 10, 15, tzinfo=timezone.utc)
    cert_path, der_chain = _build_der_chain(tmp_path)

    def _manifest(bundle_id: str, evidence_hash: str, arts: list[dict]) -> tuple[str, str]:
        payload = {"bundle_id": bundle_id, "evidence_hash_sha256": evidence_hash, "artifacts": sorted([{"role":a["role"],"path":a["path"],"sha256":a["sha256"],"required":a["required"]} for a in arts], key=lambda a:(a["role"],a["path"],a["sha256"],int(a["required"])))}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return raw, hashlib.sha256(raw.encode()).hexdigest()

    def _sig(key_instance_id: str, bundle_id: str, evidence_hash: str, role: str, path: str, sha: str, receipt_sha: str) -> str:
        msg = "|".join(["sha256_receipt_bind_v1", "ops-retrieval-ledger", key_instance_id.lower(), "ext://receipts", bundle_id, evidence_hash.lower(), role.lower(), path, sha.lower(), sha.lower(), receipt_sha.lower()])
        return hashlib.sha256(msg.encode()).hexdigest()

    def _retrieval(bundle_id: str, evidence_hash: str, rows: list[dict]) -> tuple[str, str]:
        payload = {"bundle_id": bundle_id, "evidence_hash_sha256": evidence_hash, "retrievals": sorted(rows, key=lambda r:(r["role"],r["path"],r["sha256"],r["content_sha256"],r["retrieval_receipt_sha256"],r.get("retrieval_receipt_attestor_id",""),r.get("retrieval_receipt_attestor_key_instance_id",""),r.get("retrieval_receipt_source",""),r.get("retrieval_receipt_signature",""),r.get("retrieval_receipt_signature_scheme","")))}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return raw, hashlib.sha256(raw.encode()).hexdigest()

    def _row(position_id: str, minute: int, key_instance_id: str) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "migration-key-instance"})
        ev_hash = _attested_hash(payload)
        bundle_id = f"bundle-{position_id}"
        cert_sha = hashlib.sha256(b"chain").hexdigest()
        manifest_json, manifest_hash = _manifest(bundle_id, ev_hash, [
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "required": True},
            {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "required": True},
        ])
        retrievals=[]
        for role,path,sha,suffix in [("primary_evidence","files/proof.json",ev_hash,"proof"),("certificate_chain","certs/chain.der",cert_sha,"chain")]:
            rec=hashlib.sha256((position_id+suffix).encode()).hexdigest()
            retrievals.append({"role": role, "path": path, "sha256": sha, "available": True, "content_sha256": sha, "retrieval_receipt_sha256": rec, "retrieval_receipt_attestor_id": "ops-retrieval-ledger", "retrieval_receipt_attestor_key_instance_id": key_instance_id, "retrieval_receipt_source": "ext://receipts", "retrieval_receipt_signature_scheme": "sha256_receipt_bind_v1", "retrieval_receipt_signature": _sig(key_instance_id, bundle_id, ev_hash, role, path, sha, rec)})
        retrieval_json, retrieval_hash = _retrieval(bundle_id, ev_hash, retrievals)
        return {
            "timestamp": ts.replace(minute=minute).isoformat(), "market_regime":"Trend Up","decision":"Paper Long","entry":"(220,220)","stop":"215","target":"225","projected_rr":2.0,"confidence":0.8,
            "reasoning":"manual-remediation","what_would_have_made_this_stand_aside":"n/a","position_size_btc":1.0,"position_id":position_id,"position_status":"closed",
            "executed_entry_price":220.0,"executed_notional_usd":220.0,"actual_outcome":"loss","actual_exit_price":219.0,"actual_pnl_usd":-1.0,"actual_pnl_basis":"net",
            "remediation_status":"manual_recovered","remediation_bundle_id":bundle_id,"remediation_evidence_ref":"sha256:"+ev_hash,
            "remediation_evidence_manifest_json":manifest_json,"remediation_evidence_manifest_hash_sha256":manifest_hash,
            "remediation_evidence_retrieval_proofs_json":retrieval_json,"remediation_evidence_retrieval_proofs_hash_sha256":retrieval_hash,
            "remediation_evidence_payload":payload,"remediation_evidence_hash_sha256":ev_hash,
            "remediation_parent_lineage_tier":"legacy_lineage_rejected","remediation_operator":"auditor","remediation_reason":"migration key instance",
            "remediation_timestamp_utc":ts.replace(minute=minute).isoformat(),"remediation_external_source":"ext://exchange-archive",
            "remediation_signer_id":"ops-ledger","remediation_key_instance_id":"ops-ledger:key-1","remediation_key_epoch":1,"remediation_key_serial":"k1","remediation_issuer_id":"ops-root-ca",
            "remediation_certificate_chain_id":"ops-chain-v2","remediation_certificate_fingerprint_sha256":cert_path[0],"remediation_certificate_path_fingerprints":json.dumps(cert_path),
            "remediation_certificate_path_anchor_fingerprint_sha256":cert_path[-1],"remediation_certificate_der_hex_chain":json.dumps(der_chain),
            "remediation_certificate_link_signature_scheme":"der_tbs_rsa_sha256_v1","remediation_signature_scheme":"sha256_signer_key_bind_v1",
            "remediation_signature":_external_sig("ops-ledger","ops-ledger:key-1","ext://exchange-archive",ev_hash),
        }

    _write_rows(source,[
        _row("mig-key-valid",0,"ops-retrieval-ledger:key-1"),
        _row("mig-key-revoked",1,"ops-retrieval-ledger:key-2"),
        _row("mig-key-indeterminate",2,"ops-retrieval-ledger:key-3"),
    ])

    cfg = StrategyConfig(
        taker_fee_rate=0.0, slippage_rate=0.0,
        remediation_external_signature_policy="require", remediation_trusted_signer_ids=("ops-ledger",), remediation_key_rotation_policy="allow_historical_non_active",
        remediation_issuer_chain_policy="require_key_instance_binding", remediation_key_instance_issuer_bindings={"ops-ledger:key-1":"ops-root-ca"}, remediation_issuer_root_fingerprints={"ops-root-ca":cert_path[-1]},
        remediation_certificate_link_policy="allow_metadata_only", remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1", remediation_trusted_root_fingerprints=(cert_path[-1],),
        remediation_evidence_manifest_policy="require_complete_manifest", remediation_evidence_retrieval_policy="require_retrieval_proofs", remediation_retrieval_receipt_auth_policy="require_authentic_receipts",
        remediation_trusted_retrieval_receipt_attestor_ids=("ops-retrieval-ledger",), remediation_retrieval_receipt_attestor_key_rotation_policy="require_active_key_lineage",
        remediation_trusted_retrieval_receipt_attestor_key_instances={"ops-retrieval-ledger":("ops-retrieval-ledger:key-1",)},
        remediation_revoked_retrieval_receipt_attestor_key_instances=("ops-retrieval-ledger:key-2",), remediation_retrieval_receipt_attestor_lifecycle_policy="allow_pre_revocation_only",
    )

    accepted_src, rejected_src = load_validated_realized_rows(str(source), cfg)
    report = migrate_legacy_history(str(source), str(migrated), str(ambiguous), cfg)
    accepted_mig, rejected_mig = load_validated_realized_rows(str(migrated), cfg)
    assert report.total_rows == 3
    assert [r.entry.position_id for r in accepted_src] == ["mig-key-valid"]
    assert [r.entry.position_id for r in accepted_mig] == ["mig-key-valid"]
    assert sorted(r.reason for r in rejected_src) == sorted(r.reason for r in rejected_mig)
    summary = review_journal(str(migrated), cfg)
    assert summary["manual_remediated_attested_lineage_rows"] == 1
    assert summary["manual_remediation_revoked_rows"] == 1
    assert summary["manual_remediation_indeterminate_rows"] == 1


def test_migration_review_cohere_for_receipt_attestor_key_lifecycle_evidence_states(tmp_path) -> None:
    source = tmp_path / "source_receipt_key_lifecycle_evidence.jsonl"
    migrated = tmp_path / "migrated_receipt_key_lifecycle_evidence.jsonl"
    ambiguous = tmp_path / "ambiguous_receipt_key_lifecycle_evidence.jsonl"
    ts = datetime(2026, 10, 21, tzinfo=timezone.utc)
    cert_path, der_chain = _build_der_chain(tmp_path)

    def _lifecycle_evidence(bundle_id: str, evidence_hash: str, anchor_key_instance_id: str) -> tuple[str, str]:
        payload = {
            "bundle_id": bundle_id,
            "evidence_hash_sha256": evidence_hash,
            "anchor_id": "ops-retrieval-key-ledger",
            "anchor_key_instance_id": anchor_key_instance_id,
            "source": "ext://key-lifecycle",
            "records": [{"attestor_id": "ops-retrieval-ledger", "key_instance_id": "ops-retrieval-ledger:key-1", "lifecycle_state": "active", "revoked_before_utc": ""}],
        }
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        payload["signature_scheme"] = "sha256_key_lifecycle_bind_v1"
        payload["signature"] = hashlib.sha256("|".join(["sha256_key_lifecycle_bind_v1", "ops-retrieval-key-ledger", anchor_key_instance_id, "ext://key-lifecycle", payload_hash]).encode()).hexdigest()
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return raw, hashlib.sha256(raw.encode()).hexdigest()

    def _governance_dataset(bundle_id: str, evidence_hash: str, records: list[dict]) -> tuple[str, str]:
        payload = {
            "bundle_id": bundle_id,
            "evidence_hash_sha256": evidence_hash,
            "governance_anchor_id": "ops-retrieval-anchor-governance-ledger",
            "source": "ext://anchor-governance",
            "records": sorted(
                [{"anchor_id": str(r["anchor_id"]), "anchor_key_instance_id": str(r["anchor_key_instance_id"]), "lifecycle_state": str(r["lifecycle_state"]), "revoked_before_utc": str(r.get("revoked_before_utc", "") or "")} for r in records],
                key=lambda r: (r["anchor_id"], r["anchor_key_instance_id"], r["lifecycle_state"], r["revoked_before_utc"]),
            ),
        }
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        payload["signature_scheme"] = "sha256_anchor_governance_dataset_bind_v1"
        payload["signature"] = hashlib.sha256("|".join(["sha256_anchor_governance_dataset_bind_v1", "ops-retrieval-anchor-governance-ledger", "ext://anchor-governance", payload_hash]).encode()).hexdigest()
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return raw, hashlib.sha256(raw.encode()).hexdigest()

    def _governance_anchor_identity_provenance(bundle_id: str, evidence_hash: str, records: list[dict]) -> tuple[str, str]:
        payload = {
            "bundle_id": bundle_id,
            "evidence_hash_sha256": evidence_hash,
            "provenance_anchor_id": "ops-retrieval-anchor-identity-ledger",
            "source": "ext://governance-anchor-identity",
            "records": sorted(
                [{"governance_anchor_id": str(r["governance_anchor_id"]), "lifecycle_state": str(r["lifecycle_state"]), "revoked_before_utc": str(r.get("revoked_before_utc", "") or "")} for r in records],
                key=lambda r: (r["governance_anchor_id"], r["lifecycle_state"], r["revoked_before_utc"]),
            ),
        }
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        payload["signature_scheme"] = "sha256_governance_anchor_identity_bind_v1"
        payload["signature"] = hashlib.sha256("|".join(["sha256_governance_anchor_identity_bind_v1", "ops-retrieval-anchor-identity-ledger", "ext://governance-anchor-identity", payload_hash]).encode()).hexdigest()
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return raw, hashlib.sha256(raw.encode()).hexdigest()

    def _identity_provenance_anchor_provenance(bundle_id: str, evidence_hash: str, records: list[dict]) -> tuple[str, str]:
        payload = {
            "bundle_id": bundle_id,
            "evidence_hash_sha256": evidence_hash,
            "root_anchor_id": "ops-retrieval-anchor-identity-root-ledger",
            "source": "ext://identity-provenance-anchor",
            "records": sorted(
                [{"provenance_anchor_id": str(r["provenance_anchor_id"]), "lifecycle_state": str(r["lifecycle_state"]), "revoked_before_utc": str(r.get("revoked_before_utc", "") or "")} for r in records],
                key=lambda r: (r["provenance_anchor_id"], r["lifecycle_state"], r["revoked_before_utc"]),
            ),
        }
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        payload["signature_scheme"] = "sha256_identity_provenance_anchor_bind_v1"
        payload["signature"] = hashlib.sha256("|".join(["sha256_identity_provenance_anchor_bind_v1", "ops-retrieval-anchor-identity-root-ledger", "ext://identity-provenance-anchor", payload_hash]).encode()).hexdigest()
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return raw, hashlib.sha256(raw.encode()).hexdigest()

    def _manifest(bundle_id: str, evidence_hash: str, arts: list[dict]) -> tuple[str, str]:
        payload = {"bundle_id": bundle_id, "evidence_hash_sha256": evidence_hash, "artifacts": sorted([{"role":a["role"],"path":a["path"],"sha256":a["sha256"],"required":a["required"]} for a in arts], key=lambda a:(a["role"],a["path"],a["sha256"],int(a["required"])))}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return raw, hashlib.sha256(raw.encode()).hexdigest()

    def _sig(bundle_id: str, evidence_hash: str, role: str, path: str, sha: str, receipt_sha: str) -> str:
        msg = "|".join(["sha256_receipt_bind_v1", "ops-retrieval-ledger", "ops-retrieval-ledger:key-1", "ext://receipts", bundle_id, evidence_hash.lower(), role.lower(), path, sha.lower(), sha.lower(), receipt_sha.lower()])
        return hashlib.sha256(msg.encode()).hexdigest()

    def _retrieval(bundle_id: str, evidence_hash: str, rows: list[dict]) -> tuple[str, str]:
        payload = {"bundle_id": bundle_id, "evidence_hash_sha256": evidence_hash, "retrievals": sorted(rows, key=lambda r:(r["role"],r["path"],r["sha256"],r["content_sha256"],r["retrieval_receipt_sha256"],r.get("retrieval_receipt_attestor_id",""),r.get("retrieval_receipt_attestor_key_instance_id",""),r.get("retrieval_receipt_source",""),r.get("retrieval_receipt_signature",""),r.get("retrieval_receipt_signature_scheme","")))}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return raw, hashlib.sha256(raw.encode()).hexdigest()

    def _row(position_id: str, minute: int, lifecycle_mode: str) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "migration-key-lifecycle-evidence"})
        ev_hash = _attested_hash(payload)
        bundle_id = f"bundle-{position_id}"
        cert_sha = hashlib.sha256(b"chain").hexdigest()
        manifest_json, manifest_hash = _manifest(bundle_id, ev_hash, [
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "required": True},
            {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "required": True},
        ])
        retrievals=[]
        for role,path,sha,suffix in [("primary_evidence","files/proof.json",ev_hash,"proof"),("certificate_chain","certs/chain.der",cert_sha,"chain")]:
            rec=hashlib.sha256((position_id+suffix).encode()).hexdigest()
            retrievals.append({"role": role, "path": path, "sha256": sha, "available": True, "content_sha256": sha, "retrieval_receipt_sha256": rec, "retrieval_receipt_attestor_id": "ops-retrieval-ledger", "retrieval_receipt_attestor_key_instance_id": "ops-retrieval-ledger:key-1", "retrieval_receipt_source": "ext://receipts", "retrieval_receipt_signature_scheme": "sha256_receipt_bind_v1", "retrieval_receipt_signature": _sig(bundle_id, ev_hash, role, path, sha, rec)})
        retrieval_json, retrieval_hash = _retrieval(bundle_id, ev_hash, retrievals)
        lifecycle_json, lifecycle_hash = (None, None)
        governance_json, governance_hash = (None, None)
        governance_anchor_identity_json, governance_anchor_identity_hash = (None, None)
        identity_provenance_anchor_json, identity_provenance_anchor_hash = (None, None)
        anchor_key_instance_id = "ops-retrieval-key-ledger:key-1"
        if lifecycle_mode == "anchor-key-revoked":
            anchor_key_instance_id = "ops-retrieval-key-ledger:key-2"
        if lifecycle_mode == "anchor-key-indeterminate":
            anchor_key_instance_id = ""
        if lifecycle_mode in {"anchored", "invalid", "anchor-key-revoked", "anchor-key-indeterminate"}:
            lifecycle_json, lifecycle_hash = _lifecycle_evidence(bundle_id, ev_hash, anchor_key_instance_id or "ops-retrieval-key-ledger:key-1")
            if lifecycle_mode == "anchor-key-indeterminate":
                evidence_obj = json.loads(lifecycle_json)
                evidence_obj["anchor_key_instance_id"] = ""
                signed_payload = {k: v for k, v in evidence_obj.items() if k not in {"signature", "signature_scheme"}}
                payload_hash = hashlib.sha256(json.dumps(signed_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                evidence_obj["signature"] = hashlib.sha256("|".join(["sha256_key_lifecycle_bind_v1", "ops-retrieval-key-ledger", "", "ext://key-lifecycle", payload_hash]).encode()).hexdigest()
                lifecycle_json = json.dumps(evidence_obj, sort_keys=True, separators=(",", ":"))
                lifecycle_hash = hashlib.sha256(lifecycle_json.encode()).hexdigest()
            if lifecycle_mode == "invalid":
                lifecycle_hash = "0" * 64
            governance_json, governance_hash = _governance_dataset(bundle_id, ev_hash, [{"anchor_id": "ops-retrieval-key-ledger", "anchor_key_instance_id": "ops-retrieval-key-ledger:key-1", "lifecycle_state": "active"}])
            governance_anchor_identity_json, governance_anchor_identity_hash = _governance_anchor_identity_provenance(bundle_id, ev_hash, [{"governance_anchor_id": "ops-retrieval-anchor-governance-ledger", "lifecycle_state": "active"}])
            identity_provenance_anchor_json, identity_provenance_anchor_hash = _identity_provenance_anchor_provenance(bundle_id, ev_hash, [{"provenance_anchor_id": "ops-retrieval-anchor-identity-ledger", "lifecycle_state": "active"}])
            if lifecycle_mode == "anchor-key-revoked":
                governance_json, governance_hash = _governance_dataset(bundle_id, ev_hash, [{"anchor_id": "ops-retrieval-key-ledger", "anchor_key_instance_id": "ops-retrieval-key-ledger:key-2", "lifecycle_state": "revoked"}])
            if lifecycle_mode == "anchor-key-indeterminate":
                governance_json = None
                governance_hash = None
                governance_anchor_identity_json = None
                governance_anchor_identity_hash = None
                identity_provenance_anchor_json = None
                identity_provenance_anchor_hash = None
        return {
            "timestamp": ts.replace(minute=minute).isoformat(), "market_regime":"Trend Up","decision":"Paper Long","entry":"(220,220)","stop":"215","target":"225","projected_rr":2.0,"confidence":0.8,
            "reasoning":"manual-remediation","what_would_have_made_this_stand_aside":"n/a","position_size_btc":1.0,"position_id":position_id,"position_status":"closed",
            "executed_entry_price":220.0,"executed_notional_usd":220.0,"actual_outcome":"loss","actual_exit_price":219.0,"actual_pnl_usd":-1.0,"actual_pnl_basis":"net",
            "remediation_status":"manual_recovered","remediation_bundle_id":bundle_id,"remediation_evidence_ref":"sha256:"+ev_hash,
            "remediation_evidence_manifest_json":manifest_json,"remediation_evidence_manifest_hash_sha256":manifest_hash,
            "remediation_evidence_retrieval_proofs_json":retrieval_json,"remediation_evidence_retrieval_proofs_hash_sha256":retrieval_hash,
            "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_json":lifecycle_json,
            "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_hash_sha256":lifecycle_hash,
            "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_anchor_key_instance_id":anchor_key_instance_id,
            "remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_json":governance_json,
            "remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_hash_sha256":governance_hash,
            "remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_json":governance_anchor_identity_json,
            "remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_hash_sha256":governance_anchor_identity_hash,
            "remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_json":identity_provenance_anchor_json,
            "remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_hash_sha256":identity_provenance_anchor_hash,
            "remediation_evidence_payload":payload,"remediation_evidence_hash_sha256":ev_hash,
            "remediation_parent_lineage_tier":"legacy_lineage_rejected","remediation_operator":"auditor","remediation_reason":"migration key lifecycle evidence",
            "remediation_timestamp_utc":ts.replace(minute=minute).isoformat(),"remediation_external_source":"ext://exchange-archive",
            "remediation_signer_id":"ops-ledger","remediation_key_instance_id":"ops-ledger:key-1","remediation_key_epoch":1,"remediation_key_serial":"k1","remediation_issuer_id":"ops-root-ca",
            "remediation_certificate_chain_id":"ops-chain-v2","remediation_certificate_fingerprint_sha256":cert_path[0],"remediation_certificate_path_fingerprints":json.dumps(cert_path),
            "remediation_certificate_path_anchor_fingerprint_sha256":cert_path[-1],"remediation_certificate_der_hex_chain":json.dumps(der_chain),
            "remediation_certificate_link_signature_scheme":"der_tbs_rsa_sha256_v1","remediation_signature_scheme":"sha256_signer_key_bind_v1",
            "remediation_signature":_external_sig("ops-ledger","ops-ledger:key-1","ext://exchange-archive",ev_hash),
        }

    _write_rows(source,[
        _row("mig-lifecycle-anchored",0,"anchored"),
        _row("mig-lifecycle-anchor-key-revoked",1,"anchor-key-revoked"),
        _row("mig-lifecycle-anchor-key-indeterminate",2,"anchor-key-indeterminate"),
    ])

    cfg = StrategyConfig(
        taker_fee_rate=0.0, slippage_rate=0.0,
        remediation_external_signature_policy="require", remediation_trusted_signer_ids=("ops-ledger",), remediation_key_rotation_policy="allow_historical_non_active",
        remediation_issuer_chain_policy="require_key_instance_binding", remediation_key_instance_issuer_bindings={"ops-ledger:key-1":"ops-root-ca"}, remediation_issuer_root_fingerprints={"ops-root-ca":cert_path[-1]},
        remediation_certificate_link_policy="allow_metadata_only", remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1", remediation_trusted_root_fingerprints=(cert_path[-1],),
        remediation_evidence_manifest_policy="require_complete_manifest", remediation_evidence_retrieval_policy="require_retrieval_proofs", remediation_retrieval_receipt_auth_policy="require_authentic_receipts",
        remediation_trusted_retrieval_receipt_attestor_ids=("ops-retrieval-ledger",), remediation_retrieval_receipt_attestor_key_rotation_policy="require_active_key_lineage",
        remediation_trusted_retrieval_receipt_attestor_key_instances={"ops-retrieval-ledger":("ops-retrieval-ledger:key-1",)},
        remediation_retrieval_receipt_attestor_key_lifecycle_evidence_policy="require_anchored_evidence",
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_ids=("ops-retrieval-key-ledger",),
        remediation_retrieval_receipt_attestor_key_lifecycle_anchor_key_governance_policy="require_governed_anchor_keys",
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances={"ops-retrieval-key-ledger":("ops-retrieval-key-ledger:key-1",)},
        remediation_revoked_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances=("ops-retrieval-key-ledger:key-2",),
        remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_policy="require_anchored_dataset",
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_anchor_ids=("ops-retrieval-anchor-governance-ledger",),
        remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_policy="require_anchored_identity_provenance",
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_anchor_ids=("ops-retrieval-anchor-identity-ledger",),
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_ids=("ops-retrieval-anchor-governance-ledger",),
        remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_governance_policy="require_anchored_identity_provenance_anchor_governance",
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_anchor_ids=("ops-retrieval-anchor-identity-root-ledger",),
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_ids=("ops-retrieval-anchor-identity-ledger",),
    )

    accepted_src, rejected_src = load_validated_realized_rows(str(source), cfg)
    report = migrate_legacy_history(str(source), str(migrated), str(ambiguous), cfg)
    accepted_mig, rejected_mig = load_validated_realized_rows(str(migrated), cfg)
    assert report.total_rows == 3
    assert [r.entry.position_id for r in accepted_src] == ["mig-lifecycle-anchored"]
    assert [r.entry.position_id for r in accepted_mig] == ["mig-lifecycle-anchored"]
    assert sorted(r.reason for r in rejected_src) == sorted(r.reason for r in rejected_mig)
    summary = review_journal(str(migrated), cfg)
    assert summary["manual_remediated_attested_lineage_rows"] == 1
    assert summary["manual_remediation_revoked_rows"] == 1
    assert summary["manual_remediation_indeterminate_rows"] == 1
