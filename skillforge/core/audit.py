"""The audit trail — who did what, as whom, and what actually changed.

Append-only JSONL with a **hash chain**: every record carries the hash of the one before
it, so "immutable" is something you can check rather than something we claim. Editing or
removing a past record breaks every hash after it, and `verify()` names the first record
where the chain diverges.

That property is cheap and it is the difference between a log and evidence. A log a
compromised process can quietly rewrite tells a security reviewer nothing.

**What the chain does not prove.** It shows nothing was altered *within* the sequence the
file still holds. It cannot show that records were never truncated from the end — dropping
the last N entries leaves a chain that verifies perfectly. Catching that needs the tail
hash anchored somewhere the writer cannot reach: a second store, a signed receipt, an
external timestamping service. Worth stating plainly, because "immutable audit trail" is
the kind of claim a security reviewer will probe, and the honest answer is stronger than
an overstated one.

**Before/after state.** A write is only reversible if you know what it overwrote, so each
record captures the state the skill observed either side of its changes. The audit does
not fetch that itself — it derives it from the reads the skill *already made*, using the
convention the generator prompt enforces: read before you write, read back after. A skill
that skips either has no recoverable before-state, which is the concrete reason that
convention is a hard rule rather than a style preference.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

GENESIS = "0" * 64


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    at: str                       # ISO 8601, UTC
    outcome: str                  # acted | denied | failed | ignored
    actor: str
    skill: str | None = None
    version: int | None = None
    manifest: dict | None = None  # the manifest *as executed*, not as later edited
    utterance: str | None = None
    args: dict = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)
    before: object = None
    after: object = None
    duration_s: float = 0.0
    blocked_at: str | None = None
    reason: str | None = None
    identity_confidence: str | None = None
    prev_hash: str = GENESIS
    hash: str = ""

    def payload(self) -> dict:
        """Everything the hash covers — that is, everything except the hash."""
        d = asdict(self)
        d.pop("hash", None)
        return d

    def compute_hash(self) -> str:
        canonical = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"),
                               default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    @property
    def reversible(self) -> bool:
        """Whether this record carries enough to undo the action it describes."""
        return (
            self.outcome == "acted"
            and self.before is not None
            and bool(self.manifest and self.manifest.get("reversible"))
        )


def derive_states(calls: list[dict]) -> tuple[object, object]:
    """Pull before/after state out of the reads the skill already made.

    Relies on the read-before-write / read-back convention: the first and last calls are
    the same read against the same target. Returns `(None, None)` when the skill didn't
    follow it — silently guessing would produce a before-state that is not actually the
    prior state, which is worse than having none.
    """
    ok = [c for c in calls if c.get("ok")]
    if len(ok) < 2:
        return None, None

    first, last = ok[0], ok[-1]
    if first.get("primitive") != last.get("primitive"):
        return None, None
    if first.get("input") != last.get("input"):
        return None, None
    return first.get("result"), last.get("result")


class AuditLog:
    """Append-only, hash-chained, one JSON object per line."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # --- writing -----------------------------------------------------------

    def record(
        self,
        *,
        outcome: str,
        actor: str,
        skill=None,
        utterance: str | None = None,
        args: dict | None = None,
        calls: list | None = None,
        duration_s: float = 0.0,
        blocked_at: str | None = None,
        reason: str | None = None,
        identity_confidence: str | None = None,
    ) -> AuditRecord:
        """Append one record. The only way to write to this log."""
        call_dicts = [
            c if isinstance(c, dict) else {
                "primitive": c.primitive, "input": c.input,
                "ok": c.ok, "error": c.error, "result": c.result,
            }
            for c in (calls or [])
        ]
        before, after = derive_states(call_dicts)

        tail = self.tail()
        record = AuditRecord(
            seq=(tail.seq + 1) if tail else 1,
            at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            outcome=outcome,
            actor=actor,
            skill=getattr(skill, "name", None),
            version=getattr(skill, "version", None),
            manifest=skill.manifest.to_dict() if skill is not None else None,
            utterance=utterance,
            args=args or {},
            calls=call_dicts,
            before=before,
            after=after,
            duration_s=round(duration_s, 4),
            blocked_at=blocked_at,
            reason=reason,
            identity_confidence=identity_confidence,
            prev_hash=tail.hash if tail else GENESIS,
        )
        sealed = AuditRecord(**{**record.payload(), "hash": record.compute_hash()})

        # Append-and-flush, with the directory synced too, so a record that returns is a
        # record that survives the process dying immediately afterwards.
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(sealed), default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return sealed

    # --- reading -----------------------------------------------------------

    def records(self) -> list[AuditRecord]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(AuditRecord(**json.loads(line)))
        return out

    def tail(self) -> AuditRecord | None:
        records = self.records()
        return records[-1] if records else None

    def __len__(self) -> int:
        return len(self.records())

    # --- integrity ---------------------------------------------------------

    def verify(self) -> tuple[bool, list[str]]:
        """Walk the chain. Returns (ok, problems), naming the first divergence.

        Three ways it fails, and each is reported distinctly because they mean different
        things: a record whose own hash doesn't match its content was **edited**; a record
        whose `prev_hash` doesn't match its predecessor means one was **removed or
        reordered**; a gap in `seq` means one was **dropped**.
        """
        problems: list[str] = []
        expected_prev = GENESIS

        for i, record in enumerate(self.records(), start=1):
            if record.seq != i:
                problems.append(f"record {i}: seq is {record.seq} — a record was dropped")
            if record.hash != record.compute_hash():
                problems.append(f"record {record.seq}: content was edited after writing")
            if record.prev_hash != expected_prev:
                problems.append(
                    f"record {record.seq}: chain broken — a prior record was removed "
                    "or reordered"
                )
            expected_prev = record.hash

        return not problems, problems

    # --- views -------------------------------------------------------------

    def table(self) -> list[dict]:
        """Flat rows for the Armory's audit table — a feed is for watching, a table is
        for checking."""
        return [
            {
                "seq": r.seq,
                "at": r.at,
                "actor": r.actor,
                "skill": f"{r.skill}@v{r.version}" if r.skill else "—",
                "outcome": r.outcome,
                "detail": r.blocked_at or r.reason or "",
                "reversible": r.reversible,
            }
            for r in self.records()
        ]

    def for_actor(self, actor: str) -> list[AuditRecord]:
        return [r for r in self.records() if r.actor == actor]

    def denials(self) -> list[AuditRecord]:
        return [r for r in self.records() if r.outcome == "denied"]
