from datetime import datetime

from btc_rinse_repeat_v1.journal import append_journal_jsonl
from btc_rinse_repeat_v1.models import FinalDecision, JournalEntry
from btc_rinse_repeat_v1.risk import conservative_entry


def test_conservative_entry_uses_worst_case() -> None:
    zone = (100.0, 110.0)
    assert conservative_entry(zone, FinalDecision.PAPER_LONG) == 110.0
    assert conservative_entry(zone, FinalDecision.PAPER_SHORT) == 100.0


def test_journal_jsonl_append(tmp_path) -> None:
    entry = JournalEntry(
        timestamp=datetime.utcnow(),
        market_regime="Trend Up",
        decision="Paper Long",
        entry="(100, 110)",
        stop="95",
        target="120",
        projected_rr=2.0,
        confidence=0.7,
        reasoning="test",
        what_would_have_made_this_stand_aside="weak volume",
    )
    path = tmp_path / "journal.jsonl"
    append_journal_jsonl(entry, str(path))
    assert path.exists()
    assert "Paper Long" in path.read_text()
