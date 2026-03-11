import hashlib
import json
import subprocess
from datetime import datetime, timezone
import sys

from btc_rinse_repeat_v1.config import StrategyConfig, load_strategy_config
from btc_rinse_repeat_v1.journal import load_validated_realized_rows
from btc_rinse_repeat_v1.journal import validate_realized_row
from btc_rinse_repeat_v1.main import _apply_realized_outcomes_to_kill_switch
from btc_rinse_repeat_v1.main import main as runtime_main
from btc_rinse_repeat_v1.models import JournalEntry
from btc_rinse_repeat_v1.models import KillSwitchState
from btc_rinse_repeat_v1.runtime_state import RuntimeState, load_runtime_state, save_runtime_state
from btc_rinse_repeat_v1.review import review_journal


def _attested_hash(payload: str) -> str:
    canonical = json.dumps(json.loads(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _external_sig(signer_id: str, key_instance_id: str, source: str, evidence_hash: str, scheme: str = "sha256_signer_key_bind_v1") -> str:
    message = "|".join([scheme, signer_id.lower(), key_instance_id.lower(), source.lower(), evidence_hash.lower()])
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _key_lifecycle_sig(anchor_id: str, anchor_key_instance_id: str, source: str, evidence_hash: str, scheme: str = "sha256_key_lifecycle_bind_v1") -> str:
    message = "|".join([scheme, anchor_id.lower(), anchor_key_instance_id.lower(), source.lower(), evidence_hash.lower()])
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _key_lifecycle_evidence(bundle_id: str, evidence_hash: str, records: list[dict], source: str = "ext://key-lifecycle", anchor_id: str = "ops-retrieval-key-ledger", anchor_key_instance_id: str = "ops-retrieval-key-ledger:key-1") -> tuple[str, str]:
    payload = {
        "bundle_id": bundle_id,
        "evidence_hash_sha256": evidence_hash,
        "anchor_id": anchor_id,
        "anchor_key_instance_id": anchor_key_instance_id,
        "source": source,
        "records": sorted(
            [
                {
                    "attestor_id": str(r["attestor_id"]),
                    "key_instance_id": str(r["key_instance_id"]),
                    "lifecycle_state": str(r["lifecycle_state"]),
                    "revoked_before_utc": str(r.get("revoked_before_utc", "") or ""),
                }
                for r in records
            ],
            key=lambda r: (r["attestor_id"], r["key_instance_id"], r["lifecycle_state"], r["revoked_before_utc"]),
        ),
    }
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    payload["signature_scheme"] = "sha256_key_lifecycle_bind_v1"
    payload["signature"] = _key_lifecycle_sig(anchor_id, anchor_key_instance_id, source, payload_hash)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _anchor_governance_dataset(bundle_id: str, evidence_hash: str, records: list[dict], source: str = "ext://anchor-governance", governance_anchor_id: str = "ops-retrieval-anchor-governance-ledger") -> tuple[str, str]:
    payload = {
        "bundle_id": bundle_id,
        "evidence_hash_sha256": evidence_hash,
        "governance_anchor_id": governance_anchor_id,
        "source": source,
        "records": sorted(
            [
                {
                    "anchor_id": str(r["anchor_id"]),
                    "anchor_key_instance_id": str(r["anchor_key_instance_id"]),
                    "lifecycle_state": str(r["lifecycle_state"]),
                    "revoked_before_utc": str(r.get("revoked_before_utc", "") or ""),
                }
                for r in records
            ],
            key=lambda r: (r["anchor_id"], r["anchor_key_instance_id"], r["lifecycle_state"], r["revoked_before_utc"]),
        ),
    }
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    payload["signature_scheme"] = "sha256_anchor_governance_dataset_bind_v1"
    msg = "|".join(["sha256_anchor_governance_dataset_bind_v1", governance_anchor_id.lower(), source.lower(), payload_hash.lower()])
    payload["signature"] = hashlib.sha256(msg.encode("utf-8")).hexdigest()
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _governance_anchor_identity_provenance(bundle_id: str, evidence_hash: str, records: list[dict], source: str = "ext://governance-anchor-identity", provenance_anchor_id: str = "ops-retrieval-anchor-identity-ledger") -> tuple[str, str]:
    payload = {
        "bundle_id": bundle_id,
        "evidence_hash_sha256": evidence_hash,
        "provenance_anchor_id": provenance_anchor_id,
        "source": source,
        "records": sorted(
            [
                {
                    "governance_anchor_id": str(r["governance_anchor_id"]),
                    "lifecycle_state": str(r["lifecycle_state"]),
                    "revoked_before_utc": str(r.get("revoked_before_utc", "") or ""),
                }
                for r in records
            ],
            key=lambda r: (r["governance_anchor_id"], r["lifecycle_state"], r["revoked_before_utc"]),
        ),
    }
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    payload["signature_scheme"] = "sha256_governance_anchor_identity_bind_v1"
    msg = "|".join(["sha256_governance_anchor_identity_bind_v1", provenance_anchor_id.lower(), source.lower(), payload_hash.lower()])
    payload["signature"] = hashlib.sha256(msg.encode("utf-8")).hexdigest()
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _identity_provenance_anchor_provenance(bundle_id: str, evidence_hash: str, records: list[dict], source: str = "ext://identity-provenance-anchor", root_anchor_id: str = "ops-retrieval-anchor-identity-root-ledger") -> tuple[str, str]:
    payload = {
        "bundle_id": bundle_id,
        "evidence_hash_sha256": evidence_hash,
        "root_anchor_id": root_anchor_id,
        "source": source,
        "records": sorted(
            [
                {
                    "provenance_anchor_id": str(r["provenance_anchor_id"]),
                    "lifecycle_state": str(r["lifecycle_state"]),
                    "revoked_before_utc": str(r.get("revoked_before_utc", "") or ""),
                }
                for r in records
            ],
            key=lambda r: (r["provenance_anchor_id"], r["lifecycle_state"], r["revoked_before_utc"]),
        ),
    }
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    payload["signature_scheme"] = "sha256_identity_provenance_anchor_bind_v1"
    msg = "|".join(["sha256_identity_provenance_anchor_bind_v1", root_anchor_id.lower(), source.lower(), payload_hash.lower()])
    payload["signature"] = hashlib.sha256(msg.encode("utf-8")).hexdigest()
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_runtime_state_persists_last_run_and_kill_switch(tmp_path) -> None:
    path = tmp_path / "state.json"
    now = datetime.now(timezone.utc)
    kill = KillSwitchState(consecutive_losses=2, review_required=True)
    save_runtime_state(
        RuntimeState(last_run_utc=now, last_processed_outcome_ts=now, kill_switch=kill),
        str(path),
    )
    loaded = load_runtime_state(str(path))
    assert loaded.last_run_utc is not None
    assert loaded.last_run_utc.isoformat() == now.isoformat()
    assert loaded.last_processed_outcome_ts is not None
    assert loaded.kill_switch is not None
    assert loaded.kill_switch.consecutive_losses == 2
    assert loaded.kill_switch.review_required is True


def test_save_runtime_state_accepts_naive_datetimes_and_normalizes_to_utc(tmp_path) -> None:
    path = tmp_path / "state_naive.json"
    naive = datetime(2026, 1, 20, 0, 0)
    kill = KillSwitchState(consecutive_losses=1, stand_aside_until=naive, review_required=True)
    saved = save_runtime_state(
        RuntimeState(last_run_utc=naive, last_processed_outcome_ts=naive, kill_switch=kill),
        str(path),
    )

    assert saved.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["last_run_utc"] == "2026-01-20T00:00:00+00:00"
    assert raw["last_processed_outcome_ts"] == "2026-01-20T00:00:00+00:00"
    assert raw["kill_switch"]["stand_aside_until"] == "2026-01-20T00:00:00+00:00"


def test_save_runtime_state_is_atomic_when_replace_fails(monkeypatch, tmp_path) -> None:
    path = tmp_path / "state.json"
    original = RuntimeState(
        last_run_utc=datetime(2026, 1, 21, tzinfo=timezone.utc),
        last_processed_outcome_ts=datetime(2026, 1, 21, tzinfo=timezone.utc),
        kill_switch=KillSwitchState(consecutive_losses=1, review_required=False),
    )
    save_runtime_state(original, str(path))
    before = path.read_text(encoding="utf-8")

    replacement = RuntimeState(
        last_run_utc=datetime(2026, 1, 22, tzinfo=timezone.utc),
        last_processed_outcome_ts=datetime(2026, 1, 22, tzinfo=timezone.utc),
        kill_switch=KillSwitchState(consecutive_losses=2, review_required=True),
    )

    def fail_replace(self, _target):
        raise RuntimeError("simulated_replace_failure")

    monkeypatch.setattr("pathlib.Path.replace", fail_replace)

    try:
        save_runtime_state(replacement, str(path))
        assert False, "expected save failure"
    except RuntimeError as exc:
        assert "simulated_replace_failure" in str(exc)

    # original file remains intact
    after = path.read_text(encoding="utf-8")
    assert after == before


def test_load_runtime_state_fails_closed_on_corrupted_json(tmp_path) -> None:
    path = tmp_path / "state_bad.json"
    path.write_text("{not-json", encoding="utf-8")

    try:
        load_runtime_state(str(path))
        assert False, "expected fail-closed load error"
    except RuntimeError as exc:
        assert "invalid_runtime_state_file" in str(exc)


def test_load_runtime_state_normalizes_legacy_naive_timestamps_to_utc(tmp_path) -> None:
    path = tmp_path / "state_legacy.json"
    path.write_text(
        json.dumps(
            {
                "last_run_utc": "2026-01-25T00:00:00",
                "last_processed_outcome_ts": "2026-01-25T01:00:00",
                "kill_switch": {
                    "consecutive_losses": 1,
                    "stand_aside_until": "2026-01-25T02:00:00",
                    "review_required": True,
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_runtime_state(str(path))
    assert loaded.last_run_utc is not None and loaded.last_run_utc.tzinfo is not None
    assert loaded.last_processed_outcome_ts is not None and loaded.last_processed_outcome_ts.tzinfo is not None
    assert loaded.kill_switch is not None
    assert loaded.kill_switch.stand_aside_until is not None
    assert loaded.kill_switch.stand_aside_until.tzinfo is not None


def test_load_strategy_config_from_json(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "poll_interval_minutes": 12,
                "taker_fee_rate": 0.002,
                "remediation_revoked_signer_ids": ["ops-ledger"], "remediation_key_instance_revoked_ids": ["ops-ledger:key-1"], "remediation_trusted_issuer_ids": ["ops-root-ca"], "remediation_trusted_root_fingerprints": ["f" * 64], "remediation_key_instance_issuer_bindings": {"ops-ledger:key-1": "ops-root-ca"}, "remediation_issuer_root_fingerprints": {"ops-root-ca": "f" * 64},
                "remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_ids": ["ops-retrieval-key-ledger"],
                "remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances": {"ops-retrieval-key-ledger": ["ops-retrieval-key-ledger:key-1"]},
                "remediation_revoked_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances": ["ops-retrieval-key-ledger:key-2"],
                "remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_anchor_ids": ["ops-retrieval-anchor-governance-ledger"],
                "remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_anchor_ids": ["ops-retrieval-anchor-identity-ledger"],
                "remediation_trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_anchor_ids": ["ops-retrieval-anchor-identity-root-ledger"],
            }
        ),
        encoding="utf-8",
    )
    cfg = load_strategy_config(str(path))
    assert cfg.poll_interval_minutes == 12
    assert cfg.taker_fee_rate == 0.002
    assert cfg.remediation_revoked_signer_ids == ("ops-ledger",)
    assert cfg.remediation_key_instance_revoked_ids == ("ops-ledger:key-1",)
    assert cfg.remediation_trusted_issuer_ids == ("ops-root-ca",)
    assert cfg.remediation_key_instance_issuer_bindings == {"ops-ledger:key-1": "ops-root-ca"}
    assert cfg.remediation_trusted_root_fingerprints == ("f" * 64,)
    assert cfg.remediation_issuer_root_fingerprints == {"ops-root-ca": "f" * 64}
    assert cfg.remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_ids == ("ops-retrieval-key-ledger",)
    assert cfg.remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances == {"ops-retrieval-key-ledger": ("ops-retrieval-key-ledger:key-1",)}
    assert cfg.remediation_revoked_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances == ("ops-retrieval-key-ledger:key-2",)
    assert cfg.remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_anchor_ids == ("ops-retrieval-anchor-governance-ledger",)
    assert cfg.remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_anchor_ids == ("ops-retrieval-anchor-identity-ledger",)
    assert cfg.remediation_trusted_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_anchor_ids == ("ops-retrieval-anchor-identity-root-ledger",)


def test_runtime_restart_does_not_double_apply_accepted_outcomes(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    journal_path = tmp_path / "journal.jsonl"

    ts = datetime(2026, 1, 12, tzinfo=timezone.utc)
    row = JournalEntry(
        timestamp=ts,
        market_regime="Trend Up",
        decision="Paper Long",
        entry="(100,100)",
        stop="95",
        target="105",
        projected_rr=2.0,
        confidence=0.8,
        reasoning="x",
        what_would_have_made_this_stand_aside="x",
        position_size_btc=1.0,
        executed_entry_price=100.0,
        executed_notional_usd=100.0,
        actual_outcome="loss",
        actual_exit_price=95.0,
        actual_pnl_usd=-5.0,
        actual_pnl_basis="net",
    )
    journal_path.write_text(json.dumps(row.__dict__, default=str) + "\n", encoding="utf-8")

    cfg = StrategyConfig(kill_switch_consecutive_losses=2, taker_fee_rate=0.0, slippage_rate=0.0)

    accepted, _ = load_validated_realized_rows(str(journal_path), cfg)
    state = RuntimeState(last_run_utc=None, last_processed_outcome_ts=None, kill_switch=KillSwitchState())
    state.kill_switch, state.last_processed_outcome_ts = _apply_realized_outcomes_to_kill_switch(
        state.kill_switch,
        state.last_processed_outcome_ts,
        accepted,
        cfg,
    )
    assert state.kill_switch.consecutive_losses == 1

    save_runtime_state(state, str(state_path))
    restarted = load_runtime_state(str(state_path))
    restarted.kill_switch, restarted.last_processed_outcome_ts = _apply_realized_outcomes_to_kill_switch(
        restarted.kill_switch or KillSwitchState(),
        restarted.last_processed_outcome_ts,
        accepted,
        cfg,
    )

    assert restarted.kill_switch.consecutive_losses == 1


def test_runtime_restart_handles_legacy_naive_cursor_without_replay_crash(tmp_path) -> None:
    journal_path = tmp_path / "journal.jsonl"
    ts = datetime(2026, 1, 15, tzinfo=timezone.utc)
    row = JournalEntry(
        timestamp=ts,
        market_regime="Trend Up",
        decision="Paper Long",
        entry="(100,100)",
        stop="95",
        target="105",
        projected_rr=2.0,
        confidence=0.8,
        reasoning="x",
        what_would_have_made_this_stand_aside="x",
        position_size_btc=1.0,
        executed_entry_price=100.0,
        executed_notional_usd=100.0,
        actual_outcome="loss",
        actual_exit_price=95.0,
        actual_pnl_usd=-5.0,
        actual_pnl_basis="net",
    )
    journal_path.write_text(json.dumps(row.__dict__, default=str) + "\n", encoding="utf-8")

    cfg = StrategyConfig(kill_switch_consecutive_losses=2, taker_fee_rate=0.0, slippage_rate=0.0)
    accepted, _ = load_validated_realized_rows(str(journal_path), cfg)

    ks = KillSwitchState()
    # legacy naive cursor shape
    legacy_cursor = datetime(2026, 1, 14, 0, 0)
    ks, last_ts = _apply_realized_outcomes_to_kill_switch(ks, legacy_cursor, accepted, cfg)
    assert ks.consecutive_losses == 1
    assert last_ts is not None


def test_restart_reconstruction_matches_uninterrupted_replay_for_same_stream(tmp_path) -> None:
    cfg = StrategyConfig(kill_switch_consecutive_losses=3, taker_fee_rate=0.0, slippage_rate=0.0)
    t1 = datetime(2026, 1, 26, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 26, 1, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 26, 2, 0, tzinfo=timezone.utc)

    rows = [
        JournalEntry(
            timestamp=t1,
            market_regime="Trend Up",
            decision="Paper Long",
            entry="(100,100)",
            stop="95",
            target="105",
            projected_rr=2.0,
            confidence=0.8,
            reasoning="x",
            what_would_have_made_this_stand_aside="x",
            position_size_btc=1.0,
            executed_entry_price=100.0,
            executed_notional_usd=100.0,
            actual_outcome="loss",
            actual_exit_price=95.0,
            actual_pnl_usd=-5.0,
            actual_pnl_basis="net",
        ),
        JournalEntry(
            timestamp=t2,
            market_regime="Trend Up",
            decision="Paper Short",
            entry="(100,100)",
            stop="105",
            target="95",
            projected_rr=2.0,
            confidence=0.8,
            reasoning="x",
            what_would_have_made_this_stand_aside="x",
            position_size_btc=1.0,
            executed_entry_price=100.0,
            executed_notional_usd=100.0,
            actual_outcome="win",
            actual_exit_price=95.0,
            actual_pnl_usd=5.0,
            actual_pnl_basis="net",
        ),
        JournalEntry(
            timestamp=t3,
            market_regime="Trend Up",
            decision="Paper Long",
            entry="(100,100)",
            stop="95",
            target="105",
            projected_rr=2.0,
            confidence=0.8,
            reasoning="x",
            what_would_have_made_this_stand_aside="x",
            position_size_btc=1.0,
            executed_entry_price=100.0,
            executed_notional_usd=100.0,
            actual_outcome="loss",
            actual_exit_price=95.0,
            actual_pnl_usd=-5.0,
            actual_pnl_basis="net",
        ),
    ]

    journal_path = tmp_path / "stream.jsonl"
    with journal_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r.__dict__, default=str) + "\n")

    accepted, rejected = load_validated_realized_rows(str(journal_path), cfg)
    assert rejected == []

    uninterrupted_ks, uninterrupted_cursor = _apply_realized_outcomes_to_kill_switch(
        KillSwitchState(),
        None,
        accepted,
        cfg,
    )

    phase1 = [r for r in accepted if datetime.fromisoformat(str(r.entry.timestamp)) <= t2]
    phase2 = accepted
    restarted_ks, restarted_cursor = _apply_realized_outcomes_to_kill_switch(
        KillSwitchState(),
        None,
        phase1,
        cfg,
    )
    restarted_ks, restarted_cursor = _apply_realized_outcomes_to_kill_switch(
        restarted_ks,
        restarted_cursor,
        phase2,
        cfg,
    )

    assert restarted_ks.consecutive_losses == uninterrupted_ks.consecutive_losses
    assert restarted_ks.review_required == uninterrupted_ks.review_required
    assert restarted_cursor == uninterrupted_cursor


def test_main_entrypoint_restart_is_idempotent_when_no_new_accepted_rows(monkeypatch, tmp_path) -> None:
    ts = datetime(2026, 1, 16, tzinfo=timezone.utc)
    row = JournalEntry(
        timestamp=ts,
        market_regime="Trend Up",
        decision="Paper Long",
        entry="(100,100)",
        stop="95",
        target="105",
        projected_rr=2.0,
        confidence=0.8,
        reasoning="x",
        what_would_have_made_this_stand_aside="x",
        position_size_btc=1.0,
        executed_entry_price=100.0,
        executed_notional_usd=100.0,
        actual_outcome="loss",
        actual_exit_price=95.0,
        actual_pnl_usd=-5.0,
        actual_pnl_basis="net",
    )
    cfg = StrategyConfig(kill_switch_consecutive_losses=2, taker_fee_rate=0.0, slippage_rate=0.0)
    accepted, _ = load_validated_realized_rows(
        str(tmp_path / "j.jsonl"),
        cfg,
    )
    # inject one accepted row directly to isolate entrypoint state propagation
    from btc_rinse_repeat_v1.journal import validate_realized_row

    accepted = [validate_realized_row(row, cfg)]

    state_store = RuntimeState(last_run_utc=None, last_processed_outcome_ts=None, kill_switch=KillSwitchState())
    saved_states = []

    def fake_load_runtime_state(_path):
        return state_store

    def fake_save_runtime_state(state, _path):
        nonlocal state_store
        state_store = state
        saved_states.append((state.kill_switch.consecutive_losses, state.last_processed_outcome_ts))

    def fake_load_validated_realized_rows(_path, _cfg):
        return accepted, []

    def fake_should_poll(*args, **kwargs):
        return False

    def fake_fetch_latest_candles(_cfg):
        raise AssertionError("fetch_latest_candles should not be called when should_poll is False")

    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_strategy_config", lambda _p: cfg)
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_runtime_state", fake_load_runtime_state)
    monkeypatch.setattr("btc_rinse_repeat_v1.main.save_runtime_state", fake_save_runtime_state)
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_validated_realized_rows", fake_load_validated_realized_rows)
    monkeypatch.setattr("btc_rinse_repeat_v1.main.should_poll", fake_should_poll)
    monkeypatch.setattr("btc_rinse_repeat_v1.main.fetch_latest_candles", fake_fetch_latest_candles)

    monkeypatch.setattr(sys, "argv", ["prog", "--state-file", str(tmp_path / "state.json"), "--journal", str(tmp_path / "j.jsonl")])
    runtime_main()
    runtime_main()

    assert len(saved_states) == 2
    assert saved_states[0][0] == 1
    assert saved_states[1][0] == 1


def test_main_fails_closed_on_partial_persisted_state_without_cursor(monkeypatch, tmp_path) -> None:
    ts = datetime(2026, 1, 17, tzinfo=timezone.utc)
    row = JournalEntry(
        timestamp=ts,
        market_regime="Trend Up",
        decision="Paper Long",
        entry="(100,100)",
        stop="95",
        target="105",
        projected_rr=2.0,
        confidence=0.8,
        reasoning="x",
        what_would_have_made_this_stand_aside="x",
        position_size_btc=1.0,
        executed_entry_price=100.0,
        executed_notional_usd=100.0,
        actual_outcome="loss",
        actual_exit_price=95.0,
        actual_pnl_usd=-5.0,
        actual_pnl_basis="net",
    )
    cfg = StrategyConfig(kill_switch_consecutive_losses=2, taker_fee_rate=0.0, slippage_rate=0.0)
    from btc_rinse_repeat_v1.journal import validate_realized_row

    accepted = [validate_realized_row(row, cfg)]
    # incoherent persisted state: kill-switch already advanced but cursor absent
    bad_state = RuntimeState(
        last_run_utc=None,
        last_processed_outcome_ts=None,
        kill_switch=KillSwitchState(consecutive_losses=1, review_required=False),
    )

    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_strategy_config", lambda _p: cfg)
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_runtime_state", lambda _p: bad_state)
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_validated_realized_rows", lambda _p, _c: (accepted, []))
    monkeypatch.setattr("btc_rinse_repeat_v1.main.save_runtime_state", lambda *_a, **_k: None)
    monkeypatch.setattr("btc_rinse_repeat_v1.main.should_poll", lambda *_a, **_k: False)
    monkeypatch.setattr(sys, "argv", ["prog", "--state-file", str(tmp_path / "state.json"), "--journal", str(tmp_path / "j.jsonl")])

    try:
        runtime_main()
        assert False, "expected fail-closed runtime error"
    except RuntimeError as exc:
        assert "incoherent_runtime_state_missing_outcome_cursor" in str(exc)


def test_main_allows_non_pristine_kill_switch_without_cursor_when_no_accepted_rows(monkeypatch, tmp_path) -> None:
    cfg = StrategyConfig(kill_switch_consecutive_losses=2, taker_fee_rate=0.0, slippage_rate=0.0)
    state = RuntimeState(
        last_run_utc=None,
        last_processed_outcome_ts=None,
        kill_switch=KillSwitchState(consecutive_losses=1, review_required=False),
    )
    saved = []

    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_strategy_config", lambda _p: cfg)
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_runtime_state", lambda _p: state)
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_validated_realized_rows", lambda _p, _c: ([], []))
    monkeypatch.setattr("btc_rinse_repeat_v1.main.save_runtime_state", lambda s, _p: saved.append(s))
    monkeypatch.setattr("btc_rinse_repeat_v1.main.should_poll", lambda *_a, **_k: False)
    monkeypatch.setattr(sys, "argv", ["prog", "--state-file", str(tmp_path / "state.json"), "--journal", str(tmp_path / "j.jsonl")])

    runtime_main()
    assert len(saved) == 1
    assert saved[0].kill_switch is not None
    assert saved[0].kill_switch.consecutive_losses == 1


def test_main_fails_closed_before_consumption_when_runtime_state_file_corrupted(monkeypatch, tmp_path) -> None:
    bad_state = tmp_path / "bad_state.json"
    bad_state.write_text("{not-json", encoding="utf-8")

    consumed = {"called": False}

    def fake_loader(*args, **kwargs):
        consumed["called"] = True
        return [], []

    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_validated_realized_rows", fake_loader)
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_strategy_config", lambda _p: StrategyConfig())
    monkeypatch.setattr(sys, "argv", ["prog", "--state-file", str(bad_state), "--journal", str(tmp_path / "j.jsonl")])

    try:
        runtime_main()
        assert False, "expected fail-closed runtime error"
    except RuntimeError as exc:
        assert "invalid_runtime_state_file" in str(exc)

    assert consumed["called"] is False


def test_runtime_state_persists_processed_closed_position_ids(tmp_path) -> None:
    path = tmp_path / "state_positions.json"
    save_runtime_state(
        RuntimeState(
            last_run_utc=datetime(2026, 2, 1, tzinfo=timezone.utc),
            last_processed_outcome_ts=datetime(2026, 2, 1, tzinfo=timezone.utc),
            kill_switch=KillSwitchState(),
            processed_closed_position_ids=["p-2", "p-1", "p-1"],
        ),
        str(path),
    )
    loaded = load_runtime_state(str(path))
    assert loaded.processed_closed_position_ids == ["p-1", "p-2"]


def test_apply_realized_outcomes_uses_position_id_to_avoid_double_apply_on_same_position() -> None:
    cfg = StrategyConfig(kill_switch_consecutive_losses=10, taker_fee_rate=0.0, slippage_rate=0.0)
    t1 = datetime(2026, 2, 5, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 2, 5, 1, 0, tzinfo=timezone.utc)

    row1 = JournalEntry(
        timestamp=t1, market_regime="Trend Up", decision="Paper Long", entry="(100,100)", stop="95", target="105",
        projected_rr=2.0, confidence=0.8, reasoning="x", what_would_have_made_this_stand_aside="x",
        position_size_btc=1.0, position_id="pos-1", position_status="closed", executed_entry_price=100.0,
        executed_notional_usd=100.0, actual_outcome="loss", actual_exit_price=95.0, actual_pnl_usd=-5.0, actual_pnl_basis="net",
    )
    row2 = JournalEntry(**{**row1.__dict__, "timestamp": t2, "actual_pnl_usd": -5.0})

    accepted = [validate_realized_row(row1, cfg), validate_realized_row(row2, cfg)]
    ks = KillSwitchState()
    processed = set()
    ks, cursor = _apply_realized_outcomes_to_kill_switch(ks, None, accepted, cfg, processed_closed_position_ids=processed)
    assert ks.consecutive_losses == 1
    assert "pos-1" in processed
    assert cursor is not None


def test_apply_realized_outcomes_skips_non_closed_position_rows_even_if_realized() -> None:
    cfg = StrategyConfig(kill_switch_consecutive_losses=10, taker_fee_rate=0.0, slippage_rate=0.0)
    t1 = datetime(2026, 2, 6, 0, 0, tzinfo=timezone.utc)
    row = JournalEntry(
        timestamp=t1, market_regime="Trend Up", decision="Paper Long", entry="(100,100)", stop="95", target="105",
        projected_rr=2.0, confidence=0.8, reasoning="x", what_would_have_made_this_stand_aside="x",
        position_size_btc=1.0, position_id="pos-open", position_status="open", executed_entry_price=100.0,
        executed_notional_usd=100.0, actual_outcome="loss", actual_exit_price=95.0, actual_pnl_usd=-5.0, actual_pnl_basis="net",
    )
    accepted = [validate_realized_row(row, cfg)]
    ks, cursor = _apply_realized_outcomes_to_kill_switch(
        KillSwitchState(),
        None,
        accepted,
        cfg,
        processed_closed_position_ids=set(),
    )
    assert ks.consecutive_losses == 0
    assert cursor is None


def test_main_fails_closed_when_processed_positions_exist_without_cursor(monkeypatch, tmp_path) -> None:
    state = RuntimeState(last_run_utc=None, last_processed_outcome_ts=None, kill_switch=KillSwitchState(), processed_closed_position_ids=["pos-1"])
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_runtime_state", lambda _p: state)
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_strategy_config", lambda _p: StrategyConfig())
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_validated_realized_rows", lambda _p, _c: ([], []))
    monkeypatch.setattr("btc_rinse_repeat_v1.main.save_runtime_state", lambda *_a, **_k: None)

    old = sys.argv
    sys.argv = ["prog"]
    try:
        try:
            runtime_main()
            assert False, "expected incoherent runtime state failure"
        except RuntimeError as exc:
            assert "incoherent_runtime_state_missing_outcome_cursor" in str(exc)
    finally:
        sys.argv = old


def test_runtime_state_persists_processed_legacy_event_keys(tmp_path) -> None:
    path = tmp_path / "state_legacy_keys.json"
    save_runtime_state(
        RuntimeState(
            last_run_utc=datetime(2026, 2, 2, tzinfo=timezone.utc),
            last_processed_outcome_ts=datetime(2026, 2, 2, tzinfo=timezone.utc),
            kill_switch=KillSwitchState(),
            processed_legacy_event_keys=["k-2", "k-1", "k-1"],
        ),
        str(path),
    )
    loaded = load_runtime_state(str(path))
    assert loaded.processed_legacy_event_keys == ["k-1", "k-2"]


def test_apply_realized_outcomes_legacy_rows_replay_once_with_upgrade_policy() -> None:
    cfg = StrategyConfig(kill_switch_consecutive_losses=10, taker_fee_rate=0.0, slippage_rate=0.0, runtime_legacy_row_policy="upgrade_deterministic")
    t1 = datetime(2026, 2, 7, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 2, 7, 1, 0, tzinfo=timezone.utc)

    base = dict(
        market_regime="Trend Up", decision="Paper Long", entry="(100,100)", stop="95", target="105", projected_rr=2.0, confidence=0.8,
        reasoning="x", what_would_have_made_this_stand_aside="x", position_size_btc=1.0, executed_entry_price=100.0, executed_notional_usd=100.0,
        actual_outcome="loss", actual_exit_price=95.0, actual_pnl_usd=-5.0, actual_pnl_basis="net",
    )
    row1 = JournalEntry(timestamp=t1, **base)
    row2 = JournalEntry(timestamp=t2, **base)
    accepted = [validate_realized_row(row1, cfg), validate_realized_row(row1, cfg), validate_realized_row(row2, cfg)]
    ks, _ = _apply_realized_outcomes_to_kill_switch(
        KillSwitchState(), None, accepted, cfg, processed_closed_position_ids=set(), processed_legacy_event_keys=set()
    )
    assert ks.consecutive_losses == 2


def test_apply_realized_outcomes_legacy_rows_rejected_by_policy() -> None:
    cfg = StrategyConfig(kill_switch_consecutive_losses=10, taker_fee_rate=0.0, slippage_rate=0.0, runtime_legacy_row_policy="reject")
    t1 = datetime(2026, 2, 8, 0, 0, tzinfo=timezone.utc)
    row = JournalEntry(
        timestamp=t1, market_regime="Trend Up", decision="Paper Long", entry="(100,100)", stop="95", target="105", projected_rr=2.0,
        confidence=0.8, reasoning="x", what_would_have_made_this_stand_aside="x", position_size_btc=1.0, executed_entry_price=100.0,
        executed_notional_usd=100.0, actual_outcome="loss", actual_exit_price=95.0, actual_pnl_usd=-5.0, actual_pnl_basis="net",
    )
    accepted = [validate_realized_row(row, cfg)]
    ks, cursor = _apply_realized_outcomes_to_kill_switch(
        KillSwitchState(), None, accepted, cfg, processed_closed_position_ids=set(), processed_legacy_event_keys=set()
    )
    assert ks.consecutive_losses == 0
    assert cursor is None


def test_main_fails_closed_when_processed_legacy_keys_exist_without_cursor(monkeypatch, tmp_path) -> None:
    state = RuntimeState(last_run_utc=None, last_processed_outcome_ts=None, kill_switch=KillSwitchState(), processed_legacy_event_keys=["k-1"])
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_runtime_state", lambda _p: state)
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_strategy_config", lambda _p: StrategyConfig())
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_validated_realized_rows", lambda _p, _c: ([], []))
    monkeypatch.setattr("btc_rinse_repeat_v1.main.save_runtime_state", lambda *_a, **_k: None)

    old = sys.argv
    sys.argv = ["prog"]
    try:
        try:
            runtime_main()
            assert False, "expected incoherent runtime state failure"
        except RuntimeError as exc:
            assert "incoherent_runtime_state_missing_outcome_cursor" in str(exc)
    finally:
        sys.argv = old


def test_runtime_replay_rejects_low_context_legacy_rows_by_policy() -> None:
    cfg = StrategyConfig(
        kill_switch_consecutive_losses=10,
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        runtime_legacy_row_policy="upgrade_deterministic",
        runtime_low_context_legacy_policy="reject",
        runtime_sparse_history_policy="reject",
    )
    ts = datetime(2026, 8, 1, tzinfo=timezone.utc)

    low_context = JournalEntry(
        timestamp=ts,
        market_regime="Trend Up",
        decision="Paper Long",
        entry="(100,100)",
        stop="95",
        target="105",
        projected_rr=2.0,
        confidence=0.8,
        reasoning="x",
        what_would_have_made_this_stand_aside="x",
        position_size_btc=1.0,
        executed_entry_price=100.0,
        executed_notional_usd=100.0,
        actual_outcome="loss",
        actual_exit_price=99.0,
        actual_pnl_usd=-1.0,
        actual_pnl_basis="net",
    )

    from btc_rinse_repeat_v1.journal import validate_realized_row

    accepted = [validate_realized_row(low_context, cfg)]
    ks, _ = _apply_realized_outcomes_to_kill_switch(
        KillSwitchState(),
        None,
        accepted,
        cfg,
        processed_closed_position_ids=set(),
        processed_legacy_event_keys=set(),
    )
    assert ks.consecutive_losses == 0


def test_runtime_replays_manual_remediated_rows_even_with_strict_legacy_policies() -> None:
    cfg = StrategyConfig(
        kill_switch_consecutive_losses=10,
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        runtime_legacy_row_policy="reject",
        runtime_low_context_legacy_policy="reject",
        runtime_sparse_history_policy="reject",
    )
    ts = datetime(2026, 8, 4, tzinfo=timezone.utc)
    row = JournalEntry(
        timestamp=ts,
        market_regime="Trend Up",
        decision="Paper Long",
        entry="(100,100)",
        stop="95",
        target="105",
        projected_rr=2.0,
        confidence=0.8,
        reasoning="manual-remediation",
        what_would_have_made_this_stand_aside="n/a",
        position_size_btc=1.0,
        position_id="manual-pos-runtime",
        position_status="closed",
        position_open_timestamp=ts,
        executed_entry_price=100.0,
        executed_notional_usd=100.0,
        actual_outcome="loss",
        actual_exit_price=99.0,
        actual_pnl_usd=-1.0,
        actual_pnl_basis="net",
        remediation_status="manual_recovered",
        remediation_bundle_id="bundle-runtime",
        remediation_evidence_ref="sha256:" + _attested_hash("{\"ticket\":55,\"proof\":\"runtime\"}"),
        remediation_evidence_payload="{\"ticket\":55,\"proof\":\"runtime\"}",
        remediation_evidence_hash_sha256=_attested_hash("{\"ticket\":55,\"proof\":\"runtime\"}"),
        remediation_parent_lineage_tier="low_context_legacy_event_only",
        remediation_operator="auditor",
        remediation_reason="broker statement",
        remediation_timestamp_utc=ts,
        remediation_external_source="ext://runtime-proof",
        remediation_signer_id="ops-ledger",
        remediation_key_instance_id="ops-ledger:key-1",
        remediation_key_epoch=1,
        remediation_key_serial="k1",
        remediation_issuer_id="ops-root-ca",
        remediation_certificate_chain_id="ops-chain-v1",
        remediation_certificate_fingerprint_sha256="a" * 64,
        remediation_certificate_path_fingerprints=json.dumps(["a" * 64, "e" * 64, "f" * 64]),
        remediation_certificate_path_anchor_fingerprint_sha256="f" * 64,
        remediation_signature_scheme="sha256_signer_key_bind_v1",
        remediation_signature=_external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", _attested_hash("{\"ticket\":55,\"proof\":\"runtime\"}")),
    )
    accepted = [validate_realized_row(row, cfg)]
    ks, cursor = _apply_realized_outcomes_to_kill_switch(
        KillSwitchState(),
        None,
        accepted,
        cfg,
        processed_closed_position_ids=set(),
        processed_legacy_event_keys=set(),
    )
    assert ks.consecutive_losses == 1
    assert cursor == ts


def test_runtime_does_not_replay_when_remediation_attestation_invalid() -> None:
    cfg = StrategyConfig(kill_switch_consecutive_losses=10, taker_fee_rate=0.0, slippage_rate=0.0)
    ts = datetime(2026, 8, 7, tzinfo=timezone.utc)
    row = JournalEntry(
        timestamp=ts,
        market_regime="Trend Up",
        decision="Paper Long",
        entry="(100,100)",
        stop="95",
        target="105",
        projected_rr=2.0,
        confidence=0.8,
        reasoning="manual-remediation",
        what_would_have_made_this_stand_aside="n/a",
        position_size_btc=1.0,
        position_id="manual-pos-runtime-bad",
        position_status="closed",
        position_open_timestamp=ts,
        executed_entry_price=100.0,
        executed_notional_usd=100.0,
        actual_outcome="loss",
        actual_exit_price=99.0,
        actual_pnl_usd=-1.0,
        actual_pnl_basis="net",
        remediation_status="manual_recovered",
        remediation_bundle_id="bundle-runtime-bad",
        remediation_evidence_ref="sha256:" + ("0" * 64),
        remediation_evidence_payload='{"ticket":77,"proof":"bad"}',
        remediation_evidence_hash_sha256="0" * 64,
        remediation_parent_lineage_tier="legacy_lineage_rejected",
        remediation_operator="auditor",
        remediation_reason="manual proof pack",
        remediation_timestamp_utc=ts,
        remediation_external_source="ext://runtime-proof",
        remediation_signer_id="ops-ledger",
        remediation_key_instance_id="ops-ledger:key-1",
        remediation_key_epoch=1,
        remediation_key_serial="k1",
        remediation_issuer_id="ops-root-ca",
        remediation_certificate_chain_id="ops-chain-v1",
        remediation_certificate_fingerprint_sha256="a" * 64,
        remediation_certificate_path_fingerprints=json.dumps(["a" * 64, "e" * 64, "f" * 64]),
        remediation_certificate_path_anchor_fingerprint_sha256="f" * 64,
        remediation_signature_scheme="sha256_signer_key_bind_v1",
        remediation_signature="0" * 64,
    )
    accepted, rejected = load_validated_realized_rows(
        path="/does/not/exist", config=cfg
    )
    assert accepted == [] and rejected == []
    validated, invalid = [], []
    from btc_rinse_repeat_v1.journal import validate_realized_row, normalize_legacy_position_lineage
    validated.append(validate_realized_row(row, cfg))
    accepted_rows, rejected_rows = normalize_legacy_position_lineage(validated)
    invalid.extend(rejected_rows)
    assert accepted_rows == []
    assert len([r for r in invalid if r.reason == "manual_remediation_attestation_hash_mismatch"]) == 1


def test_runtime_rejects_internal_only_when_external_required() -> None:
    cfg = StrategyConfig(kill_switch_consecutive_losses=10, taker_fee_rate=0.0, slippage_rate=0.0, remediation_external_signature_policy="require")
    ts = datetime(2026, 8, 10, tzinfo=timezone.utc)
    payload = '{"ticket":101,"proof":"runtime"}'
    ev_hash = _attested_hash(payload)
    row = JournalEntry(
        timestamp=ts,
        market_regime="Trend Up",
        decision="Paper Long",
        entry="(100,100)",
        stop="95",
        target="105",
        projected_rr=2.0,
        confidence=0.8,
        reasoning="manual-remediation",
        what_would_have_made_this_stand_aside="n/a",
        position_size_btc=1.0,
        position_id="manual-pos-runtime-int",
        position_status="closed",
        position_open_timestamp=ts,
        executed_entry_price=100.0,
        executed_notional_usd=100.0,
        actual_outcome="loss",
        actual_exit_price=99.0,
        actual_pnl_usd=-1.0,
        actual_pnl_basis="net",
        remediation_status="manual_recovered",
        remediation_bundle_id="bundle-runtime-int",
        remediation_evidence_ref="sha256:" + ev_hash,
        remediation_evidence_payload=payload,
        remediation_evidence_hash_sha256=ev_hash,
        remediation_parent_lineage_tier="legacy_lineage_rejected",
        remediation_operator="auditor",
        remediation_reason="manual proof pack",
        remediation_timestamp_utc=ts,
    )
    from btc_rinse_repeat_v1.journal import validate_realized_row, normalize_legacy_position_lineage
    accepted_rows, rejected_rows = normalize_legacy_position_lineage([validate_realized_row(row, cfg)], config=cfg)
    assert accepted_rows == []
    assert len([r for r in rejected_rows if r.reason == "manual_remediation_external_signature_missing"]) == 1


def test_runtime_revalidation_excludes_previously_accepted_row_after_signer_revocation() -> None:
    ts = datetime(2026, 8, 12, tzinfo=timezone.utc)
    payload = '{"ticket":301,"proof":"runtime"}'
    ev_hash = _attested_hash(payload)
    row = JournalEntry(
        timestamp=ts,
        market_regime="Trend Up",
        decision="Paper Long",
        entry="(100,100)",
        stop="95",
        target="105",
        projected_rr=2.0,
        confidence=0.8,
        reasoning="manual-remediation",
        what_would_have_made_this_stand_aside="n/a",
        position_size_btc=1.0,
        position_id="manual-pos-runtime-revoke",
        position_status="closed",
        position_open_timestamp=ts,
        executed_entry_price=100.0,
        executed_notional_usd=100.0,
        actual_outcome="loss",
        actual_exit_price=99.0,
        actual_pnl_usd=-1.0,
        actual_pnl_basis="net",
        remediation_status="manual_recovered",
        remediation_bundle_id="bundle-runtime-revoke",
        remediation_evidence_ref="sha256:" + ev_hash,
        remediation_evidence_payload=payload,
        remediation_evidence_hash_sha256=ev_hash,
        remediation_parent_lineage_tier="legacy_lineage_rejected",
        remediation_operator="auditor",
        remediation_reason="manual proof pack",
        remediation_timestamp_utc=ts,
        remediation_external_source="ext://runtime-proof",
        remediation_signer_id="ops-ledger",
        remediation_key_instance_id="ops-ledger:key-1",
        remediation_key_epoch=1,
        remediation_key_serial="k1",
        remediation_issuer_id="ops-root-ca",
        remediation_certificate_chain_id="ops-chain-v1",
        remediation_certificate_fingerprint_sha256="a" * 64,
        remediation_certificate_path_fingerprints=json.dumps(["a" * 64, "e" * 64, "f" * 64]),
        remediation_certificate_path_anchor_fingerprint_sha256="f" * 64,
        remediation_signature_scheme="sha256_signer_key_bind_v1",
        remediation_signature=_external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", ev_hash),
    )

    cfg_valid = StrategyConfig(kill_switch_consecutive_losses=10, taker_fee_rate=0.0, slippage_rate=0.0)
    from btc_rinse_repeat_v1.journal import validate_realized_row, normalize_legacy_position_lineage
    accepted_valid, rejected_valid = normalize_legacy_position_lineage([validate_realized_row(row, cfg_valid)], config=cfg_valid)
    assert len(accepted_valid) == 1
    assert rejected_valid == []

    cfg_revoked = StrategyConfig(
        kill_switch_consecutive_losses=10,
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_revoked_signer_ids=("ops-ledger",),
    )
    accepted_revoked, rejected_revoked = normalize_legacy_position_lineage([validate_realized_row(row, cfg_revoked)], config=cfg_revoked)
    assert accepted_revoked == []
    assert len([r for r in rejected_revoked if r.reason == "manual_remediation_key_instance_revoked"]) == 1


def test_runtime_rejects_non_active_key_instance_under_rotation_policy() -> None:
    ts = datetime(2026, 8, 14, tzinfo=timezone.utc)
    payload = '{"ticket":601,"proof":"runtime"}'
    ev_hash = _attested_hash(payload)
    row = JournalEntry(
        timestamp=ts,
        market_regime="Trend Up",
        decision="Paper Long",
        entry="(100,100)",
        stop="95",
        target="105",
        projected_rr=2.0,
        confidence=0.8,
        reasoning="manual-remediation",
        what_would_have_made_this_stand_aside="n/a",
        position_size_btc=1.0,
        position_id="manual-pos-runtime-rot",
        position_status="closed",
        position_open_timestamp=ts,
        executed_entry_price=100.0,
        executed_notional_usd=100.0,
        actual_outcome="loss",
        actual_exit_price=99.0,
        actual_pnl_usd=-1.0,
        actual_pnl_basis="net",
        remediation_status="manual_recovered",
        remediation_bundle_id="bundle-runtime-rot",
        remediation_evidence_ref="sha256:" + ev_hash,
        remediation_evidence_payload=payload,
        remediation_evidence_hash_sha256=ev_hash,
        remediation_parent_lineage_tier="legacy_lineage_rejected",
        remediation_operator="auditor",
        remediation_reason="manual proof pack",
        remediation_timestamp_utc=ts,
        remediation_external_source="ext://runtime-proof",
        remediation_signer_id="ops-ledger",
        remediation_key_instance_id="ops-ledger:key-1",
        remediation_key_epoch=1,
        remediation_key_serial="k1",
        remediation_issuer_id="ops-root-ca",
        remediation_certificate_chain_id="ops-chain-v1",
        remediation_certificate_fingerprint_sha256="a" * 64,
        remediation_certificate_path_fingerprints=json.dumps(["a" * 64, "e" * 64, "f" * 64]),
        remediation_certificate_path_anchor_fingerprint_sha256="f" * 64,
        remediation_signature_scheme="sha256_signer_key_bind_v1",
        remediation_signature=_external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", ev_hash),
    )
    cfg = StrategyConfig(
        kill_switch_consecutive_losses=10,
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_signer_active_key_instances={"ops-ledger": ("ops-ledger:key-2",)},
        remediation_key_rotation_policy="require_active_key_lineage",
    )
    from btc_rinse_repeat_v1.journal import validate_realized_row, normalize_legacy_position_lineage
    accepted, rejected = normalize_legacy_position_lineage([validate_realized_row(row, cfg)], config=cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "manual_remediation_key_instance_not_active"]) == 1


def test_runtime_rejects_row_when_issuer_revoked() -> None:
    ts = datetime(2026, 8, 17, tzinfo=timezone.utc)
    payload = '{"ticket":703,"proof":"runtime"}'
    ev_hash = _attested_hash(payload)
    row = JournalEntry(
        timestamp=ts,
        market_regime="Trend Up",
        decision="Paper Long",
        entry="(100,100)",
        stop="95",
        target="105",
        projected_rr=2.0,
        confidence=0.8,
        reasoning="manual-remediation",
        what_would_have_made_this_stand_aside="n/a",
        position_size_btc=1.0,
        position_id="manual-pos-runtime-issuer",
        position_status="closed",
        position_open_timestamp=ts,
        executed_entry_price=100.0,
        executed_notional_usd=100.0,
        actual_outcome="loss",
        actual_exit_price=99.0,
        actual_pnl_usd=-1.0,
        actual_pnl_basis="net",
        remediation_status="manual_recovered",
        remediation_bundle_id="bundle-runtime-issuer",
        remediation_evidence_ref="sha256:" + ev_hash,
        remediation_evidence_payload=payload,
        remediation_evidence_hash_sha256=ev_hash,
        remediation_parent_lineage_tier="legacy_lineage_rejected",
        remediation_operator="auditor",
        remediation_reason="manual proof pack",
        remediation_timestamp_utc=ts,
        remediation_external_source="ext://runtime-proof",
        remediation_signer_id="ops-ledger",
        remediation_key_instance_id="ops-ledger:key-1",
        remediation_key_epoch=1,
        remediation_key_serial="k1",
        remediation_issuer_id="ops-root-ca",
        remediation_certificate_chain_id="ops-chain-v1",
        remediation_certificate_fingerprint_sha256="a" * 64,
        remediation_certificate_path_fingerprints=json.dumps(["a" * 64, "e" * 64, "f" * 64]),
        remediation_certificate_path_anchor_fingerprint_sha256="f" * 64,
        remediation_signature_scheme="sha256_signer_key_bind_v1",
        remediation_signature=_external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", ev_hash),
    )
    cfg = StrategyConfig(
        kill_switch_consecutive_losses=10,
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        remediation_revoked_issuer_ids=("ops-root-ca",),
    )
    from btc_rinse_repeat_v1.journal import validate_realized_row, normalize_legacy_position_lineage
    accepted, rejected = normalize_legacy_position_lineage([validate_realized_row(row, cfg)], config=cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "manual_remediation_issuer_revoked"]) == 1



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
    msg = "|".join([scheme.lower(), chain_id.lower(), parent_fingerprint.lower(), child_fingerprint.lower()])
    digest_int = int(hashlib.sha256(msg.encode("utf-8")).hexdigest(), 16)
    sig_int = pow(digest_int % modulus_n, private_key_d, modulus_n)
    return format(sig_int, "x")


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


