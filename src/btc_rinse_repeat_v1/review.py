from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .config import StrategyConfig
from .equity import update_equity_from_entry
from .journal import load_validated_realized_rows
from .models import JournalEntry, SimulatedEquityState


def _load_entries(path: str) -> list[JournalEntry]:
    p = Path(path)
    if not p.exists():
        return []

    entries: list[JournalEntry] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        entries.append(JournalEntry(**row))
    return entries


def review_journal(path: str = "journal/journal.jsonl", config: StrategyConfig | None = None) -> dict:
    cfg = config or StrategyConfig()
    entries = _load_entries(path)
    legacy_rows = [e for e in entries if not str(getattr(e, "position_id", "") or "").strip()]
    accepted_realized, rejected_realized = load_validated_realized_rows(path, cfg)

    decision_counts = Counter(e.decision for e in entries)
    total = len(entries)
    position_ids = [str(e.position_id) for e in entries if e.position_id]
    unique_position_ids = sorted(set(position_ids))
    open_position_ids = sorted(
        {
            str(e.position_id)
            for e in entries
            if e.position_id and (str(e.position_status).lower() == "open" or (e.residual_position_size_btc or 0.0) > 0)
        }
    )

    realized_entries = [r.entry for r in accepted_realized]
    lineage_tier_counts = Counter(getattr(r, "lineage_trust_tier", "native_position_lineage") for r in accepted_realized)
    wins = [e for e in realized_entries if e.actual_outcome.lower() == "win"]
    win_rate = (len(wins) / len(realized_entries) * 100) if realized_entries else 0.0

    by_regime_totals = Counter(e.market_regime for e in realized_entries)
    by_regime_wins = Counter(e.market_regime for e in wins)
    win_rate_by_regime = {
        regime: (by_regime_wins[regime] / count * 100 if count else 0.0)
        for regime, count in by_regime_totals.items()
    }

    rr_values = [e.projected_rr for e in entries if e.projected_rr is not None]
    avg_projected_rr = sum(rr_values) / len(rr_values) if rr_values else 0.0

    realized_net = [r.normalized_net_pnl_usd for r in accepted_realized if r.normalized_net_pnl_usd is not None]
    avg_realized_pnl = sum(realized_net) / len(realized_net) if realized_net else 0.0

    legacy_policy = str(getattr(cfg, "runtime_legacy_row_policy", "upgrade_deterministic") or "upgrade_deterministic").lower().strip()
    low_context_policy = str(getattr(cfg, "runtime_low_context_legacy_policy", "bound_event_only") or "bound_event_only").lower().strip()
    sparse_policy = str(getattr(cfg, "runtime_sparse_history_policy", "reconstruct_supportable") or "reconstruct_supportable").lower().strip()
    rejected_legacy_event_only_rows = len([r for r in rejected_realized if getattr(r, "reason", "") == "legacy_row_rejected_by_policy"])
    rejected_low_context_rows = len([r for r in rejected_realized if getattr(r, "reason", "") in {"low_context_legacy_row_rejected_by_policy", "sparse_supportable_row_rejected_by_policy"}])
    rejected_sparse_supportable_rows = len([r for r in rejected_realized if getattr(r, "reason", "") == "sparse_supportable_row_rejected_by_policy"])
    replay_excluded_legacy_event_only_rows = (
        lineage_tier_counts.get("legacy_event_only", 0) + rejected_legacy_event_only_rows
    ) if legacy_policy == "reject" else 0
    replay_excluded_low_context_rows = (
        lineage_tier_counts.get("low_context_legacy_event_only", 0) + rejected_low_context_rows
    ) if low_context_policy == "reject" else 0
    replay_excluded_sparse_bounded_rows = lineage_tier_counts.get("sparse_event_only_bounded", 0) if sparse_policy == "reject" else 0
    replay_eligible_realized_rows = max(0, len(accepted_realized) - replay_excluded_legacy_event_only_rows - replay_excluded_low_context_rows - replay_excluded_sparse_bounded_rows)

    equity = SimulatedEquityState(
        starting_equity_usd=cfg.starting_equity_usd,
        current_equity_usd=cfg.starting_equity_usd,
        high_water_mark_usd=cfg.starting_equity_usd,
    )
    for row in accepted_realized:
        normalized_entry = row.entry
        normalized_entry.actual_pnl_usd = row.normalized_net_pnl_usd
        normalized_entry.actual_pnl_basis = "net"
        update_equity_from_entry(equity, normalized_entry, cfg)

    summary = {
        "total_decisions": total,
        "decision_counts": dict(decision_counts),
        "win_rate": round(win_rate, 2),
        "win_rate_by_market_regime": {k: round(v, 2) for k, v in win_rate_by_regime.items()},
        "average_projected_rr": round(avg_projected_rr, 3),
        "average_realized_pnl_usd": round(avg_realized_pnl, 2),
        "current_simulated_equity": round(equity.current_equity_usd, 2),
        "high_water_mark_usd": round(equity.high_water_mark_usd, 2),
        "max_drawdown_usd": round(equity.max_drawdown_usd, 2),
        "max_drawdown_pct": round(equity.max_drawdown_pct, 2),
        "max_consecutive_losses": equity.max_consecutive_losses,
        "rejected_realized_rows": len(rejected_realized),
        "open_residual_rows": len(
            [e for e in entries if str(e.actual_outcome).lower() == "open" and (e.residual_position_size_btc or 0.0) > 0]
        ),
        "open_residual_exposure_btc": round(
            sum((e.residual_position_size_btc or 0.0) for e in entries if str(e.actual_outcome).lower() == "open"),
            8,
        ),
        "unique_position_count": len(unique_position_ids),
        "open_position_count": len(open_position_ids),
        "closed_position_count": max(0, len(unique_position_ids) - len(open_position_ids)),
        "legacy_realized_rows": len([e for e in legacy_rows if str(e.actual_outcome).lower() in {"win", "loss", "breakeven"}]),
        "legacy_open_rows": len([e for e in legacy_rows if str(e.actual_outcome).lower() == "open"]),
        "native_position_lineage_rows": lineage_tier_counts.get("native_position_lineage", 0),
        "legacy_lineage_normalized_rows": lineage_tier_counts.get("legacy_lineage_normalized", 0),
        "legacy_event_only_rows": lineage_tier_counts.get("legacy_event_only", 0),
        "legacy_lineage_rejected_rows": len([r for r in rejected_realized if getattr(r, "lineage_trust_tier", "") == "legacy_lineage_rejected"]),
        "sparse_lineage_normalized_rows": lineage_tier_counts.get("sparse_lineage_normalized", 0),
        "sparse_event_only_bounded_rows": lineage_tier_counts.get("sparse_event_only_bounded", 0),
        "low_context_legacy_event_only_rows": lineage_tier_counts.get("low_context_legacy_event_only", 0),
        "manual_remediated_attested_lineage_rows": lineage_tier_counts.get("manual_remediated_attested_lineage", 0),
        "manual_remediated_internal_attested_lineage_rows": lineage_tier_counts.get("manual_remediated_internal_attested_lineage", 0),
        "manual_remediation_revoked_rows": len([r for r in rejected_realized if getattr(r, "lineage_trust_tier", "") == "manual_remediation_revoked"]),
        "manual_remediation_indeterminate_rows": len([r for r in rejected_realized if getattr(r, "lineage_trust_tier", "") == "manual_remediation_indeterminate"]),
        "runtime_legacy_row_policy": legacy_policy,
        "runtime_low_context_legacy_policy": low_context_policy,
        "runtime_sparse_history_policy": sparse_policy,
        "replay_eligible_realized_rows": replay_eligible_realized_rows,
        "replay_excluded_legacy_event_only_rows": replay_excluded_legacy_event_only_rows,
        "replay_excluded_low_context_rows": replay_excluded_low_context_rows,
        "replay_excluded_sparse_bounded_rows": replay_excluded_sparse_bounded_rows,
        "rejected_sparse_supportable_rows": rejected_sparse_supportable_rows,
    }
    return summary


def main() -> None:
    summary = review_journal()
    print("Total decisions:", summary["total_decisions"])
    counts = summary["decision_counts"]
    print("Paper Long:", counts.get("Paper Long", 0))
    print("Paper Short:", counts.get("Paper Short", 0))
    print("Stand Aside:", counts.get("Stand Aside", 0))
    print("Win rate (%):", summary["win_rate"])
    print("Win rate by regime:", summary["win_rate_by_market_regime"])
    print("Average projected RR:", summary["average_projected_rr"])
    print("Average realized PnL (USD):", summary["average_realized_pnl_usd"])
    print("Current simulated equity:", summary["current_simulated_equity"])
    print("High-water mark (USD):", summary["high_water_mark_usd"])
    print("Max drawdown (USD):", summary["max_drawdown_usd"])
    print("Max drawdown (%):", summary["max_drawdown_pct"])
    print("Max consecutive losses:", summary["max_consecutive_losses"])
    print("Rejected realized rows:", summary["rejected_realized_rows"])


if __name__ == "__main__":
    main()
