import json
from datetime import datetime, timezone

from btc_rinse_repeat_v1.config import StrategyConfig
from btc_rinse_repeat_v1.journal import (
    EXECUTION_NOTIONAL_COHERENCE_TOLERANCE_USD,
    REALIZED_PNL_RECONCILIATION_TOLERANCE_USD,
    is_realized_row,
    load_validated_realized_rows,
    validate_realized_row,
)
from btc_rinse_repeat_v1.main import _apply_realized_outcomes_to_kill_switch
from btc_rinse_repeat_v1.models import JournalEntry, KillSwitchState, SimulatedEquityState
from btc_rinse_repeat_v1.equity import update_equity_from_entry


def _entry(**overrides):
    base = dict(
        timestamp=datetime.now(timezone.utc),
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
        actual_outcome="win",
        actual_exit_price=105.0,
        actual_pnl_usd=4.0,
        actual_pnl_basis="net",
    )
    base.update(overrides)
    return JournalEntry(**base)


def test_explicit_net_realized_row_accepted() -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    r = validate_realized_row(_entry(actual_pnl_basis="net", actual_pnl_usd=5.0), cfg)
    assert r.status == "accepted_net"


def test_partial_fill_realized_row_uses_executed_size_for_validation() -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    entry = _entry(
        position_size_btc=2.0,
        executed_size_btc=1.0,
        unfilled_size_btc=1.0,
        entry_fill_fraction=0.5,
        executed_notional_usd=100.0,
        actual_pnl_basis="net",
        actual_exit_price=105.0,
        actual_pnl_usd=5.0,
    )
    r = validate_realized_row(entry, cfg)
    assert r.status == "accepted_net"


def test_partial_fill_rejects_when_executed_size_exceeds_intended() -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    bad = _entry(
        position_size_btc=1.0,
        executed_size_btc=1.5,
        executed_notional_usd=150.0,
        actual_pnl_basis="net",
        actual_exit_price=105.0,
        actual_pnl_usd=7.5,
    )
    r = validate_realized_row(bad, cfg)
    assert r.status == "rejected_requires_manual_review"


def test_explicit_gross_realized_row_normalized_once() -> None:
    cfg = StrategyConfig(taker_fee_rate=0.001, slippage_rate=0.001)
    r = validate_realized_row(_entry(actual_pnl_basis="gross", actual_pnl_usd=5.0), cfg)
    assert r.status == "accepted_gross_normalized"
    assert round(r.normalized_net_pnl_usd, 2) == 4.6


def test_ambiguous_legacy_realized_row_rejected_excluded() -> None:
    cfg = StrategyConfig()
    r = validate_realized_row(_entry(actual_pnl_basis=""), cfg)
    assert r.status == "rejected_requires_manual_review"




def test_outcome_label_must_match_reconciled_pnl_sign() -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    # Reconciled pnl is +5, so labeling as loss must fail closed
    r = validate_realized_row(_entry(actual_pnl_basis="net", actual_pnl_usd=5.0, actual_outcome="loss"), cfg)
    assert r.status == "rejected_requires_manual_review"

def test_structurally_coherent_but_wrong_pnl_rejected() -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    r = validate_realized_row(_entry(actual_pnl_basis="net", actual_pnl_usd=4.0), cfg)
    assert r.status == "rejected_requires_manual_review"




def test_reconciliation_uses_explicit_named_tolerance() -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    # expected net is exactly 5.0 in this fixture
    within = 5.0 + (REALIZED_PNL_RECONCILIATION_TOLERANCE_USD * 0.5)
    outside = 5.0 + (REALIZED_PNL_RECONCILIATION_TOLERANCE_USD * 2.0)

    accepted = validate_realized_row(_entry(actual_pnl_basis="net", actual_pnl_usd=within), cfg)
    rejected = validate_realized_row(_entry(actual_pnl_basis="net", actual_pnl_usd=outside), cfg)

    assert accepted.status == "accepted_net"
    assert rejected.status == "rejected_requires_manual_review"

