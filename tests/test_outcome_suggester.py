import json
from datetime import datetime, timedelta, timezone

from btc_rinse_repeat_v1.config import StrategyConfig
from btc_rinse_repeat_v1.journal import load_validated_realized_rows, validate_realized_row
from btc_rinse_repeat_v1.models import Candle, JournalEntry
from btc_rinse_repeat_v1.outcome_suggester import apply_outcome_patch_to_journal, suggest_outcome_update


def test_outcome_suggester_writes_patch(monkeypatch, tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"partial_take_profit_fraction": 0.5}), encoding="utf-8")
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 2.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 200.0,
    }
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def fake_fetch(*args, **kwargs):
        return [
            Candle(ts + timedelta(hours=1), 100, 104, 99, 103, 1),
            Candle(ts + timedelta(hours=2), 103, 106, 102, 105, 1),
        ]

    monkeypatch.setattr("btc_rinse_repeat_v1.outcome_suggester.fetch_historical_candles", fake_fetch)

    out = tmp_path / "patch.json"
    patch = suggest_outcome_update(ts.isoformat(), str(journal), str(out), None)
    assert patch["actual_outcome"] == "win"
    assert patch["actual_pnl_basis"] == "net"
    assert "friction_fees_usd" not in patch
    assert out.exists()


def test_outcome_suggester_returns_open_when_entry_never_fills(monkeypatch, tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 1.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
    }
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def fake_fetch(*args, **kwargs):
        # price never reaches 100, but touches target-like highs relative to other levels
        return [
            Candle(ts + timedelta(hours=1), 101.0, 106.0, 101.0, 105.5, 1),
            Candle(ts + timedelta(hours=2), 101.0, 104.0, 101.0, 103.0, 1),
        ]

    monkeypatch.setattr("btc_rinse_repeat_v1.outcome_suggester.fetch_historical_candles", fake_fetch)

    patch = suggest_outcome_update(ts.isoformat(), str(journal), str(tmp_path / "patch_open.json"), None)
    assert patch["actual_outcome"] == "open"
    assert patch["review_note"] == "Entry not filled in fetched window"


def test_outcome_suggester_uses_entry_then_exit_sequence_not_pre_entry_touches(monkeypatch, tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 1.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
    }
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def fake_fetch(*args, **kwargs):
        return [
            # pre-entry candle touches stop and target range but never entry=100
            Candle(ts + timedelta(hours=1), 101.0, 106.0, 94.0, 102.0, 1),
            # entry fill occurs later
            Candle(ts + timedelta(hours=2), 100.0, 100.2, 99.8, 100.1, 1),
            # then stop gets hit
            Candle(ts + timedelta(hours=3), 100.0, 100.1, 95.0, 96.0, 1),
        ]

    monkeypatch.setattr("btc_rinse_repeat_v1.outcome_suggester.fetch_historical_candles", fake_fetch)

    patch = suggest_outcome_update(ts.isoformat(), str(journal), str(tmp_path / "patch_seq.json"), None)
    assert patch["actual_outcome"] == "loss"
    assert patch["actual_exit_price"] == 95.0


def test_outcome_suggester_ignores_fill_candle_favorable_touch_as_ambiguous(monkeypatch, tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 1.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
    }
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def fake_fetch(*args, **kwargs):
        return [
            # entry and target are both reachable this candle, but ordering is unknown
            Candle(ts + timedelta(hours=1), 100.0, 106.0, 100.0, 105.0, 1),
            # only here does target get a non-ambiguous hit after entry fill candle
            Candle(ts + timedelta(hours=2), 104.0, 106.0, 103.0, 105.5, 1),
        ]

    monkeypatch.setattr("btc_rinse_repeat_v1.outcome_suggester.fetch_historical_candles", fake_fetch)

    patch = suggest_outcome_update(ts.isoformat(), str(journal), str(tmp_path / "patch_ambiguous.json"), None)
    assert patch["actual_outcome"] == "win"
    assert patch["held_duration_hours"] == 1.0


def test_outcome_suggester_applies_conservative_gap_fill_for_stop(monkeypatch, tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 1.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
    }
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def fake_fetch(*args, **kwargs):
        return [
            Candle(ts + timedelta(hours=1), 100.0, 100.5, 99.5, 100.2, 1),
            # gap-down open below stop should realize worse-than-stop fill
            Candle(ts + timedelta(hours=2), 94.0, 96.0, 93.0, 94.5, 1),
        ]

    monkeypatch.setattr("btc_rinse_repeat_v1.outcome_suggester.fetch_historical_candles", fake_fetch)

    patch = suggest_outcome_update(ts.isoformat(), str(journal), str(tmp_path / "patch_gap.json"), None)
    assert patch["actual_outcome"] == "loss"
    assert patch["actual_exit_price"] == 94.0