def _manifest_json_hash(bundle_id: str, evidence_hash: str, artifacts: list[dict]) -> tuple[str, str]:
    normalized = [
        {
            "role": str(a["role"]).strip().lower(),
            "path": str(a["path"]).strip(),
            "sha256": str(a["sha256"]).strip().lower(),
            "required": bool(a["required"]),
        }
        for a in artifacts
    ]
    payload = {
        "bundle_id": str(bundle_id).strip(),
        "evidence_hash_sha256": str(evidence_hash).strip().lower(),
        "artifacts": sorted(normalized, key=lambda a: (a["role"], a["path"], a["sha256"], int(a["required"]))),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _retrieval_json_hash(bundle_id: str, evidence_hash: str, retrievals: list[dict]) -> tuple[str, str]:
    normalized = [
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
    ]
    payload = {
        "bundle_id": str(bundle_id).strip(),
        "evidence_hash_sha256": str(evidence_hash).strip().lower(),
        "retrievals": sorted(
            normalized,
            key=lambda r: (
                r["role"],
                r["path"],
                r["sha256"],
                r["content_sha256"],
                r["retrieval_receipt_sha256"],
                r["retrieval_receipt_attestor_id"],
                r["retrieval_receipt_attestor_key_instance_id"],
                r["retrieval_receipt_source"],
                r["retrieval_receipt_signature"],
                r["retrieval_receipt_signature_scheme"],
            ),
        ),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _retrieval_receipt_sig(
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
    scheme: str = "sha256_receipt_bind_v1",
) -> str:
    msg = "|".join([
        scheme.lower(),
        attestor_id.lower(),
        key_instance_id.lower(),
        source.lower(),
        bundle_id,
        evidence_hash.lower(),
        role.lower(),
        path,
        artifact_sha.lower(),
        content_sha.lower(),
        receipt_sha.lower(),
    ])
    return hashlib.sha256(msg.encode()).hexdigest()


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


def test_runtime_and_review_agree_on_broken_vs_valid_certificate_link_trust(tmp_path, monkeypatch) -> None:
    journal_path = tmp_path / "runtime_review_cert_links.jsonl"
    state_path = tmp_path / "runtime_state.json"
    ts = datetime(2026, 9, 1, tzinfo=timezone.utc)
    chain_id = "ops-chain-v1"
    cert_path = ["a" * 64, "e" * 64, "f" * 64]
    inter1_n, inter1_e, inter1_d = _issuer_keypair(31)
    root_n, root_e, root_d = _issuer_keypair(37)

    def _row(position_id: str, outcome: str, pnl: float, minute: int, link_sigs: list[str], cert_objects: str) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "runtime"})
        ev_hash = _attested_hash(payload)
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
            "actual_outcome": outcome,
            "actual_exit_price": 221.0 if outcome == "win" else 219.0,
            "actual_pnl_usd": pnl,
            "actual_pnl_basis": "net",
            "remediation_status": "manual_recovered",
            "remediation_bundle_id": f"bundle-{position_id}",
            "remediation_evidence_ref": "sha256:" + ev_hash,
            "remediation_evidence_payload": payload,
            "remediation_evidence_hash_sha256": ev_hash,
            "remediation_parent_lineage_tier": "legacy_lineage_rejected",
            "remediation_operator": "auditor",
            "remediation_reason": "manual proof pack",
            "remediation_timestamp_utc": ts.replace(minute=minute).isoformat(),
            "remediation_external_source": "ext://runtime-proof",
            "remediation_signer_id": "ops-ledger",
            "remediation_key_instance_id": "ops-ledger:key-1",
            "remediation_key_epoch": 1,
            "remediation_key_serial": "k1",
            "remediation_issuer_id": "ops-root-ca",
            "remediation_certificate_chain_id": chain_id,
            "remediation_certificate_fingerprint_sha256": cert_path[0],
            "remediation_certificate_path_fingerprints": json.dumps(cert_path),
            "remediation_certificate_path_anchor_fingerprint_sha256": cert_path[-1],
            "remediation_certificate_objects": cert_objects,
            "remediation_certificate_link_signatures": json.dumps(link_sigs),
            "remediation_certificate_link_signature_scheme": "inprocess_rsa_raw_sha256_v1",
            "remediation_signature_scheme": "sha256_signer_key_bind_v1",
            "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", ev_hash),
        }

    rows = [
        _row("cert-valid", "win", 1.0, 0, [
            _cert_link_sig(inter1_d, inter1_n, chain_id, cert_path[1], cert_path[0]),
            _cert_link_sig(root_d, root_n, chain_id, cert_path[2], cert_path[1]),
        ], _certificate_objects(cert_path, inter1_n, inter1_e, root_n, root_e, ts.replace(year=2025).isoformat(), ts.replace(year=2027).isoformat())),
        _row("cert-broken", "loss", -1.0, 1, [
            _cert_link_sig(root_d, root_n, chain_id, cert_path[2], cert_path[1]),
            _cert_link_sig(root_d, root_n, chain_id, cert_path[2], cert_path[1]),
        ], _certificate_objects(cert_path, inter1_n, inter1_e, root_n, root_e, ts.replace(year=2025).isoformat(), ts.replace(year=2027).isoformat())),
        _row("cert-indeterminate", "loss", -1.0, 2, [
            _cert_link_sig(inter1_d, inter1_n, chain_id, cert_path[1], cert_path[0]),
            _cert_link_sig(root_d, root_n, chain_id, cert_path[2], cert_path[1]),
        ], _certificate_objects(cert_path, inter1_n, inter1_e, root_n, root_e, ts.replace(year=2025).isoformat(), "")),
    ]
    journal_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        kill_switch_consecutive_losses=3,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": "f" * 64},
        remediation_certificate_link_policy="require_verified_links",
            )

    accepted, rejected = load_validated_realized_rows(str(journal_path), cfg)
    assert len(accepted) == 1
    assert accepted[0].entry.position_id == "cert-valid"
    assert len([r for r in rejected if r.reason == "manual_remediation_certificate_link_internally_invalid"]) == 1
    assert len([r for r in rejected if r.reason == "manual_remediation_certificate_semantically_indeterminate"]) == 1

    monkeypatch.setattr(sys, "argv", [
        "prog",
        "--journal",
        str(journal_path),
        "--state-file",
        str(state_path),
    ])
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_strategy_config", lambda _p=None: cfg)
    runtime_main()

    state = load_runtime_state(str(state_path))
    assert state.kill_switch is not None
    assert state.kill_switch.consecutive_losses == 0

    summary = review_journal(str(journal_path), cfg)
    assert summary["manual_remediated_attested_lineage_rows"] == 1
    assert summary["manual_remediation_revoked_rows"] == 1
    assert summary["manual_remediation_indeterminate_rows"] == 1
    assert summary["rejected_realized_rows"] == 2


