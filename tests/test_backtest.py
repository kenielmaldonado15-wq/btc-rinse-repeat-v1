import json
from datetime import datetime, timedelta, timezone

from btc_rinse_repeat_v1.backtest import run_backtest
from btc_rinse_repeat_v1.config import StrategyConfig
from btc_rinse_repeat_v1.journal import load_validated_realized_rows
from btc_rinse_repeat_v1.models import Candle, DecisionResult, FinalDecision, JournalEntry, MarketRegime


def test_backtest_writes_jsonl(monkeypatch, tmp_path) -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)

    candles_4h = [
        Candle(start + timedelta(hours=4 * i), 100 + i, 101 + i, 99 + i, 100 + i, 1)
        for i in range(120)
    ]
    candles_1h = [
        Candle(start + timedelta(hours=i), 100 + i * 0.1, 101 + i * 0.1, 99 + i * 0.1, 100 + i * 0.1, 1)
        for i in range(500)
    ]

    def fake_fetch(config, timeframe, since_utc, limit):
        return candles_4h if timeframe == "4h" else candles_1h

    def fake_run_cycle(**kwargs):
        now = kwargs["now"]
        return DecisionResult(
            timestamp=now,
            market_regime=MarketRegime.TREND_UP,
            trade_bias="Paper Long",
            entry_zone=(100.0, 100.0),
            stop_loss=95.0,
            target=105.0,
            atr=100.0,
            risk_reward_ratio=2.0,
            confidence_score=0.8,
            final_decision=FinalDecision.PAPER_LONG,
            reasoning=["ok"],
            position_size_btc=1.0,
            hard_rule_failures=[],
        )

    monkeypatch.setattr("btc_rinse_repeat_v1.backtest.fetch_historical_candles", fake_fetch)
    monkeypatch.setattr("btc_rinse_repeat_v1.backtest.run_cycle", fake_run_cycle)

    out = tmp_path / "backtest.jsonl"
    path = run_backtest(start.isoformat(), str(out), None)
    assert path.exists()
    assert path.read_text(encoding="utf-8").strip() != ""


def test_backtest_output_is_compatible_with_validated_realized_loader(monkeypatch, tmp_path) -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)

    candles_4h = [
        Candle(start + timedelta(hours=4 * i), 100 + i, 101 + i, 99 + i, 100 + i, 1)
        for i in range(120)
    ]
    candles_1h = [
        Candle(start + timedelta(hours=i), 100.0, 104.0, 99.0, 100.0, 1)
        for i in range(500)
    ]

    for i in range(1, len(candles_1h), 4):
        c = candles_1h[i]
        candles_1h[i] = Candle(c.timestamp, c.open, c.high + 2.0, c.low, c.close, c.volume)

    def fake_fetch(config, timeframe, since_utc, limit):
        return candles_4h if timeframe == "4h" else candles_1h

    def fake_run_cycle(**kwargs):
        now = kwargs["now"]
        return DecisionResult(
            timestamp=now,
            market_regime=MarketRegime.TREND_UP,
            trade_bias="Paper Long",
            entry_zone=(100.0, 100.0),
            stop_loss=95.0,
            target=105.0,
            atr=100.0,
            risk_reward_ratio=2.0,
            confidence_score=0.8,
            final_decision=FinalDecision.PAPER_LONG,
            reasoning=["ok"],
            position_size_btc=1.0,
            hard_rule_failures=[],
        )

    monkeypatch.setattr("btc_rinse_repeat_v1.backtest.fetch_historical_candles", fake_fetch)
    monkeypatch.setattr("btc_rinse_repeat_v1.backtest.run_cycle", fake_run_cycle)

    out = tmp_path / "backtest_loader.jsonl"
    path = run_backtest(start.isoformat(), str(out), None)

    accepted, rejected = load_validated_realized_rows(str(path), StrategyConfig())
    assert len(accepted) > 0
    assert len(rejected) == 0