def test_rejected_realized_row_cannot_affect_equity(tmp_path) -> None:
    cfg = StrategyConfig(starting_equity_usd=1000)
    path = tmp_path / "j.jsonl"
    rows = [
        _entry(actual_pnl_basis="", actual_pnl_usd=100.0),
    ]
    with path.open("w", encoding="utf-8") as f:
        for e in rows:
            f.write(json.dumps(e.__dict__, default=str) + "\n")

    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert len(accepted) == 0
    assert len(rejected) == 1

    state = SimulatedEquityState(1000, 1000, 1000)
    for row in accepted:
        e = row.entry
        e.actual_pnl_usd = row.normalized_net_pnl_usd
        e.actual_pnl_basis = "net"
        update_equity_from_entry(state, e, cfg)
    assert state.current_equity_usd == 1000


def test_rejected_realized_row_cannot_affect_review_metrics(tmp_path) -> None:
    cfg = StrategyConfig()
    path = tmp_path / "j.jsonl"
    rows = [
        _entry(actual_pnl_basis="", actual_pnl_usd=100.0),
    ]
    with path.open("w", encoding="utf-8") as f:
        for e in rows:
            f.write(json.dumps(e.__dict__, default=str) + "\n")
    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert len(accepted) == 0
    assert len(rejected) == 1


def test_rejected_realized_row_cannot_affect_kill_switch_state(tmp_path) -> None:
    cfg = StrategyConfig(kill_switch_consecutive_losses=1, taker_fee_rate=0.0, slippage_rate=0.0)
    path = tmp_path / "j.jsonl"
    good = _entry(actual_pnl_basis="net", actual_pnl_usd=-5.0, actual_outcome="loss", actual_exit_price=95.0)
    bad = _entry(actual_pnl_basis="", actual_pnl_usd=-5.0, actual_outcome="loss", actual_exit_price=95.0)
    with path.open("w", encoding="utf-8") as f:
        for e in [bad, good]:
            f.write(json.dumps(e.__dict__, default=str) + "\n")

    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert len(rejected) == 1

    ks = KillSwitchState()
    ks, _ = _apply_realized_outcomes_to_kill_switch(ks, None, accepted, cfg)
    assert ks.consecutive_losses == 1


def test_is_realized_row_shared_predicate() -> None:
    assert is_realized_row(_entry()) is True
    assert is_realized_row(_entry(actual_outcome="open")) is False


def test_accepted_net_vs_gross_normalized_conflict_fails_closed(tmp_path) -> None:
    cfg = StrategyConfig(starting_equity_usd=1000.0, taker_fee_rate=0.001, slippage_rate=0.001)

    base = dict(
        timestamp=datetime.now(timezone.utc),
        market_regime="Trend Up",
        decision="Paper Long",
        entry="(100,100)",
        stop="95",
        target="105",
        projected_rr=2.0,
        confidence=0.8,
        reasoning="x",
        what_would_have_made_this_stand_aside="x",
        position_size_btc=2.0,
        executed_entry_price=100.0,
        executed_notional_usd=200.0,
        actual_outcome="win",
        actual_exit_price=105.0,
    )

    net_row = JournalEntry(**{**base, "actual_pnl_basis": "net", "actual_pnl_usd": 9.2})
    gross_row = JournalEntry(**{**base, "actual_pnl_basis": "gross", "actual_pnl_usd": 10.0})

    journal_path = tmp_path / "invariant.jsonl"
    with journal_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(net_row.__dict__, default=str) + "\n")
        f.write(json.dumps(gross_row.__dict__, default=str) + "\n")

    accepted, rejected = load_validated_realized_rows(str(journal_path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "ambiguous_duplicate_realized_event"]) == 2


def _accepted_signature(rows):
    return [
        (str(r.entry.timestamp), r.entry.decision, r.entry.actual_outcome, r.normalized_net_pnl_usd)
        for r in rows
    ]


def test_identical_timestamp_rows_have_deterministic_order_across_input_permutations(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)

    a = _entry(
        timestamp=ts,
        decision="Paper Long",
        actual_outcome="loss",
        actual_exit_price=95.0,
        actual_pnl_basis="net",
        actual_pnl_usd=-5.0,
    )
    b = _entry(
        timestamp=ts,
        decision="Paper Short",
        actual_outcome="win",
        actual_exit_price=95.0,
        actual_pnl_basis="net",
        actual_pnl_usd=5.0,
    )

    p1 = tmp_path / "o1.jsonl"
    p2 = tmp_path / "o2.jsonl"
    with p1.open("w", encoding="utf-8") as f:
        for e in [a, b]:
            f.write(json.dumps(e.__dict__, default=str) + "\n")
    with p2.open("w", encoding="utf-8") as f:
        for e in [b, a]:
            f.write(json.dumps(e.__dict__, default=str) + "\n")

    acc1, rej1 = load_validated_realized_rows(str(p1), cfg)
    acc2, rej2 = load_validated_realized_rows(str(p2), cfg)
    assert len(rej1) == 0 and len(rej2) == 0
    assert _accepted_signature(acc1) == _accepted_signature(acc2)