def test_runtime_and_review_cohere_for_der_tbs_valid_invalid_indeterminate(tmp_path, monkeypatch) -> None:
    journal_path = tmp_path / "runtime_review_der_tbs.jsonl"
    state_path = tmp_path / "runtime_state_der.json"
    ts = datetime(2026, 10, 1, tzinfo=timezone.utc)
    cert_path, der_chain = _build_der_chain(tmp_path)

    def _row(position_id: str, minute: int, der_chain_hex: list[str]) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "runtime-der"})
        ev_hash = _attested_hash(payload)
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
            "remediation_bundle_id": f"bundle-{position_id}",
            "remediation_evidence_ref": "sha256:" + ev_hash,
            "remediation_evidence_payload": payload,
            "remediation_evidence_hash_sha256": ev_hash,
            "remediation_parent_lineage_tier": "legacy_lineage_rejected",
            "remediation_operator": "auditor",
            "remediation_reason": "manual proof pack",
            "remediation_timestamp_utc": ts.replace(minute=minute).isoformat(),
            "remediation_external_source": "ext://runtime-proof",
            "remediation_signer_id": "ops-ledger",
            "remediation_key_instance_id": "ops-ledger:key-1",
            "remediation_key_epoch": 1,
            "remediation_key_serial": "k1",
            "remediation_issuer_id": "ops-root-ca",
            "remediation_certificate_chain_id": "ops-chain-v2",
            "remediation_certificate_fingerprint_sha256": cert_path[0],
            "remediation_certificate_path_fingerprints": json.dumps(cert_path),
            "remediation_certificate_path_anchor_fingerprint_sha256": cert_path[-1],
            "remediation_certificate_der_hex_chain": json.dumps(der_chain_hex),
            "remediation_certificate_link_signature_scheme": "der_tbs_rsa_sha256_v1",
            "remediation_signature_scheme": "sha256_signer_key_bind_v1",
            "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", ev_hash),
        }

    rows = [
        _row("der-valid", 0, der_chain),
        _row("der-invalid", 1, [der_chain[0][:-2] + "00", der_chain[1], der_chain[2]]),
        _row("der-indeterminate", 2, ["zz", der_chain[1], der_chain[2]]),
    ]
    journal_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        kill_switch_consecutive_losses=3,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": cert_path[-1]},
        remediation_certificate_link_policy="require_verified_links",
        remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1",
        remediation_trusted_root_fingerprints=(cert_path[-1],),
    )

    accepted, rejected = load_validated_realized_rows(str(journal_path), cfg)
    assert len(accepted) == 1
    assert accepted[0].entry.position_id == "der-valid"
    assert len([r for r in rejected if r.reason == "manual_remediation_certificate_der_tbs_invalid"]) == 1
    assert len([r for r in rejected if r.reason == "manual_remediation_certificate_der_tbs_indeterminate"]) == 1

    monkeypatch.setattr(sys, "argv", ["prog", "--journal", str(journal_path), "--state-file", str(state_path)])
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_strategy_config", lambda _p=None: cfg)
    runtime_main()

    summary = review_journal(str(journal_path), cfg)
    assert summary["manual_remediated_attested_lineage_rows"] == 1
    assert summary["manual_remediation_revoked_rows"] == 1
    assert summary["manual_remediation_indeterminate_rows"] == 1