def test_backtest_rows_do_not_emit_non_journal_fields(monkeypatch, tmp_path) -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)

    candles_4h = [
        Candle(start + timedelta(hours=4 * i), 100 + i, 101 + i, 99 + i, 100 + i, 1)
        for i in range(120)
    ]
    candles_1h = [
        Candle(start + timedelta(hours=i), 100 + i * 0.1, 106 + i * 0.1, 95 + i * 0.1, 100 + i * 0.1, 1)
        for i in range(500)
    ]

    def fake_fetch(config, timeframe, since_utc, limit):
        return candles_4h if timeframe == "4h" else candles_1h

    def fake_run_cycle(**kwargs):
        now = kwargs["now"]
        return DecisionResult(
            timestamp=now,
            market_regime=MarketRegime.TREND_UP,
            trade_bias="Paper Long",
            entry_zone=(100.0, 100.0),
            stop_loss=95.0,
            target=105.0,
            atr=100.0,
            risk_reward_ratio=2.0,
            confidence_score=0.8,
            final_decision=FinalDecision.PAPER_LONG,
            reasoning=["ok"],
            position_size_btc=1.0,
            hard_rule_failures=[],
        )

    monkeypatch.setattr("btc_rinse_repeat_v1.backtest.fetch_historical_candles", fake_fetch)
    monkeypatch.setattr("btc_rinse_repeat_v1.backtest.run_cycle", fake_run_cycle)

    out = tmp_path / "backtest_fields.jsonl"
    path = run_backtest(start.isoformat(), str(out), None)
    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    allowed = set(JournalEntry.__dataclass_fields__.keys())
    assert set(first).issubset(allowed)


def test_backtest_uses_configured_outcome_horizon(monkeypatch, tmp_path) -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"outcome_horizon_hours": 1}), encoding="utf-8")

    candles_4h = [
        Candle(start + timedelta(hours=4 * i), 100 + i, 101 + i, 99 + i, 100 + i, 1)
        for i in range(120)
    ]
    candles_1h = [
        Candle(start + timedelta(hours=i), 100.0, 100.2, 99.8, 100.0, 1)
        for i in range(500)
    ]

    for i in range(2, len(candles_1h), 4):
        c = candles_1h[i]
        candles_1h[i] = Candle(c.timestamp, c.open, 106.0, c.low, c.close, c.volume)

    def fake_fetch(config, timeframe, since_utc, limit):
        return candles_4h if timeframe == "4h" else candles_1h

    def fake_run_cycle(**kwargs):
        now = kwargs["now"]
        return DecisionResult(
            timestamp=now,
            market_regime=MarketRegime.TREND_UP,
            trade_bias="Paper Long",
            entry_zone=(100.0, 100.0),
            stop_loss=95.0,
            target=105.0,
            atr=100.0,
            risk_reward_ratio=2.0,
            confidence_score=0.8,
            final_decision=FinalDecision.PAPER_LONG,
            reasoning=["ok"],
            position_size_btc=1.0,
            hard_rule_failures=[],
        )

    monkeypatch.setattr("btc_rinse_repeat_v1.backtest.fetch_historical_candles", fake_fetch)
    monkeypatch.setattr("btc_rinse_repeat_v1.backtest.run_cycle", fake_run_cycle)

    out = tmp_path / "backtest_horizon.jsonl"
    path = run_backtest(start.isoformat(), str(out), str(cfg_path))

    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert first["actual_outcome"] == "open"


def test_backtest_emits_partial_fill_fields_for_wide_entry_zone(monkeypatch, tmp_path) -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)

    candles_4h = [
        Candle(start + timedelta(hours=4 * i), 100 + i, 101 + i, 99 + i, 100 + i, 1)
        for i in range(120)
    ]
    candles_1h = [
        Candle(start + timedelta(hours=i), 101.0, 102.0, 100.0, 101.0, 1)
        for i in range(500)
    ]

    for i in range(1, len(candles_1h), 4):
        c = candles_1h[i]
        candles_1h[i] = Candle(c.timestamp, c.open, 106.0, c.low, c.close, c.volume)

    def fake_fetch(config, timeframe, since_utc, limit):
        return candles_4h if timeframe == "4h" else candles_1h

    def fake_run_cycle(**kwargs):
        now = kwargs["now"]
        return DecisionResult(
            timestamp=now,
            market_regime=MarketRegime.TREND_UP,
            trade_bias="Paper Long",
            entry_zone=(99.0, 101.0),
            stop_loss=95.0,
            target=105.0,
            atr=100.0,
            risk_reward_ratio=2.0,
            confidence_score=0.8,
            final_decision=FinalDecision.PAPER_LONG,
            reasoning=["ok"],
            position_size_btc=2.0,
            hard_rule_failures=[],
        )

    monkeypatch.setattr("btc_rinse_repeat_v1.backtest.fetch_historical_candles", fake_fetch)
    monkeypatch.setattr("btc_rinse_repeat_v1.backtest.run_cycle", fake_run_cycle)

    out = tmp_path / "backtest_partial.jsonl"
    path = run_backtest(start.isoformat(), str(out), None)

    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert first["entry_fill_fraction"] == 0.5
    assert first["executed_size_btc"] == 1.0
    assert first["unfilled_size_btc"] == 1.0