def test_identical_timestamp_rows_replay_state_is_repeatable_and_input_order_independent(tmp_path) -> None:
    cfg = StrategyConfig(kill_switch_consecutive_losses=1, taker_fee_rate=0.0, slippage_rate=0.0)
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)

    loss_row = _entry(
        timestamp=ts,
        decision="Paper Long",
        actual_outcome="loss",
        actual_exit_price=95.0,
        actual_pnl_basis="net",
        actual_pnl_usd=-5.0,
    )
    win_row = _entry(
        timestamp=ts,
        decision="Paper Short",
        actual_outcome="win",
        actual_exit_price=95.0,
        actual_pnl_basis="net",
        actual_pnl_usd=5.0,
    )

    pa = tmp_path / "ka.jsonl"
    pb = tmp_path / "kb.jsonl"
    with pa.open("w", encoding="utf-8") as f:
        for e in [loss_row, win_row]:
            f.write(json.dumps(e.__dict__, default=str) + "\n")
    with pb.open("w", encoding="utf-8") as f:
        for e in [win_row, loss_row]:
            f.write(json.dumps(e.__dict__, default=str) + "\n")

    acc_a_1, _ = load_validated_realized_rows(str(pa), cfg)
    acc_a_2, _ = load_validated_realized_rows(str(pa), cfg)
    acc_b, _ = load_validated_realized_rows(str(pb), cfg)

    # same order on repeated runs and independent of input order
    assert _accepted_signature(acc_a_1) == _accepted_signature(acc_a_2) == _accepted_signature(acc_b)

    ks1 = KillSwitchState()
    ks1, _ = _apply_realized_outcomes_to_kill_switch(ks1, None, acc_a_1, cfg)

    ks2 = KillSwitchState()
    ks2, _ = _apply_realized_outcomes_to_kill_switch(ks2, None, acc_b, cfg)

    assert ks1.consecutive_losses == ks2.consecutive_losses
    assert ks1.review_required == ks2.review_required
    assert (ks1.stand_aside_until is None) == (ks2.stand_aside_until is None)


def test_duplicate_accepted_realized_rows_are_deduplicated_and_rejected_counted(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = _entry(
        timestamp=ts,
        actual_pnl_basis="net",
        actual_pnl_usd=5.0,
        actual_outcome="win",
    )

    path = tmp_path / "dup.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(row.__dict__, default=str) + "\n")
        f.write(json.dumps(row.__dict__, default=str) + "\n")

    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert len(accepted) == 1
    assert any(r.reason == "duplicate_realized_event" for r in rejected)


def test_ordering_normalizes_equal_instants_with_different_timestamp_offsets(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    # same instant in different offsets
    row_a = _entry(
        timestamp="2026-01-01T00:00:00+00:00",
        decision="Paper Long",
        actual_outcome="loss",
        actual_exit_price=95.0,
        actual_pnl_basis="net",
        actual_pnl_usd=-5.0,
    )
    row_b = _entry(
        timestamp="2025-12-31T19:00:00-05:00",
        decision="Paper Short",
        actual_outcome="win",
        actual_exit_price=95.0,
        actual_pnl_basis="net",
        actual_pnl_usd=5.0,
    )

    p1 = tmp_path / "tz1.jsonl"
    p2 = tmp_path / "tz2.jsonl"
    with p1.open("w", encoding="utf-8") as f:
        for e in [row_a, row_b]:
            f.write(json.dumps(e.__dict__, default=str) + "\n")
    with p2.open("w", encoding="utf-8") as f:
        for e in [row_b, row_a]:
            f.write(json.dumps(e.__dict__, default=str) + "\n")

    a1, _ = load_validated_realized_rows(str(p1), cfg)
    a2, _ = load_validated_realized_rows(str(p2), cfg)
    assert _accepted_signature(a1) == _accepted_signature(a2)


def test_non_identical_duplicate_candidates_fail_closed_as_ambiguous(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.001, slippage_rate=0.001)
    ts = datetime(2026, 1, 2, tzinfo=timezone.utc)

    base = dict(
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
        position_size_btc=2.0,
        executed_entry_price=100.0,
        executed_notional_usd=200.0,
        actual_outcome="win",
        actual_exit_price=105.0,
    )

    net_row = JournalEntry(**{**base, "actual_pnl_basis": "net", "actual_pnl_usd": 9.2})
    gross_row = JournalEntry(**{**base, "actual_pnl_basis": "gross", "actual_pnl_usd": 10.0})

    path = tmp_path / "dup_basis.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(gross_row.__dict__, default=str) + "\n")
        f.write(json.dumps(net_row.__dict__, default=str) + "\n")

    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "ambiguous_duplicate_realized_event"]) == 2