def test_runtime_review_manifest_bundle_valid_invalid_indeterminate_coherence(tmp_path, monkeypatch) -> None:
    journal_path = tmp_path / "runtime_review_manifest.jsonl"
    state_path = tmp_path / "runtime_state_manifest.json"
    ts = datetime(2026, 10, 2, tzinfo=timezone.utc)
    cert_path, der_chain = _build_der_chain(tmp_path)

    def _row(position_id: str, minute: int, manifest_json: str, manifest_hash: str) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "runtime-manifest"})
        ev_hash = _attested_hash(payload)
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
            "remediation_bundle_id": f"bundle-{position_id}",
            "remediation_evidence_ref": "sha256:" + ev_hash,
            "remediation_evidence_manifest_json": manifest_json,
            "remediation_evidence_manifest_hash_sha256": manifest_hash,
            "remediation_evidence_payload": payload,
            "remediation_evidence_hash_sha256": ev_hash,
            "remediation_parent_lineage_tier": "legacy_lineage_rejected",
            "remediation_operator": "auditor",
            "remediation_reason": "manual proof pack",
            "remediation_timestamp_utc": ts.replace(minute=minute).isoformat(),
            "remediation_external_source": "ext://runtime-proof",
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
            "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", ev_hash),
        }

    valid_ev = _attested_hash(json.dumps({"ticket": "valid", "proof": "runtime-manifest"}))
    valid_manifest, valid_hash = _manifest_json_hash(
        "bundle-manifest-valid",
        valid_ev,
        [
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": valid_ev, "required": True},
            {"role": "certificate_chain", "path": "certs/chain.der", "sha256": hashlib.sha256("chain".encode()).hexdigest(), "required": True},
        ],
    )
    invalid_ev = _attested_hash(json.dumps({"ticket": "invalid", "proof": "runtime-manifest"}))
    invalid_manifest, _invalid_hash = _manifest_json_hash(
        "bundle-manifest-invalid",
        invalid_ev,
        [
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": invalid_ev, "required": True},
            {"role": "certificate_chain", "path": "certs/chain.der", "sha256": hashlib.sha256("chain".encode()).hexdigest(), "required": True},
        ],
    )

    rows = [
        _row("manifest-valid", 0, valid_manifest.replace("bundle-manifest-valid", "bundle-manifest-valid"), valid_hash),
        _row("manifest-invalid", 1, invalid_manifest, "0" * 64),
        _row("manifest-indeterminate", 2, "", ""),
    ]
    journal_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        kill_switch_consecutive_losses=3,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": cert_path[-1]},
        remediation_certificate_link_policy="require_verified_links",
        remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1",
        remediation_trusted_root_fingerprints=(cert_path[-1],),
        remediation_evidence_manifest_policy="require_complete_manifest",
    )

    # re-bind valid manifest to row's actual evidence hash
    payload_valid = json.dumps({"ticket": "manifest-valid", "proof": "runtime-manifest"})
    ev_valid = _attested_hash(payload_valid)
    m_valid, h_valid = _manifest_json_hash(
        "bundle-manifest-valid",
        ev_valid,
        [
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_valid, "required": True},
            {"role": "certificate_chain", "path": "certs/chain.der", "sha256": hashlib.sha256("chain".encode()).hexdigest(), "required": True},
        ],
    )
    rows[0]["remediation_evidence_manifest_json"] = m_valid
    rows[0]["remediation_evidence_manifest_hash_sha256"] = h_valid

    payload_invalid = json.dumps({"ticket": "manifest-invalid", "proof": "runtime-manifest"})
    ev_invalid = _attested_hash(payload_invalid)
    m_invalid, _ = _manifest_json_hash(
        "bundle-manifest-invalid",
        ev_invalid,
        [
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_invalid, "required": True},
            {"role": "certificate_chain", "path": "certs/chain.der", "sha256": hashlib.sha256("chain".encode()).hexdigest(), "required": True},
        ],
    )
    rows[1]["remediation_evidence_manifest_json"] = m_invalid
    rows[1]["remediation_evidence_manifest_hash_sha256"] = "0" * 64

    journal_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    accepted, rejected = load_validated_realized_rows(str(journal_path), cfg)
    assert len(accepted) == 1
    assert accepted[0].entry.position_id == "manifest-valid"
    assert len([r for r in rejected if r.reason == "manual_remediation_manifest_invalid"]) == 1
    assert len([r for r in rejected if r.reason == "manual_remediation_manifest_indeterminate"]) == 1

    monkeypatch.setattr(sys, "argv", ["prog", "--journal", str(journal_path), "--state-file", str(state_path)])
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_strategy_config", lambda _p=None: cfg)
    runtime_main()

    summary = review_journal(str(journal_path), cfg)
    assert summary["manual_remediated_attested_lineage_rows"] == 1
    assert summary["manual_remediation_revoked_rows"] == 1
    assert summary["manual_remediation_indeterminate_rows"] == 1


def test_runtime_review_retrieval_proof_valid_invalid_indeterminate_coherence(tmp_path, monkeypatch) -> None:
    journal_path = tmp_path / "runtime_review_retrieval.jsonl"
    state_path = tmp_path / "runtime_state_retrieval.json"
    ts = datetime(2026, 10, 3, tzinfo=timezone.utc)
    cert_path, der_chain = _build_der_chain(tmp_path)

    def _row(position_id: str, minute: int, manifest_json: str, manifest_hash: str, retrieval_json: str, retrieval_hash: str) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "runtime-retrieval"})
        ev_hash = _attested_hash(payload)
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
            "remediation_bundle_id": f"bundle-{position_id}",
            "remediation_evidence_ref": "sha256:" + ev_hash,
            "remediation_evidence_manifest_json": manifest_json,
            "remediation_evidence_manifest_hash_sha256": manifest_hash,
            "remediation_evidence_retrieval_proofs_json": retrieval_json,
            "remediation_evidence_retrieval_proofs_hash_sha256": retrieval_hash,
            "remediation_evidence_payload": payload,
            "remediation_evidence_hash_sha256": ev_hash,
            "remediation_parent_lineage_tier": "legacy_lineage_rejected",
            "remediation_operator": "auditor",
            "remediation_reason": "manual proof pack",
            "remediation_timestamp_utc": ts.replace(minute=minute).isoformat(),
            "remediation_external_source": "ext://runtime-proof",
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
            "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", ev_hash),
        }

    def _manifest_and_retrieval(position_id: str) -> tuple[str, str, str, str]:
        payload = json.dumps({"ticket": position_id, "proof": "runtime-retrieval"})
        ev_hash = _attested_hash(payload)
        bundle_id = f"bundle-{position_id}"
        artifacts = [
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "required": True},
            {"role": "certificate_chain", "path": "certs/chain.der", "sha256": hashlib.sha256("chain".encode()).hexdigest(), "required": True},
        ]
        m_json, m_hash = _manifest_json_hash(bundle_id, ev_hash, artifacts)
        retrievals = [
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "available": True, "content_sha256": ev_hash, "retrieval_receipt_sha256": hashlib.sha256((position_id+"proof").encode()).hexdigest()},
            {"role": "certificate_chain", "path": "certs/chain.der", "sha256": hashlib.sha256("chain".encode()).hexdigest(), "available": True, "content_sha256": hashlib.sha256("chain".encode()).hexdigest(), "retrieval_receipt_sha256": hashlib.sha256((position_id+"chain").encode()).hexdigest()},
        ]
        r_json, r_hash = _retrieval_json_hash(bundle_id, ev_hash, retrievals)
        return m_json, m_hash, r_json, r_hash

    m_v, h_v, r_v, rh_v = _manifest_and_retrieval("retrieval-valid")
    m_i, h_i, r_i, _rh_i = _manifest_and_retrieval("retrieval-invalid")
    m_d, h_d, _r_d, _rh_d = _manifest_and_retrieval("retrieval-indeterminate")

    rows = [
        _row("retrieval-valid", 0, m_v, h_v, r_v, rh_v),
        _row("retrieval-invalid", 1, m_i, h_i, r_i, "0" * 64),
        _row("retrieval-indeterminate", 2, m_d, h_d, "", ""),
    ]
    journal_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        kill_switch_consecutive_losses=3,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": cert_path[-1]},
        remediation_certificate_link_policy="require_verified_links",
        remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1",
        remediation_trusted_root_fingerprints=(cert_path[-1],),
        remediation_evidence_manifest_policy="require_complete_manifest",
        remediation_evidence_retrieval_policy="require_retrieval_proofs",
    )

    accepted, rejected = load_validated_realized_rows(str(journal_path), cfg)
    assert len(accepted) == 1
    assert accepted[0].entry.position_id == "retrieval-valid"
    assert len([r for r in rejected if r.reason == "manual_remediation_retrieval_invalid"]) == 1
    assert len([r for r in rejected if r.reason == "manual_remediation_retrieval_indeterminate"]) == 1

    monkeypatch.setattr(sys, "argv", ["prog", "--journal", str(journal_path), "--state-file", str(state_path)])
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_strategy_config", lambda _p=None: cfg)
    runtime_main()

    summary = review_journal(str(journal_path), cfg)
    assert summary["manual_remediated_attested_lineage_rows"] == 1
    assert summary["manual_remediation_revoked_rows"] == 1
    assert summary["manual_remediation_indeterminate_rows"] == 1


