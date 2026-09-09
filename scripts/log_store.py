"""Appends decisions to docs/data/log.json -- the same file the GitHub
Pages dashboard (docs/index.html) reads from. Keeping it inside docs/
means Pages serves it with no separate build step.
"""
import json
import os
from datetime import datetime, timezone

LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "log.json")


def append_entry(kind, headline, reasoning, meta=None):
    entries = []
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH) as f:
            entries = json.load(f)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": kind,  # "waiver" | "lineup" | "draft"
        "headline": headline,
        "reasoning": reasoning,
        "meta": meta or {},
    }
    entries.insert(0, entry)

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w") as f:
        json.dump(entries, f, indent=2)

    return entry