def test_fully_identical_duplicates_do_not_double_count_replay_state(tmp_path) -> None:
    cfg = StrategyConfig(starting_equity_usd=1000.0, taker_fee_rate=0.0, slippage_rate=0.0)
    ts = datetime(2026, 1, 3, tzinfo=timezone.utc)
    row = _entry(
        timestamp=ts,
        decision="Paper Long",
        actual_outcome="win",
        actual_exit_price=105.0,
        actual_pnl_basis="net",
        actual_pnl_usd=5.0,
    )

    path = tmp_path / "dup_replay.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(row.__dict__, default=str) + "\n")
        f.write(json.dumps(row.__dict__, default=str) + "\n")

    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert len(accepted) == 1
    assert len([r for r in rejected if r.reason == "duplicate_realized_event"]) == 1

    state = SimulatedEquityState(1000.0, 1000.0, 1000.0)
    for r in accepted:
        e = r.entry
        e.actual_pnl_usd = r.normalized_net_pnl_usd
        e.actual_pnl_basis = "net"
        update_equity_from_entry(state, e, cfg)
    assert state.current_equity_usd == 1005.0


def test_duplicate_policy_is_deterministic_and_order_independent(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    ts = datetime(2026, 1, 4, tzinfo=timezone.utc)

    identical = _entry(timestamp=ts, actual_pnl_basis="net", actual_pnl_usd=5.0, actual_outcome="win")
    ambiguous_a = _entry(
        timestamp=ts,
        decision="Paper Short",
        actual_outcome="loss",
        actual_exit_price=105.0,
        actual_pnl_basis="net",
        actual_pnl_usd=-5.0,
        reasoning="a",
    )
    ambiguous_b = _entry(
        timestamp=ts,
        decision="Paper Short",
        actual_outcome="loss",
        actual_exit_price=105.0,
        actual_pnl_basis="net",
        actual_pnl_usd=-5.0,
        reasoning="b",
    )

    p1 = tmp_path / "dup_mix_1.jsonl"
    p2 = tmp_path / "dup_mix_2.jsonl"

    with p1.open("w", encoding="utf-8") as f:
        for e in [identical, identical, ambiguous_a, ambiguous_b]:
            f.write(json.dumps(e.__dict__, default=str) + "\n")
    with p2.open("w", encoding="utf-8") as f:
        for e in [ambiguous_b, identical, ambiguous_a, identical]:
            f.write(json.dumps(e.__dict__, default=str) + "\n")

    a1_acc_1, a1_rej_1 = load_validated_realized_rows(str(p1), cfg)
    a1_acc_2, a1_rej_2 = load_validated_realized_rows(str(p1), cfg)
    a2_acc, a2_rej = load_validated_realized_rows(str(p2), cfg)

    assert _accepted_signature(a1_acc_1) == _accepted_signature(a1_acc_2) == _accepted_signature(a2_acc)

    def reason_counts(rows):
        counts = {}
        for r in rows:
            counts[r.reason] = counts.get(r.reason, 0) + 1
        return counts

    assert reason_counts(a1_rej_1) == reason_counts(a1_rej_2) == reason_counts(a2_rej)
    assert reason_counts(a1_rej_1).get("duplicate_realized_event") == 1
    assert reason_counts(a1_rej_1).get("ambiguous_duplicate_realized_event") == 2


def test_identity_sufficient_distinct_events_are_retained(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    ts = datetime(2026, 1, 5, tzinfo=timezone.utc)

    a = _entry(
        timestamp=ts,
        decision="Paper Long",
        actual_outcome="win",
        actual_exit_price=105.0,
        actual_pnl_basis="net",
        actual_pnl_usd=5.0,
    )
    b = _entry(
        timestamp=ts,
        decision="Paper Long",
        actual_outcome="win",
        actual_exit_price=106.0,
        actual_pnl_basis="net",
        actual_pnl_usd=6.0,
    )

    path = tmp_path / "identity_distinct.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(a.__dict__, default=str) + "\n")
        f.write(json.dumps(b.__dict__, default=str) + "\n")

    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert len(accepted) == 2
    assert rejected == []


def test_identity_insufficient_case_fails_closed_and_excluded_from_downstream(tmp_path) -> None:
    cfg = StrategyConfig(kill_switch_consecutive_losses=1, taker_fee_rate=0.0, slippage_rate=0.0)
    ts = datetime(2026, 1, 6, tzinfo=timezone.utc)

    a = _entry(
        timestamp=ts,
        decision="Paper Long",
        actual_outcome="win",
        actual_exit_price=105.0,
        actual_pnl_basis="net",
        actual_pnl_usd=5.0,
        stop="95",
        target="105",
    )
    b = _entry(
        timestamp=ts,
        decision="Paper Long",
        actual_outcome="win",
        actual_exit_price=105.0,
        actual_pnl_basis="net",
        actual_pnl_usd=5.0,
        stop="94.5",
        target="106",
    )

    path = tmp_path / "identity_insufficient.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(a.__dict__, default=str) + "\n")
        f.write(json.dumps(b.__dict__, default=str) + "\n")

    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "insufficient_identity_fields"]) == 2

    ks = KillSwitchState()
    ks, _ = _apply_realized_outcomes_to_kill_switch(ks, None, accepted, cfg)
    assert ks.consecutive_losses == 0


