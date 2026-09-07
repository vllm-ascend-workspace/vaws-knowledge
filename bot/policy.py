"""Load and validate ``bot/policy.yaml``.

Policy is data, not code, but a malformed policy must fail closed with a
message a maintainer can act on rather than silently disabling a gate.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.corpus import require_yaml

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "policy.yaml"

SUPPORTED_POLICY_VERSION = 1

DEFAULTS: dict[str, Any] = {
    "staleness": {
        "reverification_horizon_days": 180,
        "supported_window": {},
        "retired_soc": [],
        "downgrade_from": ["verified"],
    },
    "duplicates": {
        "near_threshold": 0.82,
        "field_match_threshold": 0.9,
        "shingle_size": 3,
    },
    "conflicts": {
        "same_phenomenon_threshold": 0.45,
        "divergent_explanation_max": 0.35,
    },
    "integrity": {"bot_handles": []},
}


class PolicyError(ValueError):
    """The policy file is missing, unreadable or semantically invalid."""


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_POLICY_PATH
    if not p.is_file():
        raise PolicyError(f"policy file not found: {p.name} (expected at bot/policy.yaml)")
    y = require_yaml()
    with p.open("r", encoding="utf-8") as fh:
        raw = y.safe_load(fh)
    if not isinstance(raw, Mapping):
        raise PolicyError("policy file must be a mapping")
    if raw.get("policy_version") != SUPPORTED_POLICY_VERSION:
        raise PolicyError(
            f"unsupported policy_version {raw.get('policy_version')!r}; "
            f"this bot understands {SUPPORTED_POLICY_VERSION}"
        )
    policy = _merge(DEFAULTS, raw)
    _validate(policy)
    return policy


def _merge(defaults: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in set(defaults) | set(override):
        d = defaults.get(key)
        o = override.get(key)
        if isinstance(d, Mapping) and isinstance(o, Mapping):
            out[key] = _merge(d, o)
        elif key in override:
            out[key] = o
        else:
            out[key] = d
    return out


def _validate(policy: Mapping[str, Any]) -> None:
    st = policy["staleness"]
    horizon = st.get("reverification_horizon_days")
    if not isinstance(horizon, int) or horizon <= 0:
        raise PolicyError("staleness.reverification_horizon_days must be a positive integer")
    window = st.get("supported_window")
    if not isinstance(window, Mapping):
        raise PolicyError("staleness.supported_window must be a mapping")
    for dim, bounds in window.items():
        if not isinstance(bounds, Mapping) or set(bounds) != {"min", "max"}:
            raise PolicyError(
                f"staleness.supported_window.{dim} must have exactly `min` and `max`"
            )
    if not isinstance(st.get("retired_soc"), list):
        raise PolicyError("staleness.retired_soc must be a list")
    if not isinstance(st.get("downgrade_from"), list) or not st["downgrade_from"]:
        raise PolicyError("staleness.downgrade_from must be a non-empty list")
    for section, keys in (
        ("duplicates", ("near_threshold", "field_match_threshold")),
        ("conflicts", ("same_phenomenon_threshold", "divergent_explanation_max")),
    ):
        for key in keys:
            value = policy[section].get(key)
            if not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise PolicyError(f"{section}.{key} must be a number in [0, 1]")
    shingle = policy["duplicates"].get("shingle_size")
    if not isinstance(shingle, int) or shingle < 1:
        raise PolicyError("duplicates.shingle_size must be a positive integer")
    if not isinstance(policy["integrity"].get("bot_handles"), list):
        raise PolicyError("integrity.bot_handles must be a list")
