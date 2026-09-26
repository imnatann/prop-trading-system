"""Append-only audit log. Evidence that the protocol was followed.

Every phase transition, fit, freeze and evaluation is recorded with a timestamp.
The log is append-only: entries can be added but never rewritten, so it can be used
to reconstruct what was known at each point.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG = ROOT / "data" / "audit_log.jsonl"


class AuditLog:
    """JSONL append-only log."""

    def __init__(self, path: Optional[Path] = None, run_id: Optional[str] = None):
        self.path = Path(path) if path is not None else DEFAULT_LOG
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def write(self, event: str, **fields: Any) -> Dict[str, Any]:
        rec = {"ts": datetime.now(timezone.utc).isoformat(),
               "run_id": self.run_id, "event": event}
        rec.update(fields)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as fh:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
        return rec

    def read(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def freeze_hash(self, obj: Any) -> str:
        """Deterministic hash of a frozen object, for the audit trail."""
        blob = json.dumps(obj, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:32]
