from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import KillSwitchState


def _parse_datetime_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class RuntimeState:
    last_run_utc: datetime | None = None
    last_processed_outcome_ts: datetime | None = None
    kill_switch: KillSwitchState | None = None
    processed_closed_position_ids: list[str] | None = None
    processed_legacy_event_keys: list[str] | None = None


def load_runtime_state(path: str = "runtime_state/state.json") -> RuntimeState:
    p = Path(path)
    if not p.exists():
        return RuntimeState(kill_switch=KillSwitchState())

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("invalid_runtime_state_file") from exc
    value = raw.get("last_run_utc")
    processed = raw.get("last_processed_outcome_ts")
    kill = raw.get("kill_switch") or {}
    stand_aside_until = kill.get("stand_aside_until")

    try:
        return RuntimeState(
            last_run_utc=_parse_datetime_utc(value),
            last_processed_outcome_ts=_parse_datetime_utc(processed),
            kill_switch=KillSwitchState(
                consecutive_losses=int(kill.get("consecutive_losses", 0)),
                stand_aside_until=_parse_datetime_utc(stand_aside_until),
                review_required=bool(kill.get("review_required", False)),
            ),
            processed_closed_position_ids=[str(x) for x in (raw.get("processed_closed_position_ids") or []) if str(x).strip()],
            processed_legacy_event_keys=[str(x) for x in (raw.get("processed_legacy_event_keys") or []) if str(x).strip()],
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid_runtime_state_file") from exc


def save_runtime_state(state: RuntimeState, path: str = "runtime_state/state.json") -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    def _normalize_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    kill = state.kill_switch or KillSwitchState()
    last_run_utc = _normalize_utc(state.last_run_utc)
    last_processed_outcome_ts = _normalize_utc(state.last_processed_outcome_ts)
    stand_aside_until = _normalize_utc(kill.stand_aside_until)
    payload = {
        "last_run_utc": last_run_utc.isoformat() if last_run_utc else None,
        "last_processed_outcome_ts": last_processed_outcome_ts.isoformat() if last_processed_outcome_ts else None,
        "processed_closed_position_ids": sorted(set(state.processed_closed_position_ids or [])),
        "processed_legacy_event_keys": sorted(set(state.processed_legacy_event_keys or [])),
        "kill_switch": {
            "consecutive_losses": kill.consecutive_losses,
            "stand_aside_until": stand_aside_until.isoformat() if stand_aside_until else None,
            "review_required": kill.review_required,
        },
    }
    temp = p.with_suffix(p.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp.replace(p)
    return p