def test_required_artifact_retrieval_state_derivation_is_explicit() -> None:
    from btc_rinse_repeat_v1.journal import _derive_retrieval_bundle_state, _retrieval_artifact_state

    required = ("primary_evidence", "files/proof.json", "a" * 64)
    assert _retrieval_artifact_state(required, []) == "retrieval_invalid_artifact"
    assert _retrieval_artifact_state(required, [{"available": False, "content_sha256": "a" * 64}]) == "retrieval_invalid_artifact"
    assert _retrieval_artifact_state(required, [{"available": True, "content_sha256": "b" * 64}]) == "retrieval_invalid_artifact"
    assert _retrieval_artifact_state(required, [{"available": True, "content_sha256": "a" * 64}]) == "retrieval_valid_artifact"
    assert _retrieval_artifact_state(
        required,
        [
            {"available": True, "content_sha256": "a" * 64, "retrieval_receipt_sha256": "1" * 64},
            {"available": False, "content_sha256": "a" * 64, "retrieval_receipt_sha256": "2" * 64},
        ],
    ) == "retrieval_indeterminate_artifact"

    assert _derive_retrieval_bundle_state(["retrieval_valid_artifact", "retrieval_valid_artifact"]) == "retrieval_valid_bundle"
    assert _derive_retrieval_bundle_state(["retrieval_valid_artifact", "retrieval_invalid_artifact"]) == "retrieval_invalid_bundle"
    assert _derive_retrieval_bundle_state(["retrieval_valid_artifact", "retrieval_indeterminate_artifact"]) == "retrieval_indeterminate_bundle"


def test_manual_remediation_retrieval_fail_closed_for_missing_unavailable_and_content_mismatch(tmp_path) -> None:
    journal_path = tmp_path / "retrieval_fail_closed.jsonl"
    ts = datetime(2026, 10, 4, tzinfo=timezone.utc)
    cert_path, der_chain = _build_der_chain(tmp_path)

    def _row(position_id: str, minute: int, retrievals: list[dict]) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "runtime-retrieval-fail-closed"})
        ev_hash = _attested_hash(payload)
        bundle_id = f"bundle-{position_id}"
        cert_sha = hashlib.sha256("chain".encode()).hexdigest()
        manifest_json, manifest_hash = _manifest_json_hash(
            bundle_id,
            ev_hash,
            [
                {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "required": True},
                {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "required": True},
            ],
        )
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
            "remediation_reason": "manual proof pack",
            "remediation_timestamp_utc": ts.replace(minute=minute).isoformat(),
            "remediation_external_source": "ext://runtime-proof",
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
            "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", ev_hash),
        }

    cert_sha = hashlib.sha256("chain".encode()).hexdigest()
    rows = [
        _row(
            "retrieval-valid",
            0,
            [
                {"role": "primary_evidence", "path": "files/proof.json", "sha256": _attested_hash(json.dumps({"ticket": "retrieval-valid", "proof": "runtime-retrieval-fail-closed"})), "available": True, "content_sha256": _attested_hash(json.dumps({"ticket": "retrieval-valid", "proof": "runtime-retrieval-fail-closed"})), "retrieval_receipt_sha256": hashlib.sha256(b"valid-proof").hexdigest()},
                {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "available": True, "content_sha256": cert_sha, "retrieval_receipt_sha256": hashlib.sha256(b"valid-chain").hexdigest()},
            ],
        ),
        _row(
            "retrieval-missing-required",
            1,
            [
                {"role": "primary_evidence", "path": "files/proof.json", "sha256": _attested_hash(json.dumps({"ticket": "retrieval-missing-required", "proof": "runtime-retrieval-fail-closed"})), "available": True, "content_sha256": _attested_hash(json.dumps({"ticket": "retrieval-missing-required", "proof": "runtime-retrieval-fail-closed"})), "retrieval_receipt_sha256": hashlib.sha256(b"missing-proof").hexdigest()},
            ],
        ),
        _row(
            "retrieval-unavailable",
            2,
            [
                {"role": "primary_evidence", "path": "files/proof.json", "sha256": _attested_hash(json.dumps({"ticket": "retrieval-unavailable", "proof": "runtime-retrieval-fail-closed"})), "available": True, "content_sha256": _attested_hash(json.dumps({"ticket": "retrieval-unavailable", "proof": "runtime-retrieval-fail-closed"})), "retrieval_receipt_sha256": hashlib.sha256(b"unavailable-proof").hexdigest()},
                {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "available": False, "content_sha256": cert_sha, "retrieval_receipt_sha256": hashlib.sha256(b"unavailable-chain").hexdigest()},
            ],
        ),
        _row(
            "retrieval-content-mismatch",
            3,
            [
                {"role": "primary_evidence", "path": "files/proof.json", "sha256": _attested_hash(json.dumps({"ticket": "retrieval-content-mismatch", "proof": "runtime-retrieval-fail-closed"})), "available": True, "content_sha256": _attested_hash(json.dumps({"ticket": "retrieval-content-mismatch", "proof": "runtime-retrieval-fail-closed"})), "retrieval_receipt_sha256": hashlib.sha256(b"mismatch-proof").hexdigest()},
                {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "available": True, "content_sha256": "0" * 64, "retrieval_receipt_sha256": hashlib.sha256(b"mismatch-chain").hexdigest()},
            ],
        ),
        _row(
            "retrieval-conflicting-duplicate",
            4,
            [
                {"role": "primary_evidence", "path": "files/proof.json", "sha256": _attested_hash(json.dumps({"ticket": "retrieval-conflicting-duplicate", "proof": "runtime-retrieval-fail-closed"})), "available": True, "content_sha256": _attested_hash(json.dumps({"ticket": "retrieval-conflicting-duplicate", "proof": "runtime-retrieval-fail-closed"})), "retrieval_receipt_sha256": hashlib.sha256(b"dup-proof").hexdigest()},
                {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "available": True, "content_sha256": cert_sha, "retrieval_receipt_sha256": hashlib.sha256(b"dup-chain-ok").hexdigest()},
                {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "available": False, "content_sha256": cert_sha, "retrieval_receipt_sha256": hashlib.sha256(b"dup-chain-bad").hexdigest()},
            ],
        ),
    ]
    journal_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        kill_switch_consecutive_losses=3,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": cert_path[-1]},
        remediation_certificate_link_policy="require_verified_links",
        remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1",
        remediation_trusted_root_fingerprints=(cert_path[-1],),
        remediation_evidence_manifest_policy="require_complete_manifest",
        remediation_evidence_retrieval_policy="require_retrieval_proofs",
    )

    accepted, rejected = load_validated_realized_rows(str(journal_path), cfg)
    assert [row.entry.position_id for row in accepted] == ["retrieval-valid"]
    assert len([row for row in rejected if row.reason == "manual_remediation_retrieval_invalid"]) == 3
    assert len([row for row in rejected if row.reason == "manual_remediation_retrieval_indeterminate"]) == 1


def test_retrieval_validation_is_deterministic_and_idempotent_across_runtime_review(tmp_path, monkeypatch) -> None:
    journal_path = tmp_path / "runtime_review_retrieval_idempotent.jsonl"
    state_path = tmp_path / "runtime_state_retrieval_idempotent.json"
    ts = datetime(2026, 10, 5, tzinfo=timezone.utc)
    cert_path, der_chain = _build_der_chain(tmp_path)

    def _row(position_id: str, minute: int, retrieval_json: str, retrieval_hash: str) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "runtime-retrieval-idempotent"})
        ev_hash = _attested_hash(payload)
        bundle_id = f"bundle-{position_id}"
        cert_sha = hashlib.sha256("chain".encode()).hexdigest()
        manifest_json, manifest_hash = _manifest_json_hash(
            bundle_id,
            ev_hash,
            [
                {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "required": True},
                {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "required": True},
            ],
        )
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
            "remediation_reason": "manual proof pack",
            "remediation_timestamp_utc": ts.replace(minute=minute).isoformat(),
            "remediation_external_source": "ext://runtime-proof",
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
            "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", ev_hash),
        }

    def _retrieval_payload(position_id: str) -> tuple[str, str]:
        payload = json.dumps({"ticket": position_id, "proof": "runtime-retrieval-idempotent"})
        ev_hash = _attested_hash(payload)
        cert_sha = hashlib.sha256("chain".encode()).hexdigest()
        bundle_id = f"bundle-{position_id}"
        retrievals = [
            {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "available": True, "content_sha256": cert_sha, "retrieval_receipt_sha256": hashlib.sha256((position_id + "-chain").encode()).hexdigest()},
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "available": True, "content_sha256": ev_hash, "retrieval_receipt_sha256": hashlib.sha256((position_id + "-proof").encode()).hexdigest()},
        ]
        return _retrieval_json_hash(bundle_id, ev_hash, retrievals)

    valid_json, valid_hash = _retrieval_payload("retrieval-valid")
    invalid_json, _invalid_hash = _retrieval_payload("retrieval-invalid")
    rows = [
        _row("retrieval-valid", 0, valid_json, valid_hash),
        _row("retrieval-invalid", 1, invalid_json, "0" * 64),
        _row("retrieval-indeterminate", 2, "", ""),
    ]
    journal_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        kill_switch_consecutive_losses=3,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": cert_path[-1]},
        remediation_certificate_link_policy="require_verified_links",
        remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1",
        remediation_trusted_root_fingerprints=(cert_path[-1],),
        remediation_evidence_manifest_policy="require_complete_manifest",
        remediation_evidence_retrieval_policy="require_retrieval_proofs",
    )

    accepted_a, rejected_a = load_validated_realized_rows(str(journal_path), cfg)
    accepted_b, rejected_b = load_validated_realized_rows(str(journal_path), cfg)
    assert [(r.entry.position_id, r.reason) for r in accepted_a] == [(r.entry.position_id, r.reason) for r in accepted_b]
    assert [(r.entry.position_id, r.reason) for r in rejected_a] == [(r.entry.position_id, r.reason) for r in rejected_b]

    monkeypatch.setattr(sys, "argv", ["prog", "--journal", str(journal_path), "--state-file", str(state_path)])
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_strategy_config", lambda _p=None: cfg)
    runtime_main()
    summary = review_journal(str(journal_path), cfg)
    assert summary["manual_remediated_attested_lineage_rows"] == 1
    assert summary["manual_remediation_revoked_rows"] == 1
    assert summary["manual_remediation_indeterminate_rows"] == 1


def test_retrieval_receipt_authenticity_states_and_bundle_derivation() -> None:
    from btc_rinse_repeat_v1.journal import _derive_receipt_bundle_state, _retrieval_receipt_state

    cfg = StrategyConfig(
        remediation_retrieval_receipt_auth_policy="require_authentic_receipts",
        remediation_trusted_retrieval_receipt_attestor_ids=("ops-retrieval-ledger",),
        remediation_trusted_retrieval_receipt_attestor_key_instances={"ops-retrieval-ledger": ("ops-retrieval-ledger:key-1",)},
    )
    bundle_id = "bundle-state"
    evidence_hash = "a" * 64
    base_row = {
        "role": "primary_evidence",
        "path": "files/proof.json",
        "sha256": evidence_hash,
        "content_sha256": evidence_hash,
        "retrieval_receipt_sha256": "b" * 64,
    }
    indeterminate = dict(base_row)
    assert _retrieval_receipt_state(indeterminate, bundle_id, evidence_hash, None, cfg) == "retrieval_receipt_indeterminate"

    invalid = dict(base_row)
    invalid.update(
        {
            "retrieval_receipt_attestor_id": "unknown",
            "retrieval_receipt_attestor_key_instance_id": "unknown:key-1",
            "retrieval_receipt_source": "ext://receipts",
            "retrieval_receipt_signature_scheme": "sha256_receipt_bind_v1",
            "retrieval_receipt_signature": "c" * 64,
        }
    )
    assert _retrieval_receipt_state(invalid, bundle_id, evidence_hash, None, cfg) == "retrieval_receipt_invalid"

    authentic = dict(base_row)
    authentic.update(
        {
            "retrieval_receipt_attestor_id": "ops-retrieval-ledger",
            "retrieval_receipt_attestor_key_instance_id": "ops-retrieval-ledger:key-1",
            "retrieval_receipt_source": "ext://receipts",
            "retrieval_receipt_signature_scheme": "sha256_receipt_bind_v1",
        }
    )
    authentic["retrieval_receipt_signature"] = _retrieval_receipt_sig(
        "ops-retrieval-ledger",
        "ops-retrieval-ledger:key-1",
        "ext://receipts",
        bundle_id,
        evidence_hash,
        authentic["role"],
        authentic["path"],
        authentic["sha256"],
        authentic["content_sha256"],
        authentic["retrieval_receipt_sha256"],
    )
    assert _retrieval_receipt_state(authentic, bundle_id, evidence_hash, None, cfg) == "retrieval_receipt_authentic"

    assert _derive_receipt_bundle_state(["retrieval_receipt_authentic", "retrieval_receipt_authentic"]) == "retrieval_receipt_authentic"
    assert _derive_receipt_bundle_state(["retrieval_receipt_authentic", "retrieval_receipt_invalid"]) == "retrieval_receipt_invalid"
    assert _derive_receipt_bundle_state(["retrieval_receipt_authentic", "retrieval_receipt_indeterminate"]) == "retrieval_receipt_indeterminate"




def test_retrieval_receipt_attestor_lifecycle_states_revoked_and_indeterminate() -> None:
    from btc_rinse_repeat_v1.journal import _retrieval_receipt_attestor_lifecycle_state

    remediation_ts = datetime(2026, 10, 6, tzinfo=timezone.utc)
    cfg_revoked = StrategyConfig(
        remediation_revoked_retrieval_receipt_attestor_ids=("ops-retrieval-ledger",),
        remediation_retrieval_receipt_attestor_lifecycle_policy="reject_all_revoked",
        remediation_retrieval_receipt_attestor_rotation_policy="allow_historical_non_active",
    )
    assert _retrieval_receipt_attestor_lifecycle_state("ops-retrieval-ledger", remediation_ts, cfg_revoked) == "receipt_attestor_revoked"

    cfg_pre_revocation = StrategyConfig(
        remediation_revoked_retrieval_receipt_attestor_ids=("ops-retrieval-ledger",),
        remediation_retrieval_receipt_attestor_revoked_before_utc={"ops-retrieval-ledger": "2026-10-07T00:00:00+00:00"},
        remediation_retrieval_receipt_attestor_lifecycle_policy="allow_pre_revocation_only",
        remediation_retrieval_receipt_attestor_rotation_policy="allow_historical_non_active",
    )
    assert _retrieval_receipt_attestor_lifecycle_state("ops-retrieval-ledger", remediation_ts, cfg_pre_revocation) == "receipt_attestor_valid"

    cfg_indeterminate = StrategyConfig(
        remediation_revoked_retrieval_receipt_attestor_ids=("ops-retrieval-ledger",),
        remediation_retrieval_receipt_attestor_lifecycle_policy="allow_pre_revocation_only",
    )
    assert _retrieval_receipt_attestor_lifecycle_state("ops-retrieval-ledger", remediation_ts, cfg_indeterminate) == "receipt_attestor_indeterminate"