def test_backtest_marks_open_when_only_partial_exit_occurs(monkeypatch, tmp_path) -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)

    candles_4h = [
        Candle(start + timedelta(hours=4 * i), 100 + i, 101 + i, 99 + i, 100 + i, 1)
        for i in range(120)
    ]
    candles_1h = [
        Candle(start + timedelta(hours=i), 100.0, 100.2, 99.8, 100.0, 1)
        for i in range(500)
    ]
    c = candles_1h[242]
    candles_1h[242] = Candle(c.timestamp, c.open, 105.5, c.low, c.close, c.volume)

    def fake_fetch(config, timeframe, since_utc, limit):
        return candles_4h if timeframe == "4h" else candles_1h

    def fake_run_cycle(**kwargs):
        now = kwargs["now"]
        return DecisionResult(
            timestamp=now,
            market_regime=MarketRegime.TREND_UP,
            trade_bias="Paper Long",
            entry_zone=(100.0, 100.0),
            stop_loss=95.0,
            target=105.0,
            atr=100.0,
            risk_reward_ratio=2.0,
            confidence_score=0.8,
            final_decision=FinalDecision.PAPER_LONG,
            reasoning=["ok"],
            position_size_btc=2.0,
            hard_rule_failures=[],
        )

    monkeypatch.setattr("btc_rinse_repeat_v1.backtest.fetch_historical_candles", fake_fetch)
    monkeypatch.setattr("btc_rinse_repeat_v1.backtest.run_cycle", fake_run_cycle)

    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps({"partial_take_profit_fraction": 0.5}), encoding="utf-8")

    out = tmp_path / "backtest_partial_exit_open.jsonl"
    path = run_backtest(start.isoformat(), str(out), str(cfg_path))

    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert first["actual_outcome"] == "open"
    assert first["target_exit_size_btc"] == 1.0
    assert first["residual_position_size_btc"] == 1.0


def test_backtest_emits_residual_lifecycle_fields(monkeypatch, tmp_path) -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)

    candles_4h = [
        Candle(start + timedelta(hours=4 * i), 100 + i, 101 + i, 99 + i, 100 + i, 1)
        for i in range(120)
    ]
    candles_1h = [
        Candle(start + timedelta(hours=i), 100.0, 100.1, 99.9, 100.0, 1)
        for i in range(500)
    ]

    def fake_fetch(config, timeframe, since_utc, limit):
        return candles_4h if timeframe == "4h" else candles_1h

    def fake_run_cycle(**kwargs):
        now = kwargs["now"]
        return DecisionResult(
            timestamp=now,
            market_regime=MarketRegime.TREND_UP,
            trade_bias="Paper Long",
            entry_zone=(100.0, 100.0),
            stop_loss=95.0,
            target=105.0,
            atr=100.0,
            risk_reward_ratio=2.0,
            confidence_score=0.8,
            final_decision=FinalDecision.PAPER_LONG,
            reasoning=["ok"],
            position_size_btc=1.0,
            hard_rule_failures=[],
        )

    monkeypatch.setattr("btc_rinse_repeat_v1.backtest.fetch_historical_candles", fake_fetch)
    monkeypatch.setattr("btc_rinse_repeat_v1.backtest.run_cycle", fake_run_cycle)

    out = tmp_path / "backtest_lifecycle_fields.jsonl"
    path = run_backtest(start.isoformat(), str(out), None)
    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert "residual_lifecycle_stage" in first
    assert "entry_fill_timestamp" in first
    assert "realized_gross_so_far_usd" in first