def test_outcome_suggester_respects_configured_horizon_for_exit(monkeypatch, tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"outcome_horizon_hours": 1}), encoding="utf-8")

    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 1.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
    }
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def fake_fetch(*args, **kwargs):
        return [
            Candle(ts + timedelta(hours=1), 100.0, 100.2, 99.8, 100.1, 1),
            Candle(ts + timedelta(hours=2), 100.0, 106.0, 99.5, 105.5, 1),
        ]

    monkeypatch.setattr("btc_rinse_repeat_v1.outcome_suggester.fetch_historical_candles", fake_fetch)

    patch = suggest_outcome_update(ts.isoformat(), str(journal), str(tmp_path / "patch_horizon.json"), str(cfg))
    assert patch["actual_outcome"] == "open"


def test_outcome_suggester_scales_pnl_by_partial_entry_fill(monkeypatch, tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"partial_take_profit_fraction": 1.0}), encoding="utf-8")
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(99.0,101.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 2.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 200.0,
    }
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def fake_fetch(*args, **kwargs):
        return [
            # only half the entry zone overlaps (100 to 101 of 99 to 101)
            Candle(ts + timedelta(hours=1), 101.0, 102.0, 100.0, 101.5, 1),
            Candle(ts + timedelta(hours=2), 101.5, 106.0, 101.0, 105.5, 1),
        ]

    monkeypatch.setattr("btc_rinse_repeat_v1.outcome_suggester.fetch_historical_candles", fake_fetch)

    patch = suggest_outcome_update(ts.isoformat(), str(journal), str(tmp_path / "patch_partial.json"), str(cfg))
    assert patch["actual_outcome"] == "win"
    assert patch["entry_fill_fraction"] == 0.5
    assert patch["executed_size_btc"] == 1.0
    assert patch["unfilled_size_btc"] == 1.0
    assert patch["actual_pnl_usd"] < 5.0




def test_outcome_suggester_marks_open_when_partial_target_hit_but_residual_unclosed(monkeypatch, tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"partial_take_profit_fraction": 0.5}), encoding="utf-8")
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 2.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 200.0,
    }
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def fake_fetch(*args, **kwargs):
        return [
            Candle(ts + timedelta(hours=1), 100.0, 100.1, 99.9, 100.0, 1),
            Candle(ts + timedelta(hours=2), 100.0, 105.5, 99.5, 104.5, 1),
        ]

    monkeypatch.setattr("btc_rinse_repeat_v1.outcome_suggester.fetch_historical_candles", fake_fetch)

    patch = suggest_outcome_update(ts.isoformat(), str(journal), str(tmp_path / "patch_partial_open.json"), str(cfg))
    assert patch["actual_outcome"] == "open"
    assert patch["target_exit_size_btc"] == 1.0
    assert patch["residual_position_size_btc"] == 1.0
    assert "actual_pnl_usd" not in patch


def test_outcome_suggester_staged_target_then_stop_produces_weighted_exit_and_loss(monkeypatch, tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"partial_take_profit_fraction": 0.5}), encoding="utf-8")
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 2.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 200.0,
    }
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def fake_fetch(*args, **kwargs):
        return [
            Candle(ts + timedelta(hours=1), 100.0, 100.1, 99.9, 100.0, 1),
            Candle(ts + timedelta(hours=2), 100.0, 106.0, 99.8, 105.1, 1),
            Candle(ts + timedelta(hours=3), 95.0, 96.0, 94.0, 94.5, 1),
        ]

    monkeypatch.setattr("btc_rinse_repeat_v1.outcome_suggester.fetch_historical_candles", fake_fetch)

    patch = suggest_outcome_update(ts.isoformat(), str(journal), str(tmp_path / "patch_staged.json"), str(cfg))
    assert patch["actual_outcome"] == "loss"
    assert patch["target_exit_size_btc"] == 1.0
    assert patch["stop_exit_size_btc"] == 1.0
    assert patch["residual_position_size_btc"] == 0.0
    assert patch["actual_exit_price"] == 100.0
    assert patch["held_duration_hours"] == 2.0