def test_manual_remediation_receipt_auth_fail_closed_invalid_and_indeterminate(tmp_path) -> None:
    journal_path = tmp_path / "retrieval_receipt_auth.jsonl"
    ts = datetime(2026, 10, 6, tzinfo=timezone.utc)
    cert_path, der_chain = _build_der_chain(tmp_path)

    def _row(position_id: str, minute: int, receipt_mode: str) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "runtime-receipt-auth"})
        ev_hash = _attested_hash(payload)
        bundle_id = f"bundle-{position_id}"
        cert_sha = hashlib.sha256("chain".encode()).hexdigest()
        manifest_json, manifest_hash = _manifest_json_hash(
            bundle_id,
            ev_hash,
            [
                {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "required": True},
                {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "required": True},
            ],
        )

        retrievals = []
        for role, path, sha, suffix in [
            ("primary_evidence", "files/proof.json", ev_hash, "proof"),
            ("certificate_chain", "certs/chain.der", cert_sha, "chain"),
        ]:
            receipt_sha = hashlib.sha256((position_id + suffix).encode()).hexdigest()
            row = {
                "role": role,
                "path": path,
                "sha256": sha,
                "available": True,
                "content_sha256": sha,
                "retrieval_receipt_sha256": receipt_sha,
            }
            if receipt_mode != "indeterminate":
                row["retrieval_receipt_attestor_id"] = "ops-retrieval-ledger"
                row["retrieval_receipt_attestor_key_instance_id"] = "ops-retrieval-ledger:key-1"
                row["retrieval_receipt_source"] = "ext://receipts"
                row["retrieval_receipt_signature_scheme"] = "sha256_receipt_bind_v1"
                sig = _retrieval_receipt_sig("ops-retrieval-ledger", "ops-retrieval-ledger:key-1", "ext://receipts", bundle_id, ev_hash, role, path, sha, sha, receipt_sha)
                row["retrieval_receipt_signature"] = sig if receipt_mode == "authentic" else "0" * 64
            retrievals.append(row)

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
            "remediation_reason": "manual proof pack",
            "remediation_timestamp_utc": ts.replace(minute=minute).isoformat(),
            "remediation_external_source": "ext://runtime-proof",
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
            "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", ev_hash),
        }

    rows = [
        _row("receipt-authentic", 0, "authentic"),
        _row("receipt-invalid", 1, "invalid"),
        _row("receipt-indeterminate", 2, "indeterminate"),
    ]
    journal_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        kill_switch_consecutive_losses=3,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": cert_path[-1]},
        remediation_certificate_link_policy="require_verified_links",
        remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1",
        remediation_trusted_root_fingerprints=(cert_path[-1],),
        remediation_evidence_manifest_policy="require_complete_manifest",
        remediation_evidence_retrieval_policy="require_retrieval_proofs",
        remediation_retrieval_receipt_auth_policy="require_authentic_receipts",
        remediation_trusted_retrieval_receipt_attestor_ids=("ops-retrieval-ledger",),
    )

    accepted, rejected = load_validated_realized_rows(str(journal_path), cfg)
    assert [row.entry.position_id for row in accepted] == ["receipt-authentic"]
    assert len([row for row in rejected if row.reason == "manual_remediation_retrieval_receipt_invalid"]) == 1
    assert len([row for row in rejected if row.reason == "manual_remediation_retrieval_receipt_indeterminate"]) == 1


def test_runtime_review_cohere_for_retrieval_receipt_authentic_invalid_indeterminate(tmp_path, monkeypatch) -> None:
    journal_path = tmp_path / "runtime_review_retrieval_receipt_auth.jsonl"
    state_path = tmp_path / "runtime_state_retrieval_receipt_auth.json"
    ts = datetime(2026, 10, 7, tzinfo=timezone.utc)
    cert_path, der_chain = _build_der_chain(tmp_path)

    def _receipt_rows(position_id: str, mode: str) -> tuple[str, str, str, str]:
        payload = json.dumps({"ticket": position_id, "proof": "runtime-receipt-coherence"})
        ev_hash = _attested_hash(payload)
        bundle_id = f"bundle-{position_id}"
        cert_sha = hashlib.sha256("chain".encode()).hexdigest()
        manifest_json, manifest_hash = _manifest_json_hash(
            bundle_id,
            ev_hash,
            [
                {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "required": True},
                {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "required": True},
            ],
        )
        retrievals = []
        for role, path, sha, suffix in [
            ("primary_evidence", "files/proof.json", ev_hash, "proof"),
            ("certificate_chain", "certs/chain.der", cert_sha, "chain"),
        ]:
            receipt_sha = hashlib.sha256((position_id + suffix).encode()).hexdigest()
            row = {
                "role": role,
                "path": path,
                "sha256": sha,
                "available": True,
                "content_sha256": sha,
                "retrieval_receipt_sha256": receipt_sha,
            }
            if mode != "indeterminate":
                row["retrieval_receipt_attestor_id"] = "ops-retrieval-ledger"
                row["retrieval_receipt_attestor_key_instance_id"] = "ops-retrieval-ledger:key-1"
                row["retrieval_receipt_source"] = "ext://receipts"
                row["retrieval_receipt_signature_scheme"] = "sha256_receipt_bind_v1"
                row["retrieval_receipt_signature"] = _retrieval_receipt_sig(
                    "ops-retrieval-ledger",
                    "ops-retrieval-ledger:key-1",
                    "ext://receipts",
                    bundle_id,
                    ev_hash,
                    role,
                    path,
                    sha,
                    sha,
                    receipt_sha,
                )
                if mode == "invalid":
                    row["retrieval_receipt_signature"] = "0" * 64
            retrievals.append(row)
        retrieval_json, retrieval_hash = _retrieval_json_hash(bundle_id, ev_hash, retrievals)
        return manifest_json, manifest_hash, retrieval_json, retrieval_hash

    def _row(position_id: str, minute: int, data: tuple[str, str, str, str]) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "runtime-receipt-coherence"})
        ev_hash = _attested_hash(payload)
        manifest_json, manifest_hash, retrieval_json, retrieval_hash = data
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
            "remediation_bundle_id": f"bundle-{position_id}",
            "remediation_evidence_ref": "sha256:" + ev_hash,
            "remediation_evidence_manifest_json": manifest_json,
            "remediation_evidence_manifest_hash_sha256": manifest_hash,
            "remediation_evidence_retrieval_proofs_json": retrieval_json,
            "remediation_evidence_retrieval_proofs_hash_sha256": retrieval_hash,
            "remediation_evidence_payload": payload,
            "remediation_evidence_hash_sha256": ev_hash,
            "remediation_parent_lineage_tier": "legacy_lineage_rejected",
            "remediation_operator": "auditor",
            "remediation_reason": "manual proof pack",
            "remediation_timestamp_utc": ts.replace(minute=minute).isoformat(),
            "remediation_external_source": "ext://runtime-proof",
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
            "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", ev_hash),
        }

    rows = [
        _row("receipt-authentic", 0, _receipt_rows("receipt-authentic", "authentic")),
        _row("receipt-invalid", 1, _receipt_rows("receipt-invalid", "invalid")),
        _row("receipt-indeterminate", 2, _receipt_rows("receipt-indeterminate", "indeterminate")),
    ]
    journal_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        kill_switch_consecutive_losses=3,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": cert_path[-1]},
        remediation_certificate_link_policy="require_verified_links",
        remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1",
        remediation_trusted_root_fingerprints=(cert_path[-1],),
        remediation_evidence_manifest_policy="require_complete_manifest",
        remediation_evidence_retrieval_policy="require_retrieval_proofs",
        remediation_retrieval_receipt_auth_policy="require_authentic_receipts",
        remediation_trusted_retrieval_receipt_attestor_ids=("ops-retrieval-ledger",),
    )

    accepted_a, rejected_a = load_validated_realized_rows(str(journal_path), cfg)
    accepted_b, rejected_b = load_validated_realized_rows(str(journal_path), cfg)
    assert [(r.entry.position_id, r.reason) for r in accepted_a] == [(r.entry.position_id, r.reason) for r in accepted_b]
    assert [(r.entry.position_id, r.reason) for r in rejected_a] == [(r.entry.position_id, r.reason) for r in rejected_b]

    monkeypatch.setattr(sys, "argv", ["prog", "--journal", str(journal_path), "--state-file", str(state_path)])
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_strategy_config", lambda _p=None: cfg)
    runtime_main()

    summary = review_journal(str(journal_path), cfg)
    assert summary["manual_remediated_attested_lineage_rows"] == 1
    assert summary["manual_remediation_revoked_rows"] == 1
    assert summary["manual_remediation_indeterminate_rows"] == 1


def test_manual_remediation_receipt_attestor_lifecycle_fail_closed(tmp_path) -> None:
    journal_path = tmp_path / "retrieval_receipt_attestor_lifecycle.jsonl"
    ts = datetime(2026, 10, 9, tzinfo=timezone.utc)
    cert_path, der_chain = _build_der_chain(tmp_path)

    def _row(position_id: str, minute: int, attestor_id: str) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "runtime-receipt-attestor-lifecycle"})
        ev_hash = _attested_hash(payload)
        bundle_id = f"bundle-{position_id}"
        cert_sha = hashlib.sha256("chain".encode()).hexdigest()
        manifest_json, manifest_hash = _manifest_json_hash(
            bundle_id,
            ev_hash,
            [
                {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "required": True},
                {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "required": True},
            ],
        )
        retrievals = []
        for role, path, sha, suffix in [
            ("primary_evidence", "files/proof.json", ev_hash, "proof"),
            ("certificate_chain", "certs/chain.der", cert_sha, "chain"),
        ]:
            receipt_sha = hashlib.sha256((position_id + suffix).encode()).hexdigest()
            retrievals.append(
                {
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
                    "retrieval_receipt_signature": _retrieval_receipt_sig(attestor_id, f"{attestor_id}:key-1", "ext://receipts", bundle_id, ev_hash, role, path, sha, sha, receipt_sha),
                }
            )
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
            "remediation_reason": "manual proof pack",
            "remediation_timestamp_utc": ts.replace(minute=minute).isoformat(),
            "remediation_external_source": "ext://runtime-proof",
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
            "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", ev_hash),
        }

    rows = [
        _row("attestor-valid", 0, "ops-retrieval-ledger"),
        _row("attestor-revoked", 1, "ops-retrieval-ledger"),
        _row("attestor-indeterminate", 2, "ops-retrieval-legacy"),
    ]
    journal_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        kill_switch_consecutive_losses=3,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": cert_path[-1]},
        remediation_certificate_link_policy="require_verified_links",
        remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1",
        remediation_trusted_root_fingerprints=(cert_path[-1],),
        remediation_evidence_manifest_policy="require_complete_manifest",
        remediation_evidence_retrieval_policy="require_retrieval_proofs",
        remediation_retrieval_receipt_auth_policy="require_authentic_receipts",
        remediation_trusted_retrieval_receipt_attestor_ids=("ops-retrieval-ledger", "ops-retrieval-legacy"),
        remediation_revoked_retrieval_receipt_attestor_ids=("ops-retrieval-ledger", "ops-retrieval-legacy"),
        remediation_retrieval_receipt_attestor_lifecycle_policy="allow_pre_revocation_only",
        remediation_retrieval_receipt_attestor_revoked_before_utc={"ops-retrieval-ledger": "2026-10-09T00:01:00+00:00"},
        remediation_retrieval_receipt_attestor_rotation_policy="allow_historical_non_active",
        remediation_retrieval_receipt_active_attestor_ids=("ops-retrieval-ledger",),
    )

    accepted, rejected = load_validated_realized_rows(str(journal_path), cfg)
    assert [r.entry.position_id for r in accepted] == ["attestor-valid"]
    assert len([r for r in rejected if r.reason == "manual_remediation_retrieval_receipt_attestor_revoked"]) == 1
    assert len([r for r in rejected if r.reason == "manual_remediation_retrieval_receipt_attestor_indeterminate"]) == 1
    assert len([r for r in rejected if r.reason == "manual_remediation_retrieval_receipt_invalid"]) == 0




def test_runtime_review_cohere_for_receipt_attestor_lifecycle_states(tmp_path, monkeypatch) -> None:
    journal_path = tmp_path / "runtime_review_receipt_attestor_lifecycle.jsonl"
    state_path = tmp_path / "runtime_state_receipt_attestor_lifecycle.json"
    ts = datetime(2026, 10, 10, tzinfo=timezone.utc)
    cert_path, der_chain = _build_der_chain(tmp_path)

    def _row(position_id: str, minute: int, attestor_id: str) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "runtime-receipt-attestor-lifecycle-coherence"})
        ev_hash = _attested_hash(payload)
        bundle_id = f"bundle-{position_id}"
        cert_sha = hashlib.sha256("chain".encode()).hexdigest()
        manifest_json, manifest_hash = _manifest_json_hash(bundle_id, ev_hash, [
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "required": True},
            {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "required": True},
        ])
        retrievals = []
        for role, path, sha, suffix in [
            ("primary_evidence", "files/proof.json", ev_hash, "proof"),
            ("certificate_chain", "certs/chain.der", cert_sha, "chain"),
        ]:
            receipt_sha = hashlib.sha256((position_id + suffix).encode()).hexdigest()
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
                "retrieval_receipt_signature": _retrieval_receipt_sig(attestor_id, f"{attestor_id}:key-1", "ext://receipts", bundle_id, ev_hash, role, path, sha, sha, receipt_sha),
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
            "remediation_reason": "manual proof pack",
            "remediation_timestamp_utc": ts.replace(minute=minute).isoformat(),
            "remediation_external_source": "ext://runtime-proof",
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
            "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", ev_hash),
        }

    rows = [
        _row("attestor-valid", 0, "ops-retrieval-ledger"),
        _row("attestor-revoked", 1, "ops-retrieval-ledger"),
        _row("attestor-indeterminate", 2, "ops-retrieval-legacy"),
    ]
    journal_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    cfg = StrategyConfig(
        taker_fee_rate=0.0,
        slippage_rate=0.0,
        kill_switch_consecutive_losses=3,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": cert_path[-1]},
        remediation_certificate_link_policy="require_verified_links",
        remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1",
        remediation_trusted_root_fingerprints=(cert_path[-1],),
        remediation_evidence_manifest_policy="require_complete_manifest",
        remediation_evidence_retrieval_policy="require_retrieval_proofs",
        remediation_retrieval_receipt_auth_policy="require_authentic_receipts",
        remediation_trusted_retrieval_receipt_attestor_ids=("ops-retrieval-ledger", "ops-retrieval-legacy"),
        remediation_revoked_retrieval_receipt_attestor_ids=("ops-retrieval-ledger", "ops-retrieval-legacy"),
        remediation_retrieval_receipt_attestor_lifecycle_policy="allow_pre_revocation_only",
        remediation_retrieval_receipt_attestor_revoked_before_utc={"ops-retrieval-ledger": "2026-10-10T00:01:00+00:00"},
        remediation_retrieval_receipt_attestor_key_rotation_policy="allow_historical_non_active",
        remediation_trusted_retrieval_receipt_attestor_key_instances={"ops-retrieval-ledger": ("ops-retrieval-ledger:key-1",), "ops-retrieval-legacy": ("ops-retrieval-legacy:key-1",)},
    )

    accepted, rejected = load_validated_realized_rows(str(journal_path), cfg)
    assert [r.entry.position_id for r in accepted] == ["attestor-valid"]
    assert len([r for r in rejected if r.reason == "manual_remediation_retrieval_receipt_attestor_revoked"]) == 1
    assert len([r for r in rejected if r.reason == "manual_remediation_retrieval_receipt_attestor_indeterminate"]) == 1

    monkeypatch.setattr(sys, "argv", ["prog", "--journal", str(journal_path), "--state-file", str(state_path)])
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_strategy_config", lambda _p=None: cfg)
    runtime_main()
    summary = review_journal(str(journal_path), cfg)
    assert summary["manual_remediated_attested_lineage_rows"] == 1
    assert summary["manual_remediation_revoked_rows"] == 1
    assert summary["manual_remediation_indeterminate_rows"] == 1


def test_retrieval_receipt_attestor_key_instance_states_explicit() -> None:
    from btc_rinse_repeat_v1.journal import _retrieval_receipt_attestor_key_instance_state

    ts = datetime(2026, 10, 12, tzinfo=timezone.utc)
    cfg_valid = StrategyConfig(
        remediation_retrieval_receipt_attestor_key_rotation_policy="require_active_key_lineage",
        remediation_trusted_retrieval_receipt_attestor_key_instances={"ops-retrieval-ledger": ("ops-retrieval-ledger:key-1",)},
    )
    assert _retrieval_receipt_attestor_key_instance_state("ops-retrieval-ledger", "ops-retrieval-ledger:key-1", ts, cfg_valid, "lifecycle_evidence_indeterminate", None) == "receipt_attestor_key_instance_valid"

    cfg_revoked = StrategyConfig(
        remediation_revoked_retrieval_receipt_attestor_key_instances=("ops-retrieval-ledger:key-1",),
        remediation_retrieval_receipt_attestor_lifecycle_policy="reject_all_revoked",
    )
    assert _retrieval_receipt_attestor_key_instance_state("ops-retrieval-ledger", "ops-retrieval-ledger:key-1", ts, cfg_revoked, "lifecycle_evidence_indeterminate", None) == "receipt_attestor_key_instance_revoked"

    cfg_ind = StrategyConfig(
        remediation_revoked_retrieval_receipt_attestor_key_instances=("ops-retrieval-ledger:key-1",),
        remediation_retrieval_receipt_attestor_lifecycle_policy="allow_pre_revocation_only",
    )
    assert _retrieval_receipt_attestor_key_instance_state("ops-retrieval-ledger", "ops-retrieval-ledger:key-1", ts, cfg_ind, "lifecycle_evidence_indeterminate", None) == "receipt_attestor_key_instance_indeterminate"