def test_identity_sufficiency_decision_is_deterministic_across_runs(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    ts = datetime(2026, 1, 7, tzinfo=timezone.utc)

    ambiguous_a = _entry(
        timestamp=ts,
        decision="Paper Short",
        actual_outcome="loss",
        actual_exit_price=105.0,
        actual_pnl_basis="net",
        actual_pnl_usd=-5.0,
        stop="106",
        target="94",
    )
    ambiguous_b = _entry(
        timestamp=ts,
        decision="Paper Short",
        actual_outcome="loss",
        actual_exit_price=105.0,
        actual_pnl_basis="net",
        actual_pnl_usd=-5.0,
        stop="107",
        target="93",
    )

    p1 = tmp_path / "identity_det_1.jsonl"
    p2 = tmp_path / "identity_det_2.jsonl"
    with p1.open("w", encoding="utf-8") as f:
        for e in [ambiguous_a, ambiguous_b]:
            f.write(json.dumps(e.__dict__, default=str) + "\n")
    with p2.open("w", encoding="utf-8") as f:
        for e in [ambiguous_b, ambiguous_a]:
            f.write(json.dumps(e.__dict__, default=str) + "\n")

    a1_acc_1, a1_rej_1 = load_validated_realized_rows(str(p1), cfg)
    a1_acc_2, a1_rej_2 = load_validated_realized_rows(str(p1), cfg)
    a2_acc, a2_rej = load_validated_realized_rows(str(p2), cfg)

    assert _accepted_signature(a1_acc_1) == _accepted_signature(a1_acc_2) == _accepted_signature(a2_acc)

    def reason_counts(rows):
        counts = {}
        for r in rows:
            counts[r.reason] = counts.get(r.reason, 0) + 1
        return counts

    assert reason_counts(a1_rej_1) == reason_counts(a1_rej_2) == reason_counts(a2_rej)
    assert reason_counts(a1_rej_1).get("insufficient_identity_fields") == 2


def test_reconciliation_tolerance_boundary_exact_below_above() -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    expected = 5.0
    eps = REALIZED_PNL_RECONCILIATION_TOLERANCE_USD

    at = validate_realized_row(_entry(actual_pnl_basis="net", actual_pnl_usd=expected + eps), cfg)
    below = validate_realized_row(_entry(actual_pnl_basis="net", actual_pnl_usd=expected + (eps * 0.999)), cfg)
    above = validate_realized_row(_entry(actual_pnl_basis="net", actual_pnl_usd=expected + (eps * 1.001)), cfg)

    assert at.status == "accepted_net"
    assert below.status == "accepted_net"
    assert above.status == "rejected_requires_manual_review"


def test_execution_notional_coherence_tolerance_boundary_exact_below_above() -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    expected_notional = 100.0
    eps = EXECUTION_NOTIONAL_COHERENCE_TOLERANCE_USD

    at = validate_realized_row(
        _entry(executed_notional_usd=expected_notional + eps, actual_pnl_basis="net", actual_pnl_usd=5.0),
        cfg,
    )
    below = validate_realized_row(
        _entry(executed_notional_usd=expected_notional + (eps * 0.999), actual_pnl_basis="net", actual_pnl_usd=5.0),
        cfg,
    )
    above = validate_realized_row(
        _entry(executed_notional_usd=expected_notional + (eps * 1.001), actual_pnl_basis="net", actual_pnl_usd=5.0),
        cfg,
    )

    assert at.status == "accepted_net"
    assert below.status == "accepted_net"
    assert above.reason == "execution_fields_incoherent"


def test_tolerance_boundaries_are_deterministic_across_runs() -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    eps = REALIZED_PNL_RECONCILIATION_TOLERANCE_USD

    statuses = []
    for _ in range(3):
        r_at = validate_realized_row(_entry(actual_pnl_basis="net", actual_pnl_usd=5.0 + eps), cfg)
        r_above = validate_realized_row(_entry(actual_pnl_basis="net", actual_pnl_usd=5.0 + (eps * 1.001)), cfg)
        statuses.append((r_at.status, r_above.status, r_above.reason))

    assert statuses[0] == statuses[1] == statuses[2]


def test_duplicate_and_identity_paths_are_exact_match_not_tolerance_based(tmp_path) -> None:
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    ts = datetime(2026, 1, 8, tzinfo=timezone.utc)
    eps = REALIZED_PNL_RECONCILIATION_TOLERANCE_USD

    a = _entry(
        timestamp=ts,
        actual_pnl_basis="net",
        actual_pnl_usd=5.0,
        actual_outcome="win",
        stop="95",
        target="105",
    )
    # differs on stop/target so duplicate collapse does not trigger;
    # pnl difference is within reconciliation tolerance and should not alter identity exact-match semantics.
    b = _entry(
        timestamp=ts,
        actual_pnl_basis="net",
        actual_pnl_usd=5.0 + (eps * 0.5),
        actual_outcome="win",
        stop="94.5",
        target="106",
    )

    path = tmp_path / "tol_exact_match.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(a.__dict__, default=str) + "\n")
        f.write(json.dumps(b.__dict__, default=str) + "\n")

    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert accepted == []
    assert len([r for r in rejected if r.reason == "insufficient_identity_fields"]) == 2


def test_tolerance_edge_acceptance_is_consumed_identically_e2e(monkeypatch, tmp_path) -> None:
    from btc_rinse_repeat_v1 import review as review_module
    from btc_rinse_repeat_v1.review import review_journal

    cfg = StrategyConfig(starting_equity_usd=1000.0, taker_fee_rate=0.0, slippage_rate=0.0)
    eps = REALIZED_PNL_RECONCILIATION_TOLERANCE_USD
    ts = datetime(2026, 1, 9, tzinfo=timezone.utc)

    cases = [
        ("at", eps, True),
        ("below", eps * 0.999, True),
        ("above", eps * 1.001, False),
    ]

    original_loader = review_module.load_validated_realized_rows

    for label, delta, should_accept in cases:
        row = _entry(
            timestamp=ts,
            decision="Paper Long",
            actual_outcome="loss",
            actual_exit_price=95.0,
            actual_pnl_basis="net",
            actual_pnl_usd=-5.0 - delta,
        )
        path = tmp_path / f"edge_{label}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(row.__dict__, default=str) + "\n")

        accepted, rejected = load_validated_realized_rows(str(path), cfg)
        accepted_sig = _accepted_signature(accepted)

        captured = {}

        def wrapped_loader(path_arg, cfg_arg):
            acc, rej = original_loader(path_arg, cfg_arg)
            captured["accepted_sig"] = _accepted_signature(acc)
            captured["accepted_len"] = len(acc)
            captured["rejected_len"] = len(rej)
            return acc, rej

        monkeypatch.setattr(review_module, "load_validated_realized_rows", wrapped_loader)

        ks = KillSwitchState()
        ks, _ = _apply_realized_outcomes_to_kill_switch(ks, None, accepted, cfg)

        eq = SimulatedEquityState(1000.0, 1000.0, 1000.0)
        for r in accepted:
            e = r.entry
            e.actual_pnl_usd = r.normalized_net_pnl_usd
            e.actual_pnl_basis = "net"
            update_equity_from_entry(eq, e, cfg)

        summary = review_journal(str(path), cfg)

        assert captured["accepted_sig"] == accepted_sig
        assert captured["accepted_len"] == len(accepted)
        assert captured["rejected_len"] == len(rejected)

        if should_accept:
            assert len(accepted) == 1
            assert ks.consecutive_losses == 1
            assert eq.current_equity_usd == 995.0
            assert summary["current_simulated_equity"] == 995.0
            assert summary["average_realized_pnl_usd"] == -5.0
            assert summary["rejected_realized_rows"] == 0
        else:
            assert len(accepted) == 0
            assert ks.consecutive_losses == 0
            assert eq.current_equity_usd == 1000.0
            assert summary["current_simulated_equity"] == 1000.0
            assert summary["average_realized_pnl_usd"] == 0.0
            assert summary["rejected_realized_rows"] == 1


def test_tolerance_edge_consumption_is_repeatable_e2e(monkeypatch, tmp_path) -> None:
    from btc_rinse_repeat_v1 import review as review_module
    from btc_rinse_repeat_v1.review import review_journal

    cfg = StrategyConfig(starting_equity_usd=1000.0, taker_fee_rate=0.0, slippage_rate=0.0)
    eps = REALIZED_PNL_RECONCILIATION_TOLERANCE_USD
    ts = datetime(2026, 1, 10, tzinfo=timezone.utc)

    row = _entry(
        timestamp=ts,
        decision="Paper Long",
        actual_outcome="loss",
        actual_exit_price=95.0,
        actual_pnl_basis="net",
        actual_pnl_usd=-5.0 - eps,
    )
    path = tmp_path / "edge_repeat.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(row.__dict__, default=str) + "\n")

    original_loader = review_module.load_validated_realized_rows
    snapshots = []

    for _ in range(3):
        accepted, rejected = load_validated_realized_rows(str(path), cfg)
        ks = KillSwitchState()
        ks, _ = _apply_realized_outcomes_to_kill_switch(ks, None, accepted, cfg)

        eq = SimulatedEquityState(1000.0, 1000.0, 1000.0)
        for r in accepted:
            e = r.entry
            e.actual_pnl_usd = r.normalized_net_pnl_usd
            e.actual_pnl_basis = "net"
            update_equity_from_entry(eq, e, cfg)

        captured = {}

        def wrapped_loader(path_arg, cfg_arg):
            acc, rej = original_loader(path_arg, cfg_arg)
            captured["acc_sig"] = _accepted_signature(acc)
            captured["rej_len"] = len(rej)
            return acc, rej

        monkeypatch.setattr(review_module, "load_validated_realized_rows", wrapped_loader)
        summary = review_journal(str(path), cfg)

        snapshots.append(
            (
                _accepted_signature(accepted),
                len(rejected),
                ks.consecutive_losses,
                eq.current_equity_usd,
                summary["current_simulated_equity"],
                summary["rejected_realized_rows"],
                captured["acc_sig"],
                captured["rej_len"],
            )
        )

    assert snapshots[0] == snapshots[1] == snapshots[2]


def test_kill_switch_replay_handles_naive_row_timestamp_with_aware_cursor(tmp_path) -> None:
    cfg = StrategyConfig(kill_switch_consecutive_losses=1, taker_fee_rate=0.0, slippage_rate=0.0)
    # naive timestamp (no timezone) remains a valid persisted row shape
    row = _entry(
        timestamp="2026-01-11T00:00:00",
        decision="Paper Long",
        actual_outcome="loss",
        actual_exit_price=95.0,
        actual_pnl_basis="net",
        actual_pnl_usd=-5.0,
    )

    path = tmp_path / "naive_replay.jsonl"
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(row.__dict__, default=str) + "\n")

    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert len(accepted) == 1
    assert rejected == []

    ks = KillSwitchState()
    # simulate restart cursor persisted as aware UTC timestamp
    cursor = datetime(2026, 1, 10, tzinfo=timezone.utc)
    ks, last_ts = _apply_realized_outcomes_to_kill_switch(ks, cursor, accepted, cfg)
    assert ks.consecutive_losses == 1
    assert last_ts is not None
    assert last_ts.tzinfo is not None