def test_outcome_suggester_respects_partial_take_profit_fraction_override(monkeypatch, tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"partial_take_profit_fraction": 0.25}), encoding="utf-8")

    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 2.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 200.0,
    }
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def fake_fetch(*args, **kwargs):
        return [
            Candle(ts + timedelta(hours=1), 100.0, 100.1, 99.9, 100.0, 1),
            Candle(ts + timedelta(hours=2), 100.0, 106.0, 99.8, 105.1, 1),
        ]

    monkeypatch.setattr("btc_rinse_repeat_v1.outcome_suggester.fetch_historical_candles", fake_fetch)

    patch = suggest_outcome_update(ts.isoformat(), str(journal), str(tmp_path / "patch_cfg_tp.json"), str(cfg))
    assert patch["actual_outcome"] == "open"
    assert patch["target_exit_size_btc"] == 0.5
    assert patch["residual_position_size_btc"] == 1.5
    assert patch["exit_fill_count"] == 1

def test_outcome_suggester_patch_fields_validate_without_rounding_mismatch(monkeypatch, tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.03,100.03)",
        "stop": "99.71",
        "target": "100.37",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 0.3333,
        "executed_entry_price": 100.03,
        "executed_notional_usd": 33.339999,
    }
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def fake_fetch(*args, **kwargs):
        return [
            Candle(ts + timedelta(hours=1), 100.03, 100.2, 100.0, 100.1, 1),
            Candle(ts + timedelta(hours=2), 100.1, 100.5, 100.0, 100.4, 1),
        ]

    monkeypatch.setattr("btc_rinse_repeat_v1.outcome_suggester.fetch_historical_candles", fake_fetch)

    patch = suggest_outcome_update(ts.isoformat(), str(journal), str(tmp_path / "patch.json"), None)
    entry = JournalEntry(**{**row, **patch})
    result = validate_realized_row(entry, StrategyConfig())
    assert result.status in {"accepted_net", "accepted_gross_normalized"}


def test_outcome_patch_merged_row_is_loader_accepted_e2e(monkeypatch, tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 1.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
    }
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def fake_fetch(*args, **kwargs):
        return [
            Candle(ts + timedelta(hours=1), 100, 104, 99, 103, 1),
            Candle(ts + timedelta(hours=2), 103, 106, 102, 105, 1),
        ]

    monkeypatch.setattr("btc_rinse_repeat_v1.outcome_suggester.fetch_historical_candles", fake_fetch)

    patch = suggest_outcome_update(ts.isoformat(), str(journal), str(tmp_path / "patch.json"), None)
    merged = {**row, **patch}
    journal.write_text(json.dumps(merged, default=str) + "\n", encoding="utf-8")

    accepted, rejected = load_validated_realized_rows(str(journal), StrategyConfig())
    assert len(accepted) == 1
    assert rejected == []


def test_apply_outcome_patch_overwrites_loader_critical_fields_without_stale_conflicts(tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    original = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 1.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
        "actual_outcome": "open",
        "actual_exit_price": None,
        "actual_pnl_usd": None,
        "actual_pnl_basis": "net",
    }
    journal.write_text(json.dumps(original) + "\n", encoding="utf-8")

    patch = {
        "timestamp": ts.isoformat(),
        "actual_outcome": "win",
        "actual_exit_price": 105.0,
        "actual_pnl_usd": 5.0,
        "actual_pnl_basis": "net",
        "executed_notional_usd": 100.0,
    }

    merged = apply_outcome_patch_to_journal(patch, str(journal), StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0))
    assert merged.actual_outcome == "win"
    assert merged.actual_exit_price == 105.0
    assert merged.actual_pnl_usd == 5.0
    assert merged.actual_pnl_basis == "net"

    persisted = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    assert persisted["actual_outcome"] == "win"
    assert persisted["actual_exit_price"] == 105.0
    assert persisted["actual_pnl_usd"] == 5.0
    assert persisted["executed_notional_usd"] == 100.0
    assert persisted["decision"] == "Paper Long"


def test_apply_outcome_patch_fails_closed_when_required_realized_fields_missing(tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    base = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 1.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
    }
    journal.write_text(json.dumps(base) + "\n", encoding="utf-8")

    bad_patch = {
        "timestamp": ts.isoformat(),
        "actual_outcome": "win",
        "actual_pnl_basis": "net",
    }
    try:
        apply_outcome_patch_to_journal(bad_patch, str(journal), StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0))
        assert False, "expected merge failure"
    except RuntimeError as exc:
        assert "missing required realized fields" in str(exc)

    persisted = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    assert persisted == base