def test_retrieval_receipt_key_instance_lifecycle_evidence_states() -> None:
    from btc_rinse_repeat_v1.journal import _parse_retrieval_receipt_attestor_key_lifecycle_evidence

    bundle_id = "bundle-lifecycle"
    evidence_hash = "a" * 64
    records = [{"attestor_id": "ops-retrieval-ledger", "key_instance_id": "ops-retrieval-ledger:key-1", "lifecycle_state": "active"}]
    evidence_json, evidence_sha = _key_lifecycle_evidence(bundle_id, evidence_hash, records)
    governance_json, governance_sha = _anchor_governance_dataset(
        bundle_id,
        evidence_hash,
        [{"anchor_id": "ops-retrieval-key-ledger", "anchor_key_instance_id": "ops-retrieval-key-ledger:key-1", "lifecycle_state": "active"}],
    )

    remediation_ts = datetime(2026, 10, 1, tzinfo=timezone.utc)
    cfg = StrategyConfig(
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_ids=("ops-retrieval-key-ledger",),
        remediation_retrieval_receipt_attestor_key_lifecycle_anchor_key_governance_policy="require_governed_anchor_keys",
        remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_policy="require_anchored_dataset",
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_anchor_ids=("ops-retrieval-anchor-governance-ledger",),
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances={"ops-retrieval-key-ledger": ("ops-retrieval-key-ledger:key-1",)},
    )
    entry = JournalEntry(
        timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc), market_regime="Trend Up", decision="Paper Long", entry="(220,220)", stop="215", target="225",
        projected_rr=2.0, confidence=0.8, reasoning="x", what_would_have_made_this_stand_aside="x",
        remediation_retrieval_receipt_attestor_key_lifecycle_evidence_json=evidence_json,
        remediation_retrieval_receipt_attestor_key_lifecycle_evidence_hash_sha256=evidence_sha,
        remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_json=governance_json,
        remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_hash_sha256=governance_sha,
    )
    state, parsed = _parse_retrieval_receipt_attestor_key_lifecycle_evidence(entry, bundle_id, evidence_hash, remediation_ts, "governance_dataset_anchored", {("ops-retrieval-key-ledger", "ops-retrieval-key-ledger:key-1"): {"anchor_id": "ops-retrieval-key-ledger", "anchor_key_instance_id": "ops-retrieval-key-ledger:key-1", "lifecycle_state": "active", "revoked_before_utc": ""}}, cfg)
    assert state == "lifecycle_evidence_anchored"
    assert parsed[("ops-retrieval-ledger", "ops-retrieval-ledger:key-1")]["lifecycle_state"] == "active"

    tampered = JournalEntry(**{**entry.__dict__, "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_hash_sha256": "0" * 64})
    bad_state, _ = _parse_retrieval_receipt_attestor_key_lifecycle_evidence(tampered, bundle_id, evidence_hash, remediation_ts, "governance_dataset_anchored", {("ops-retrieval-key-ledger", "ops-retrieval-key-ledger:key-1"): {"anchor_id": "ops-retrieval-key-ledger", "anchor_key_instance_id": "ops-retrieval-key-ledger:key-1", "lifecycle_state": "active", "revoked_before_utc": ""}}, cfg)
    assert bad_state == "lifecycle_evidence_invalid"

    missing = JournalEntry(**{**entry.__dict__, "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_json": None, "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_hash_sha256": None})
    missing_state, _ = _parse_retrieval_receipt_attestor_key_lifecycle_evidence(missing, bundle_id, evidence_hash, remediation_ts, "governance_dataset_anchored", {("ops-retrieval-key-ledger", "ops-retrieval-key-ledger:key-1"): {"anchor_id": "ops-retrieval-key-ledger", "anchor_key_instance_id": "ops-retrieval-key-ledger:key-1", "lifecycle_state": "active", "revoked_before_utc": ""}}, cfg)
    assert missing_state == "lifecycle_evidence_indeterminate"

    cfg_revoked = StrategyConfig(
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_ids=("ops-retrieval-key-ledger",),
        remediation_retrieval_receipt_attestor_key_lifecycle_anchor_key_governance_policy="require_governed_anchor_keys",
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances={"ops-retrieval-key-ledger": ("ops-retrieval-key-ledger:key-1",)},
        remediation_revoked_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances=("ops-retrieval-key-ledger:key-1",),
    )
    revoked_state, _ = _parse_retrieval_receipt_attestor_key_lifecycle_evidence(entry, bundle_id, evidence_hash, remediation_ts, "governance_dataset_anchored", {("ops-retrieval-key-ledger", "ops-retrieval-key-ledger:key-1"): {"anchor_id": "ops-retrieval-key-ledger", "anchor_key_instance_id": "ops-retrieval-key-ledger:key-1", "lifecycle_state": "active", "revoked_before_utc": ""}}, cfg_revoked)
    assert revoked_state == "lifecycle_evidence_anchor_key_revoked"


def test_governance_dataset_anchor_identity_states() -> None:
    from btc_rinse_repeat_v1.journal import _parse_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset

    bundle_id = "bundle-governance-anchor"
    evidence_hash = "b" * 64
    remediation_ts = datetime(2026, 10, 3, tzinfo=timezone.utc)
    gov_json, gov_sha = _anchor_governance_dataset(bundle_id, evidence_hash, [{"anchor_id": "ops-retrieval-key-ledger", "anchor_key_instance_id": "ops-retrieval-key-ledger:key-1", "lifecycle_state": "active"}])
    entry = JournalEntry(
        timestamp=remediation_ts, market_regime="Trend Up", decision="Paper Long", entry="(220,220)", stop="215", target="225",
        projected_rr=2.0, confidence=0.8, reasoning="x", what_would_have_made_this_stand_aside="x",
        remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_json=gov_json,
        remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_hash_sha256=gov_sha,
    )
    cfg = StrategyConfig(
        remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_policy="require_anchored_dataset",
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_anchor_ids=("ops-retrieval-anchor-governance-ledger",),
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_ids=("ops-retrieval-anchor-governance-ledger",),
    )
    anchored_state, _ = _parse_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset(
        entry,
        bundle_id,
        evidence_hash,
        remediation_ts,
        "governance_anchor_valid",
        {"ops-retrieval-anchor-governance-ledger": {"governance_anchor_id": "ops-retrieval-anchor-governance-ledger", "lifecycle_state": "active", "revoked_before_utc": ""}},
        cfg,
    )
    assert anchored_state == "governance_dataset_anchored"

    revoked_state, _ = _parse_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset(
        entry,
        bundle_id,
        evidence_hash,
        remediation_ts,
        "governance_anchor_revoked",
        {"ops-retrieval-anchor-governance-ledger": {"governance_anchor_id": "ops-retrieval-anchor-governance-ledger", "lifecycle_state": "revoked", "revoked_before_utc": ""}},
        cfg,
    )
    assert revoked_state == "governance_dataset_anchor_revoked"

    ind_state, _ = _parse_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset(
        entry,
        bundle_id,
        evidence_hash,
        remediation_ts,
        "governance_anchor_indeterminate",
        {},
        cfg,
    )
    assert ind_state == "governance_dataset_anchor_indeterminate"


def test_identity_provenance_anchor_states_for_governance_anchor_provenance() -> None:
    from btc_rinse_repeat_v1.journal import _parse_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance

    bundle_id = "bundle-identity-anchor"
    evidence_hash = "c" * 64
    remediation_ts = datetime(2026, 10, 4, tzinfo=timezone.utc)
    prov_json, prov_sha = _governance_anchor_identity_provenance(
        bundle_id,
        evidence_hash,
        [{"governance_anchor_id": "ops-retrieval-anchor-governance-ledger", "lifecycle_state": "active"}],
    )
    entry = JournalEntry(
        timestamp=remediation_ts, market_regime="Trend Up", decision="Paper Long", entry="(220,220)", stop="215", target="225",
        projected_rr=2.0, confidence=0.8, reasoning="x", what_would_have_made_this_stand_aside="x",
        remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_json=prov_json,
        remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_hash_sha256=prov_sha,
    )
    cfg = StrategyConfig(
        remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_policy="require_anchored_identity_provenance",
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_anchor_ids=("ops-retrieval-anchor-identity-ledger",),
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_governance_anchor_ids=("ops-retrieval-anchor-governance-ledger",),
    )
    state_valid, _ = _parse_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance(
        entry,
        bundle_id,
        evidence_hash,
        remediation_ts,
        "identity_provenance_anchor_valid",
        {"ops-retrieval-anchor-identity-ledger": {"provenance_anchor_id": "ops-retrieval-anchor-identity-ledger", "lifecycle_state": "active", "revoked_before_utc": ""}},
        cfg,
    )
    assert state_valid == "governance_anchor_valid"

    state_revoked, _ = _parse_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance(
        entry,
        bundle_id,
        evidence_hash,
        remediation_ts,
        "identity_provenance_anchor_revoked",
        {"ops-retrieval-anchor-identity-ledger": {"provenance_anchor_id": "ops-retrieval-anchor-identity-ledger", "lifecycle_state": "revoked", "revoked_before_utc": ""}},
        cfg,
    )
    assert state_revoked == "governance_anchor_source_revoked"

    state_ind, _ = _parse_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance(
        entry,
        bundle_id,
        evidence_hash,
        remediation_ts,
        "identity_provenance_anchor_indeterminate",
        {},
        cfg,
    )
    assert state_ind == "governance_anchor_source_indeterminate"


def test_manual_remediation_receipt_attestor_key_instance_fail_closed(tmp_path) -> None:
    journal_path = tmp_path / "retrieval_receipt_attestor_key_instance.jsonl"
    ts = datetime(2026, 10, 13, tzinfo=timezone.utc)
    cert_path, der_chain = _build_der_chain(tmp_path)

    def _row(position_id: str, minute: int, key_instance_id: str) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "runtime-receipt-key-instance"})
        ev_hash = _attested_hash(payload)
        bundle_id = f"bundle-{position_id}"
        cert_sha = hashlib.sha256("chain".encode()).hexdigest()
        manifest_json, manifest_hash = _manifest_json_hash(bundle_id, ev_hash, [
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "required": True},
            {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "required": True},
        ])
        retrievals = []
        for role, path, sha, suffix in [("primary_evidence", "files/proof.json", ev_hash, "proof"), ("certificate_chain", "certs/chain.der", cert_sha, "chain")]:
            receipt_sha = hashlib.sha256((position_id + suffix).encode()).hexdigest()
            retrievals.append({
                "role": role, "path": path, "sha256": sha, "available": True, "content_sha256": sha,
                "retrieval_receipt_sha256": receipt_sha,
                "retrieval_receipt_attestor_id": "ops-retrieval-ledger",
                "retrieval_receipt_attestor_key_instance_id": key_instance_id,
                "retrieval_receipt_source": "ext://receipts",
                "retrieval_receipt_signature_scheme": "sha256_receipt_bind_v1",
                "retrieval_receipt_signature": _retrieval_receipt_sig("ops-retrieval-ledger", key_instance_id, "ext://receipts", bundle_id, ev_hash, role, path, sha, sha, receipt_sha),
            })
        retrieval_json, retrieval_hash = _retrieval_json_hash(bundle_id, ev_hash, retrievals)
        return {
            "timestamp": ts.replace(minute=minute).isoformat(),
            "market_regime": "Trend Up", "decision": "Paper Long", "entry": "(220,220)", "stop": "215", "target": "225",
            "projected_rr": 2.0, "confidence": 0.8, "reasoning": "manual-remediation", "what_would_have_made_this_stand_aside": "n/a",
            "position_size_btc": 1.0, "position_id": position_id, "position_status": "closed",
            "executed_entry_price": 220.0, "executed_notional_usd": 220.0,
            "actual_outcome": "loss", "actual_exit_price": 219.0, "actual_pnl_usd": -1.0, "actual_pnl_basis": "net",
            "remediation_status": "manual_recovered", "remediation_bundle_id": bundle_id, "remediation_evidence_ref": "sha256:" + ev_hash,
            "remediation_evidence_manifest_json": manifest_json, "remediation_evidence_manifest_hash_sha256": manifest_hash,
            "remediation_evidence_retrieval_proofs_json": retrieval_json, "remediation_evidence_retrieval_proofs_hash_sha256": retrieval_hash,
            "remediation_evidence_payload": payload, "remediation_evidence_hash_sha256": ev_hash,
            "remediation_parent_lineage_tier": "legacy_lineage_rejected", "remediation_operator": "auditor", "remediation_reason": "manual proof pack",
            "remediation_timestamp_utc": ts.replace(minute=minute).isoformat(), "remediation_external_source": "ext://runtime-proof",
            "remediation_signer_id": "ops-ledger", "remediation_key_instance_id": "ops-ledger:key-1", "remediation_key_epoch": 1, "remediation_key_serial": "k1",
            "remediation_issuer_id": "ops-root-ca", "remediation_certificate_chain_id": "ops-chain-v2", "remediation_certificate_fingerprint_sha256": cert_path[0],
            "remediation_certificate_path_fingerprints": json.dumps(cert_path), "remediation_certificate_path_anchor_fingerprint_sha256": cert_path[-1],
            "remediation_certificate_der_hex_chain": json.dumps(der_chain), "remediation_certificate_link_signature_scheme": "der_tbs_rsa_sha256_v1",
            "remediation_signature_scheme": "sha256_signer_key_bind_v1", "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", ev_hash),
        }

    rows = [
        _row("key-valid", 0, "ops-retrieval-ledger:key-1"),
        _row("key-revoked", 1, "ops-retrieval-ledger:key-2"),
        _row("key-indeterminate", 2, "ops-retrieval-ledger:key-3"),
    ]
    journal_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    cfg = StrategyConfig(
        taker_fee_rate=0.0, slippage_rate=0.0, kill_switch_consecutive_losses=3,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": cert_path[-1]},
        remediation_certificate_link_policy="require_verified_links",
        remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1",
        remediation_trusted_root_fingerprints=(cert_path[-1],),
        remediation_evidence_manifest_policy="require_complete_manifest",
        remediation_evidence_retrieval_policy="require_retrieval_proofs",
        remediation_retrieval_receipt_auth_policy="require_authentic_receipts",
        remediation_trusted_retrieval_receipt_attestor_ids=("ops-retrieval-ledger",),
        remediation_retrieval_receipt_attestor_key_rotation_policy="require_active_key_lineage",
        remediation_trusted_retrieval_receipt_attestor_key_instances={"ops-retrieval-ledger": ("ops-retrieval-ledger:key-1",)},
        remediation_revoked_retrieval_receipt_attestor_key_instances=("ops-retrieval-ledger:key-2",),
        remediation_retrieval_receipt_attestor_lifecycle_policy="allow_pre_revocation_only",
    )

    accepted, rejected = load_validated_realized_rows(str(journal_path), cfg)
    assert [r.entry.position_id for r in accepted] == ["key-valid"]
    assert len([r for r in rejected if r.reason == "manual_remediation_retrieval_receipt_attestor_key_instance_revoked"]) == 1
    assert len([r for r in rejected if r.reason == "manual_remediation_retrieval_receipt_attestor_key_instance_indeterminate"]) == 1