def test_cross_consumer_loss_streak_coherence_from_same_accepted_stream(tmp_path) -> None:
    cfg = StrategyConfig(kill_switch_consecutive_losses=3, taker_fee_rate=0.0, slippage_rate=0.0)
    ts = datetime(2026, 1, 13, tzinfo=timezone.utc)

    rows = [
        _entry(
            timestamp=ts,
            decision="Paper Long",
            actual_outcome="loss",
            actual_exit_price=95.0,
            actual_pnl_basis="net",
            actual_pnl_usd=-5.0,
        ),
        _entry(
            timestamp=ts.replace(hour=1),
            decision="Paper Long",
            actual_outcome="loss",
            actual_exit_price=95.0,
            actual_pnl_basis="net",
            actual_pnl_usd=-5.0,
        ),
        _entry(
            timestamp=ts.replace(hour=2),
            decision="Paper Short",
            actual_outcome="win",
            actual_exit_price=95.0,
            actual_pnl_basis="net",
            actual_pnl_usd=5.0,
        ),
        _entry(
            timestamp=ts.replace(hour=3),
            decision="Paper Long",
            actual_outcome="loss",
            actual_exit_price=95.0,
            actual_pnl_basis="net",
            actual_pnl_usd=-5.0,
        ),
    ]

    path = tmp_path / "cross_consumer.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for e in rows:
            f.write(json.dumps(e.__dict__, default=str) + "\n")

    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert rejected == []

    ks = KillSwitchState()
    ks, _ = _apply_realized_outcomes_to_kill_switch(ks, None, accepted, cfg)

    eq = SimulatedEquityState(1000.0, 1000.0, 1000.0)
    for r in accepted:
        e = r.entry
        e.actual_pnl_usd = r.normalized_net_pnl_usd
        e.actual_pnl_basis = "net"
        update_equity_from_entry(eq, e, cfg)

    assert ks.consecutive_losses == eq.current_consecutive_losses == 1