def test_apply_outcome_patch_fails_closed_when_merge_would_be_invalid(tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    base = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 1.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
    }
    journal.write_text(json.dumps(base) + "\n", encoding="utf-8")

    incoherent_patch = {
        "timestamp": ts.isoformat(),
        "actual_outcome": "win",
        "actual_exit_price": 95.0,
        "actual_pnl_usd": -5.0,
        "actual_pnl_basis": "net",
        "executed_notional_usd": 100.0,
    }
    try:
        apply_outcome_patch_to_journal(incoherent_patch, str(journal), StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0))
        assert False, "expected merge failure"
    except RuntimeError as exc:
        assert "Patch merge rejected by realized validation" in str(exc)

    persisted = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    assert persisted == base


def test_applied_patch_consumes_identically_to_canonical_row(tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)

    base = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 1.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
    }
    patch = {
        "timestamp": ts.isoformat(),
        "actual_outcome": "win",
        "actual_exit_price": 105.0,
        "actual_pnl_usd": 5.0,
        "actual_pnl_basis": "net",
        "executed_notional_usd": 100.0,
    }

    merged_path = tmp_path / "merged.jsonl"
    merged_path.write_text(json.dumps(base) + "\n", encoding="utf-8")
    apply_outcome_patch_to_journal(patch, str(merged_path), cfg)

    canonical_path = tmp_path / "canonical.jsonl"
    canonical_path.write_text(json.dumps({**base, **patch}) + "\n", encoding="utf-8")

    a1, r1 = load_validated_realized_rows(str(merged_path), cfg)
    a2, r2 = load_validated_realized_rows(str(canonical_path), cfg)

    assert len(r1) == len(r2) == 0
    assert len(a1) == len(a2) == 1
    assert a1[0].normalized_net_pnl_usd == a2[0].normalized_net_pnl_usd
    assert a1[0].entry.actual_outcome == a2[0].entry.actual_outcome


def test_apply_outcome_patch_fails_closed_when_timestamp_matches_multiple_rows(tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    base = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 1.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
    }
    journal.write_text(json.dumps(base) + "\n" + json.dumps(base) + "\n", encoding="utf-8")

    patch = {
        "timestamp": ts.isoformat(),
        "actual_outcome": "win",
        "actual_exit_price": 105.0,
        "actual_pnl_usd": 5.0,
        "actual_pnl_basis": "net",
        "executed_notional_usd": 100.0,
    }
    try:
        apply_outcome_patch_to_journal(patch, str(journal), StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0))
        assert False, "expected merge failure"
    except RuntimeError as exc:
        assert "Expected exactly 1 journal row" in str(exc)


def test_apply_outcome_patch_fails_closed_for_non_realized_outcome_with_stale_realized_fields(tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 1.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
        "actual_outcome": "win",
        "actual_exit_price": 105.0,
        "actual_pnl_usd": 5.0,
        "actual_pnl_basis": "net",
    }
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    patch = {
        "timestamp": ts.isoformat(),
        "actual_outcome": "open",
        "review_note": "No stop/target hit in fetched window",
    }
    try:
        apply_outcome_patch_to_journal(patch, str(journal), StrategyConfig())
        assert False, "expected merge failure"
    except RuntimeError as exc:
        assert "non-realized outcome cannot retain realized pnl/exit fields" in str(exc)


def test_apply_outcome_patch_is_idempotent_for_same_valid_patch(tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    cfg = StrategyConfig(taker_fee_rate=0.0, slippage_rate=0.0)
    journal = tmp_path / "journal.jsonl"
    base = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 1.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
    }
    journal.write_text(json.dumps(base) + "\n", encoding="utf-8")

    patch = {
        "timestamp": ts.isoformat(),
        "actual_outcome": "win",
        "actual_exit_price": 105.0,
        "actual_pnl_usd": 5.0,
        "actual_pnl_basis": "net",
        "executed_notional_usd": 100.0,
    }

    apply_outcome_patch_to_journal(patch, str(journal), cfg)
    first = journal.read_text(encoding="utf-8")
    apply_outcome_patch_to_journal(patch, str(journal), cfg)
    second = journal.read_text(encoding="utf-8")
    assert first == second

    accepted, rejected = load_validated_realized_rows(str(journal), cfg)
    assert len(accepted) == 1
    assert rejected == []