def test_runtime_review_cohere_for_receipt_attestor_key_instance_states(tmp_path, monkeypatch) -> None:
    journal_path = tmp_path / "runtime_review_receipt_key_instance.jsonl"
    state_path = tmp_path / "runtime_state_receipt_key_instance.json"
    ts = datetime(2026, 10, 14, tzinfo=timezone.utc)
    cert_path, der_chain = _build_der_chain(tmp_path)

    def _build_row(position_id: str, minute: int, key_instance_id: str) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "runtime-review-receipt-key-instance"})
        ev_hash = _attested_hash(payload)
        bundle_id = f"bundle-{position_id}"
        cert_sha = hashlib.sha256("chain".encode()).hexdigest()
        manifest_json, manifest_hash = _manifest_json_hash(bundle_id, ev_hash, [
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "required": True},
            {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "required": True},
        ])
        retrievals = []
        for role, path, sha, suffix in [("primary_evidence", "files/proof.json", ev_hash, "proof"), ("certificate_chain", "certs/chain.der", cert_sha, "chain")]:
            receipt_sha = hashlib.sha256((position_id + suffix).encode()).hexdigest()
            retrievals.append({
                "role": role, "path": path, "sha256": sha, "available": True, "content_sha256": sha,
                "retrieval_receipt_sha256": receipt_sha,
                "retrieval_receipt_attestor_id": "ops-retrieval-ledger",
                "retrieval_receipt_attestor_key_instance_id": key_instance_id,
                "retrieval_receipt_source": "ext://receipts",
                "retrieval_receipt_signature_scheme": "sha256_receipt_bind_v1",
                "retrieval_receipt_signature": _retrieval_receipt_sig("ops-retrieval-ledger", key_instance_id, "ext://receipts", bundle_id, ev_hash, role, path, sha, sha, receipt_sha),
            })
        retrieval_json, retrieval_hash = _retrieval_json_hash(bundle_id, ev_hash, retrievals)
        return {
            "timestamp": ts.replace(minute=minute).isoformat(), "market_regime": "Trend Up", "decision": "Paper Long", "entry": "(220,220)", "stop": "215", "target": "225",
            "projected_rr": 2.0, "confidence": 0.8, "reasoning": "manual-remediation", "what_would_have_made_this_stand_aside": "n/a",
            "position_size_btc": 1.0, "position_id": position_id, "position_status": "closed",
            "executed_entry_price": 220.0, "executed_notional_usd": 220.0, "actual_outcome": "loss", "actual_exit_price": 219.0, "actual_pnl_usd": -1.0, "actual_pnl_basis": "net",
            "remediation_status": "manual_recovered", "remediation_bundle_id": bundle_id, "remediation_evidence_ref": "sha256:" + ev_hash,
            "remediation_evidence_manifest_json": manifest_json, "remediation_evidence_manifest_hash_sha256": manifest_hash,
            "remediation_evidence_retrieval_proofs_json": retrieval_json, "remediation_evidence_retrieval_proofs_hash_sha256": retrieval_hash,
            "remediation_evidence_payload": payload, "remediation_evidence_hash_sha256": ev_hash,
            "remediation_parent_lineage_tier": "legacy_lineage_rejected", "remediation_operator": "auditor", "remediation_reason": "manual proof pack",
            "remediation_timestamp_utc": ts.replace(minute=minute).isoformat(), "remediation_external_source": "ext://runtime-proof",
            "remediation_signer_id": "ops-ledger", "remediation_key_instance_id": "ops-ledger:key-1", "remediation_key_epoch": 1, "remediation_key_serial": "k1",
            "remediation_issuer_id": "ops-root-ca", "remediation_certificate_chain_id": "ops-chain-v2", "remediation_certificate_fingerprint_sha256": cert_path[0],
            "remediation_certificate_path_fingerprints": json.dumps(cert_path), "remediation_certificate_path_anchor_fingerprint_sha256": cert_path[-1],
            "remediation_certificate_der_hex_chain": json.dumps(der_chain), "remediation_certificate_link_signature_scheme": "der_tbs_rsa_sha256_v1",
            "remediation_signature_scheme": "sha256_signer_key_bind_v1", "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", ev_hash),
        }

    rows = [
        _build_row("key-valid", 0, "ops-retrieval-ledger:key-1"),
        _build_row("key-revoked", 1, "ops-retrieval-ledger:key-2"),
        _build_row("key-indeterminate", 2, "ops-retrieval-ledger:key-3"),
    ]
    journal_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    cfg = StrategyConfig(
        taker_fee_rate=0.0, slippage_rate=0.0, kill_switch_consecutive_losses=3,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": cert_path[-1]},
        remediation_certificate_link_policy="require_verified_links",
        remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1",
        remediation_trusted_root_fingerprints=(cert_path[-1],),
        remediation_evidence_manifest_policy="require_complete_manifest",
        remediation_evidence_retrieval_policy="require_retrieval_proofs",
        remediation_retrieval_receipt_auth_policy="require_authentic_receipts",
        remediation_trusted_retrieval_receipt_attestor_ids=("ops-retrieval-ledger",),
        remediation_retrieval_receipt_attestor_key_rotation_policy="require_active_key_lineage",
        remediation_trusted_retrieval_receipt_attestor_key_instances={"ops-retrieval-ledger": ("ops-retrieval-ledger:key-1",)},
        remediation_revoked_retrieval_receipt_attestor_key_instances=("ops-retrieval-ledger:key-2",),
        remediation_retrieval_receipt_attestor_lifecycle_policy="allow_pre_revocation_only",
    )

    accepted_a, rejected_a = load_validated_realized_rows(str(journal_path), cfg)
    accepted_b, rejected_b = load_validated_realized_rows(str(journal_path), cfg)
    assert [(r.entry.position_id, r.reason) for r in accepted_a] == [(r.entry.position_id, r.reason) for r in accepted_b]
    assert [(r.entry.position_id, r.reason) for r in rejected_a] == [(r.entry.position_id, r.reason) for r in rejected_b]

    monkeypatch.setattr(sys, "argv", ["prog", "--journal", str(journal_path), "--state-file", str(state_path)])
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_strategy_config", lambda _p=None: cfg)
    runtime_main()

    summary = review_journal(str(journal_path), cfg)
    assert summary["manual_remediated_attested_lineage_rows"] == 1
    assert summary["manual_remediation_revoked_rows"] == 1
    assert summary["manual_remediation_indeterminate_rows"] == 1


def test_runtime_review_cohere_for_attestor_key_lifecycle_evidence_states(tmp_path, monkeypatch) -> None:
    journal_path = tmp_path / "runtime_review_key_lifecycle_evidence.jsonl"
    state_path = tmp_path / "runtime_state_key_lifecycle_evidence.json"
    ts = datetime(2026, 10, 20, tzinfo=timezone.utc)
    cert_path, der_chain = _build_der_chain(tmp_path)

    def _build_row(position_id: str, minute: int, lifecycle_mode: str) -> dict:
        payload = json.dumps({"ticket": position_id, "proof": "runtime-review-key-lifecycle-evidence"})
        ev_hash = _attested_hash(payload)
        bundle_id = f"bundle-{position_id}"
        cert_sha = hashlib.sha256("chain".encode()).hexdigest()
        manifest_json, manifest_hash = _manifest_json_hash(bundle_id, ev_hash, [
            {"role": "primary_evidence", "path": "files/proof.json", "sha256": ev_hash, "required": True},
            {"role": "certificate_chain", "path": "certs/chain.der", "sha256": cert_sha, "required": True},
        ])
        retrievals = []
        for role, path, sha, suffix in [("primary_evidence", "files/proof.json", ev_hash, "proof"), ("certificate_chain", "certs/chain.der", cert_sha, "chain")]:
            receipt_sha = hashlib.sha256((position_id + suffix).encode()).hexdigest()
            retrievals.append({
                "role": role, "path": path, "sha256": sha, "available": True, "content_sha256": sha,
                "retrieval_receipt_sha256": receipt_sha,
                "retrieval_receipt_attestor_id": "ops-retrieval-ledger",
                "retrieval_receipt_attestor_key_instance_id": "ops-retrieval-ledger:key-1",
                "retrieval_receipt_source": "ext://receipts",
                "retrieval_receipt_signature_scheme": "sha256_receipt_bind_v1",
                "retrieval_receipt_signature": _retrieval_receipt_sig("ops-retrieval-ledger", "ops-retrieval-ledger:key-1", "ext://receipts", bundle_id, ev_hash, role, path, sha, sha, receipt_sha),
            })
        retrieval_json, retrieval_hash = _retrieval_json_hash(bundle_id, ev_hash, retrievals)

        lifecycle_json = None
        lifecycle_hash = None
        governance_json = None
        governance_hash = None
        governance_anchor_identity_json = None
        governance_anchor_identity_hash = None
        identity_provenance_anchor_json = None
        identity_provenance_anchor_hash = None
        anchor_key_instance_id = "ops-retrieval-key-ledger:key-1"
        if lifecycle_mode == "anchor-key-revoked":
            anchor_key_instance_id = "ops-retrieval-key-ledger:key-2"
        if lifecycle_mode == "anchor-key-indeterminate":
            anchor_key_instance_id = ""
        if lifecycle_mode in {"anchored", "invalid", "anchor-key-revoked", "anchor-key-indeterminate"}:
            lifecycle_json, lifecycle_hash = _key_lifecycle_evidence(
                bundle_id,
                ev_hash,
                [{"attestor_id": "ops-retrieval-ledger", "key_instance_id": "ops-retrieval-ledger:key-1", "lifecycle_state": "active"}],
                anchor_key_instance_id=anchor_key_instance_id or "ops-retrieval-key-ledger:key-1",
            )
            if lifecycle_mode == "anchor-key-indeterminate":
                evidence_obj = json.loads(lifecycle_json)
                evidence_obj["anchor_key_instance_id"] = ""
                signed_payload = {k: v for k, v in evidence_obj.items() if k not in {"signature", "signature_scheme"}}
                payload_hash = hashlib.sha256(json.dumps(signed_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                evidence_obj["signature"] = _key_lifecycle_sig("ops-retrieval-key-ledger", "", "ext://key-lifecycle", payload_hash)
                lifecycle_json = json.dumps(evidence_obj, sort_keys=True, separators=(",", ":"))
                lifecycle_hash = hashlib.sha256(lifecycle_json.encode("utf-8")).hexdigest()
            if lifecycle_mode == "invalid":
                lifecycle_hash = "0" * 64

            governance_json, governance_hash = _anchor_governance_dataset(
                bundle_id,
                ev_hash,
                [{"anchor_id": "ops-retrieval-key-ledger", "anchor_key_instance_id": "ops-retrieval-key-ledger:key-1", "lifecycle_state": "active"}],
            )
            governance_anchor_identity_json, governance_anchor_identity_hash = _governance_anchor_identity_provenance(
                bundle_id,
                ev_hash,
                [{"governance_anchor_id": "ops-retrieval-anchor-governance-ledger", "lifecycle_state": "active"}],
            )
            identity_provenance_anchor_json, identity_provenance_anchor_hash = _identity_provenance_anchor_provenance(
                bundle_id,
                ev_hash,
                [{"provenance_anchor_id": "ops-retrieval-anchor-identity-ledger", "lifecycle_state": "active"}],
            )
            if lifecycle_mode == "anchor-key-revoked":
                governance_json, governance_hash = _anchor_governance_dataset(
                    bundle_id,
                    ev_hash,
                    [{"anchor_id": "ops-retrieval-key-ledger", "anchor_key_instance_id": "ops-retrieval-key-ledger:key-2", "lifecycle_state": "revoked"}],
                )
            if lifecycle_mode == "anchor-key-indeterminate":
                governance_json = None
                governance_hash = None
                governance_anchor_identity_json = None
                governance_anchor_identity_hash = None
                identity_provenance_anchor_json = None
                identity_provenance_anchor_hash = None

        return {
            "timestamp": ts.replace(minute=minute).isoformat(), "market_regime": "Trend Up", "decision": "Paper Long", "entry": "(220,220)", "stop": "215", "target": "225",
            "projected_rr": 2.0, "confidence": 0.8, "reasoning": "manual-remediation", "what_would_have_made_this_stand_aside": "n/a",
            "position_size_btc": 1.0, "position_id": position_id, "position_status": "closed",
            "executed_entry_price": 220.0, "executed_notional_usd": 220.0, "actual_outcome": "loss", "actual_exit_price": 219.0, "actual_pnl_usd": -1.0, "actual_pnl_basis": "net",
            "remediation_status": "manual_recovered", "remediation_bundle_id": bundle_id, "remediation_evidence_ref": "sha256:" + ev_hash,
            "remediation_evidence_manifest_json": manifest_json, "remediation_evidence_manifest_hash_sha256": manifest_hash,
            "remediation_evidence_retrieval_proofs_json": retrieval_json, "remediation_evidence_retrieval_proofs_hash_sha256": retrieval_hash,
            "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_json": lifecycle_json,
            "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_hash_sha256": lifecycle_hash,
            "remediation_retrieval_receipt_attestor_key_lifecycle_evidence_anchor_key_instance_id": anchor_key_instance_id,
            "remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_json": governance_json,
            "remediation_retrieval_receipt_attestor_key_lifecycle_anchor_governance_dataset_hash_sha256": governance_hash,
            "remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_json": governance_anchor_identity_json,
            "remediation_retrieval_receipt_attestor_key_lifecycle_governance_anchor_identity_provenance_hash_sha256": governance_anchor_identity_hash,
            "remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_json": identity_provenance_anchor_json,
            "remediation_retrieval_receipt_attestor_key_lifecycle_identity_provenance_anchor_provenance_hash_sha256": identity_provenance_anchor_hash,
            "remediation_evidence_payload": payload, "remediation_evidence_hash_sha256": ev_hash,
            "remediation_parent_lineage_tier": "legacy_lineage_rejected", "remediation_operator": "auditor", "remediation_reason": "manual proof pack",
            "remediation_timestamp_utc": ts.replace(minute=minute).isoformat(), "remediation_external_source": "ext://runtime-proof",
            "remediation_signer_id": "ops-ledger", "remediation_key_instance_id": "ops-ledger:key-1", "remediation_key_epoch": 1, "remediation_key_serial": "k1",
            "remediation_issuer_id": "ops-root-ca", "remediation_certificate_chain_id": "ops-chain-v2", "remediation_certificate_fingerprint_sha256": cert_path[0],
            "remediation_certificate_path_fingerprints": json.dumps(cert_path), "remediation_certificate_path_anchor_fingerprint_sha256": cert_path[-1],
            "remediation_certificate_der_hex_chain": json.dumps(der_chain), "remediation_certificate_link_signature_scheme": "der_tbs_rsa_sha256_v1",
            "remediation_signature_scheme": "sha256_signer_key_bind_v1", "remediation_signature": _external_sig("ops-ledger", "ops-ledger:key-1", "ext://runtime-proof", ev_hash),
        }

    rows = [
        _build_row("key-lifecycle-anchored", 0, "anchored"),
        _build_row("key-lifecycle-anchor-key-revoked", 1, "anchor-key-revoked"),
        _build_row("key-lifecycle-anchor-key-indeterminate", 2, "anchor-key-indeterminate"),
    ]
    journal_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    cfg = StrategyConfig(
        taker_fee_rate=0.0, slippage_rate=0.0, kill_switch_consecutive_losses=3,
        remediation_issuer_chain_policy="require_key_instance_binding",
        remediation_key_instance_issuer_bindings={"ops-ledger:key-1": "ops-root-ca"},
        remediation_issuer_root_fingerprints={"ops-root-ca": cert_path[-1]},
        remediation_certificate_link_policy="require_verified_links",
        remediation_certificate_link_signature_scheme="der_tbs_rsa_sha256_v1",
        remediation_trusted_root_fingerprints=(cert_path[-1],),
        remediation_evidence_manifest_policy="require_complete_manifest",
        remediation_evidence_retrieval_policy="require_retrieval_proofs",
        remediation_retrieval_receipt_auth_policy="require_authentic_receipts",
        remediation_trusted_retrieval_receipt_attestor_ids=("ops-retrieval-ledger",),
        remediation_retrieval_receipt_attestor_key_rotation_policy="require_active_key_lineage",
        remediation_trusted_retrieval_receipt_attestor_key_instances={"ops-retrieval-ledger": ("ops-retrieval-ledger:key-1",)},
        remediation_retrieval_receipt_attestor_key_lifecycle_evidence_policy="require_anchored_evidence",
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_ids=("ops-retrieval-key-ledger",),
        remediation_retrieval_receipt_attestor_key_lifecycle_anchor_key_governance_policy="require_governed_anchor_keys",
        remediation_trusted_retrieval_receipt_attestor_key_lifecycle_anchor_key_instances={"ops-retrieval-key-ledger": ("ops-retrieval-key-ledger:key-1",)},
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

    accepted_a, rejected_a = load_validated_realized_rows(str(journal_path), cfg)
    accepted_b, rejected_b = load_validated_realized_rows(str(journal_path), cfg)
    assert [(r.entry.position_id, r.reason) for r in accepted_a] == [(r.entry.position_id, r.reason) for r in accepted_b]
    assert [(r.entry.position_id, r.reason) for r in rejected_a] == [(r.entry.position_id, r.reason) for r in rejected_b]
    assert [r.entry.position_id for r in accepted_a] == ["key-lifecycle-anchored"]

    monkeypatch.setattr(sys, "argv", ["prog", "--journal", str(journal_path), "--state-file", str(state_path)])
    monkeypatch.setattr("btc_rinse_repeat_v1.main.load_strategy_config", lambda _p=None: cfg)
    runtime_main()

    summary = review_journal(str(journal_path), cfg)
    assert summary["manual_remediated_attested_lineage_rows"] == 1
    assert summary["manual_remediation_revoked_rows"] == 1
    assert summary["manual_remediation_indeterminate_rows"] == 1