def test_replay_window_applies_only_events_after_persisted_cursor(tmp_path) -> None:
    cfg = StrategyConfig(kill_switch_consecutive_losses=3, taker_fee_rate=0.0, slippage_rate=0.0)
    t1 = datetime(2026, 1, 14, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 14, 1, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 14, 2, 0, tzinfo=timezone.utc)

    rows = [
        _entry(timestamp=t1, decision="Paper Long", actual_outcome="loss", actual_exit_price=95.0, actual_pnl_basis="net", actual_pnl_usd=-5.0),
        _entry(timestamp=t2, decision="Paper Short", actual_outcome="win", actual_exit_price=95.0, actual_pnl_basis="net", actual_pnl_usd=5.0),
        _entry(timestamp=t3, decision="Paper Long", actual_outcome="loss", actual_exit_price=95.0, actual_pnl_basis="net", actual_pnl_usd=-5.0),
    ]

    path = tmp_path / "cursor_window.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for e in rows:
            f.write(json.dumps(e.__dict__, default=str) + "\n")

    accepted, rejected = load_validated_realized_rows(str(path), cfg)
    assert rejected == []

    cursor = t2
    ks = KillSwitchState()
    ks, last_ts = _apply_realized_outcomes_to_kill_switch(ks, cursor, accepted, cfg)
    assert ks.consecutive_losses == 1
    assert last_ts == t3

    # Equity processed from same replay window should reflect only t3
    eq = SimulatedEquityState(1000.0, 1000.0, 1000.0)
    for r in accepted:
        ts = datetime.fromisoformat(str(r.entry.timestamp))
        if ts <= cursor:
            continue
        e = r.entry
        e.actual_pnl_usd = r.normalized_net_pnl_usd
        e.actual_pnl_basis = "net"
        update_equity_from_entry(eq, e, cfg)
    assert eq.current_equity_usd == 995.0