def test_outcome_suggester_carries_residual_lifecycle_across_windows(monkeypatch, tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"partial_take_profit_fraction": 0.5, "outcome_horizon_hours": 2}), encoding="utf-8")
    journal = tmp_path / "journal.jsonl"
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 2.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 200.0,
    }
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def first_fetch(*args, **kwargs):
        return [
            Candle(ts + timedelta(hours=1), 100.0, 100.2, 99.8, 100.0, 1),
            Candle(ts + timedelta(hours=2), 100.0, 106.0, 99.5, 105.0, 1),
        ]

    monkeypatch.setattr("btc_rinse_repeat_v1.outcome_suggester.fetch_historical_candles", first_fetch)
    patch1 = suggest_outcome_update(ts.isoformat(), str(journal), str(tmp_path / "p1.json"), str(cfg))
    assert patch1["actual_outcome"] == "open"
    assert patch1["residual_position_size_btc"] == 1.0
    assert patch1["residual_lifecycle_stage"] == 1
    assert patch1["position_id"]

    merged = {**row, **patch1}
    journal.write_text(json.dumps(merged, default=str) + "\n", encoding="utf-8")

    def second_fetch(*args, **kwargs):
        return [
            Candle(ts + timedelta(hours=3), 95.0, 96.0, 94.0, 95.2, 1),
        ]

    monkeypatch.setattr("btc_rinse_repeat_v1.outcome_suggester.fetch_historical_candles", second_fetch)
    patch2 = suggest_outcome_update(ts.isoformat(), str(journal), str(tmp_path / "p2.json"), str(cfg))
    assert patch2["actual_outcome"] == "loss"
    assert patch2["realized_exit_size_btc"] == 2.0
    assert patch2["residual_position_size_btc"] == 0.0
    assert patch2["residual_lifecycle_stage"] == 2
    assert patch2["held_duration_hours"] == 2.0
    assert patch2["position_id"] == patch1["position_id"]


def test_outcome_suggester_carry_mode_does_not_double_count_prior_partial_exit(monkeypatch, tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"partial_take_profit_fraction": 0.5, "outcome_horizon_hours": 3, "taker_fee_rate": 0.0, "slippage_rate": 0.0}), encoding="utf-8")
    journal = tmp_path / "journal.jsonl"
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 2.0,
        "executed_entry_price": 100.0,
        "executed_notional_usd": 200.0,
        "actual_outcome": "open",
        "executed_size_btc": 2.0,
        "unfilled_size_btc": 0.0,
        "target_exit_size_btc": 1.0,
        "stop_exit_size_btc": 0.0,
        "residual_position_size_btc": 1.0,
        "realized_exit_size_btc": 1.0,
        "exit_fill_count": 1,
        "residual_lifecycle_stage": 1,
        "entry_fill_timestamp": (ts + timedelta(hours=1)).isoformat(),
        "residual_last_event_ts": (ts + timedelta(hours=2)).isoformat(),
        "realized_gross_so_far_usd": 5.0,
    }
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def fake_fetch(*args, **kwargs):
        return [
            Candle(ts + timedelta(hours=3), 95.0, 95.5, 94.0, 94.5, 1),
        ]

    monkeypatch.setattr("btc_rinse_repeat_v1.outcome_suggester.fetch_historical_candles", fake_fetch)
    patch = suggest_outcome_update(ts.isoformat(), str(journal), str(tmp_path / "carry_patch.json"), str(cfg))
    assert patch["actual_outcome"] == "breakeven"
    assert patch["realized_exit_size_btc"] == 2.0
    assert patch["target_exit_size_btc"] == 1.0
    assert patch["stop_exit_size_btc"] == 1.0


def test_apply_outcome_patch_rejects_position_identity_mutation(tmp_path) -> None:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    journal = tmp_path / "journal.jsonl"
    row = {
        "timestamp": ts.isoformat(),
        "market_regime": "Trend Up",
        "decision": "Paper Long",
        "entry": "(100.0,100.0)",
        "stop": "95.0",
        "target": "105.0",
        "projected_rr": 2.0,
        "confidence": 0.7,
        "reasoning": "x",
        "what_would_have_made_this_stand_aside": "x",
        "position_size_btc": 1.0,
        "position_id": "pos-original",
        "executed_entry_price": 100.0,
        "executed_notional_usd": 100.0,
    }
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")

    patch = {
        "timestamp": ts.isoformat(),
        "position_id": "pos-mutated",
        "actual_outcome": "open",
    }
    try:
        apply_outcome_patch_to_journal(patch, str(journal), StrategyConfig())
        assert False, "expected identity mutation rejection"
    except RuntimeError as exc:
        assert "Position identity mismatch" in str(exc)
