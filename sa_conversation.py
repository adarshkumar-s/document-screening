"""Multi-turn conversation memory for the SA assistant.

SA is used in conversation: an administrator inspects a record, then says
"check its documents", "what about the previous property", or "fix the issue
you found". Without memory each turn is interpreted in isolation, so pronouns
resolve to nothing and follow-ups silently answer the wrong question.

This module keeps a small, bounded, persisted state per SA session:

* the last few turns and the intent that was parsed for each,
* the entities SA is currently talking about (the "focus") plus a short
  history per entity type, so "the previous property" can be answered,
* the problems SA reported, so "fix the issue you found" has an antecedent,
* any question SA asked and is waiting on.

Two rules are enforced by design, not by convention:

1. Nothing here can execute anything. Memory only ever resolves a reference to
   a value; the caller decides whether that value is safe to act on.
2. A resolution carries its own doubt. Every answer says where the value came
   from, whether it was ambiguous and whether it has gone stale, so a mutation
   is never prepared from a guess.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Sequence

MAX_TURNS = 12
MAX_HISTORY_PER_KIND = 5
MAX_ISSUES = 8

# A focus entry older than this many turns (or this many seconds) is reported
# as stale. Stale references are good enough to answer a question and never
# good enough to prepare a mutation.
STALE_TURNS = 6
STALE_SECONDS = 30 * 60

# Entity kinds SA can remember.
KIND_DOCUMENT = "document_id"
KIND_PROPERTY = "property_id"
KIND_LAND = "land_id"
KIND_SURVEY = "survey"
KIND_VILLAGE = "village"
KIND_OFFICER = "officer_id"
KIND_TASK = "task_id"
KIND_PROPOSAL = "proposal_id"
KIND_MUTATION = "mutation_no"
KIND_CASE = "case_number"
KIND_ISSUE = "issue"


@dataclass
class FocusRef:
    """One remembered entity."""

    kind: str
    value: str
    label: str = ""
    turn: int = 0
    at: float = 0.0
    verified: bool = False
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Resolution:
    """The outcome of resolving "that record" against the conversation."""

    kind: Optional[str] = None
    value: Optional[str] = None
    label: str = ""
    confidence: float = 0.0
    source: str = "none"           # explicit | focus | history | issue | none
    ambiguous: bool = False
    stale: bool = False
    candidates: List[str] = field(default_factory=list)
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.value) and not self.ambiguous

    @property
    def safe_for_mutation(self) -> bool:
        """A mutation may be prepared only from an unambiguous, fresh value."""
        return self.resolved and not self.stale and self.source in {"explicit", "focus", "issue"}

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "value": self.value, "label": self.label,
            "confidence": round(self.confidence, 3), "source": self.source,
            "ambiguous": self.ambiguous, "stale": self.stale,
            "candidates": list(self.candidates), "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Reference language
# ---------------------------------------------------------------------------

_REFERENCE_MARKERS = (
    "that ", "this ", "it ", "its ", "the same", "same one", "same record",
    "same property", "that one", "this one", "the one", "previous ", "last ",
    "earlier ", "them", "those", "these", "the issue", "the problem",
    "you found", "above", "earlier you",
)

# Words that make a reference "the one before the current one".
_PREVIOUS_MARKERS = ("previous", "last ", "earlier", "before", "prior", "the other")

# Type hints: what kind of thing is being referred to.
_KIND_HINTS: Sequence[tuple] = (
    (KIND_DOCUMENT, ("record", "records", "document", "documents", "ocr", "field", "fields", "upload")),
    (KIND_PROPERTY, ("property", "properties", "parcel", "parcels")),
    (KIND_LAND, ("land", "plot", "plots")),
    (KIND_SURVEY, ("survey", "khasra", "gat", "khata")),
    (KIND_VILLAGE, ("village", "mauza")),
    (KIND_OFFICER, ("officer", "officers", "verifier")),
    (KIND_TASK, ("task", "tasks", "assignment")),
    (KIND_PROPOSAL, ("proposal", "proposals")),
    (KIND_MUTATION, ("mutation", "mutations")),
    (KIND_CASE, ("case", "cases", "litigation", "court")),
    (KIND_ISSUE, ("issue", "issues", "problem", "problems", "found", "error", "defect")),
)

# Which kinds can satisfy a reference when the wording is generic.
_KIND_FALLBACK_ORDER = (
    KIND_DOCUMENT, KIND_PROPERTY, KIND_LAND, KIND_SURVEY, KIND_VILLAGE,
    KIND_MUTATION, KIND_CASE, KIND_TASK, KIND_OFFICER, KIND_PROPOSAL, KIND_ISSUE,
)


def _kinds_mentioned(text: str) -> List[str]:
    lowered = (text or "").lower()
    found: List[str] = []
    for kind, hints in _KIND_HINTS:
        if any(hint in lowered for hint in hints):
            found.append(kind)
    return found


def is_referential(text: str) -> bool:
    """True when the text refers to something already mentioned."""
    lowered = " " + (text or "").lower().strip()
    return any(marker in lowered for marker in _REFERENCE_MARKERS)


def wants_previous(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _PREVIOUS_MARKERS)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

class ConversationMemory:
    """Bounded, serialisable memory for one SA session."""

    def __init__(self, session_id: str, state: Optional[Dict[str, Any]] = None):
        self.session_id = session_id
        self.turns: List[Dict[str, Any]] = []
        self.focus: Dict[str, FocusRef] = {}
        self.history: Dict[str, List[FocusRef]] = {}
        self.issues: List[Dict[str, Any]] = []
        self.pending_clarification: Optional[Dict[str, Any]] = None
        self.last_intent: Optional[str] = None
        self.turn_index: int = 0
        if state:
            self._load(state)

    # -- serialization -----------------------------------------------------

    def _load(self, state: Dict[str, Any]) -> None:
        self.turns = list(state.get("turns") or [])[:MAX_TURNS]
        self.turn_index = int(state.get("turn_index") or len(self.turns))
        self.last_intent = state.get("last_intent")
        self.pending_clarification = state.get("pending_clarification")
        self.issues = list(state.get("issues") or [])[:MAX_ISSUES]
        for kind, payload in (state.get("focus") or {}).items():
            try:
                self.focus[kind] = FocusRef(**payload)
            except Exception:
                continue
        for kind, entries in (state.get("history") or {}).items():
            loaded: List[FocusRef] = []
            for payload in entries[:MAX_HISTORY_PER_KIND]:
                try:
                    loaded.append(FocusRef(**payload))
                except Exception:
                    continue
            if loaded:
                self.history[kind] = loaded

    def as_dict(self) -> Dict[str, Any]:
        return {
            "turns": self.turns[-MAX_TURNS:],
            "turn_index": self.turn_index,
            "last_intent": self.last_intent,
            "pending_clarification": self.pending_clarification,
            "issues": self.issues[-MAX_ISSUES:],
            "focus": {kind: ref.as_dict() for kind, ref in self.focus.items()},
            "history": {
                kind: [ref.as_dict() for ref in entries[:MAX_HISTORY_PER_KIND]]
                for kind, entries in self.history.items()
            },
        }

    # -- focus -------------------------------------------------------------

    def set_focus(self, kind: str, value: Any, label: str = "", *,
                  verified: bool = False, note: str = "") -> Optional[FocusRef]:
        """Remember the entity SA is now talking about."""
        if value is None:
            return None
        value = str(value).strip()
        if not value:
            return None
        previous = self.focus.get(kind)
        if previous and previous.value == value:
            previous.turn = self.turn_index
            previous.at = time.time()
            if verified:
                previous.verified = True
            return previous
        ref = FocusRef(kind=kind, value=value, label=label or value,
                       turn=self.turn_index, at=time.time(), verified=verified, note=note)
        self.focus[kind] = ref
        entries = self.history.setdefault(kind, [])
        entries.insert(0, ref)
        del entries[MAX_HISTORY_PER_KIND:]
        return ref

    def get_focus(self, kind: str) -> Optional[FocusRef]:
        return self.focus.get(kind)

    def current(self) -> Dict[str, str]:
        return {kind: ref.value for kind, ref in self.focus.items()}

    def forget(self, kind: str) -> None:
        self.focus.pop(kind, None)

    # -- issues ------------------------------------------------------------

    def add_issue(self, issue_id: str, title: str, detail: str = "", *,
                  repairable: bool = False, safe: bool = False) -> None:
        self.issues = [i for i in self.issues if i.get("id") != issue_id]
        self.issues.append({
            "id": issue_id, "title": title, "detail": detail,
            "repairable": bool(repairable), "safe": bool(safe),
            "turn": self.turn_index, "at": time.time(),
        })
        del self.issues[:-MAX_ISSUES]
        self.set_focus(KIND_ISSUE, issue_id, title)

    def open_issues(self) -> List[Dict[str, Any]]:
        return list(self.issues)

    # -- turns -------------------------------------------------------------

    def add_turn(self, role: str, text: str, *, intent: Optional[str] = None,
                 entities: Optional[List[Dict[str, Any]]] = None,
                 tools: Optional[List[str]] = None,
                 resolution: Optional[Dict[str, Any]] = None) -> int:
        self.turn_index += 1
        self.turns.append({
            "turn": self.turn_index,
            "role": role,
            "text": (text or "")[:2000],
            "intent": intent,
            "entities": entities or [],
            "tools": tools or [],
            "resolution": resolution or {},
            "at": time.time(),
        })
        del self.turns[:-MAX_TURNS]
        if intent and role == "user":
            self.last_intent = intent
        return self.turn_index

    def recent_turns(self, count: int = 4) -> List[Dict[str, Any]]:
        return self.turns[-count:]

    # -- clarification -----------------------------------------------------

    def ask(self, question: str, options: Optional[Sequence[str]] = None, *,
            kind: str = "") -> None:
        self.pending_clarification = {
            "question": question,
            "options": list(options or []),
            "kind": kind,
            "turn": self.turn_index,
        }

    def clear_question(self) -> None:
        self.pending_clarification = None

    # -- corrections -------------------------------------------------------

    def apply_correction(self, entities: Sequence[Any], *, note: str = "administrator correction") -> List[str]:
        """Drop the entities the administrator just corrected and adopt the new ones.

        Returns the kinds that were discarded so the turn can be reported.
        """
        discarded: List[str] = []
        corrections: Dict[str, str] = {}
        for entity in entities or []:
            kind = getattr(entity, "type", None) if not isinstance(entity, dict) else entity.get("type")
            value = getattr(entity, "value", None) if not isinstance(entity, dict) else entity.get("value")
            if not kind or not value:
                continue
            corrections[kind] = str(value)

        # A correction of one kind invalidates that kind only; unrelated focus
        # (for example the village being discussed) survives.
        for kind, value in corrections.items():
            previous = self.focus.get(kind)
            self.set_focus(kind, value, note=note)
            # The corrected value becomes the only history for that kind, so
            # "the previous property" can never resurrect a value the
            # administrator has just said was wrong.
            self.history[kind] = [self.focus[kind]]
            if previous and previous.value != value:
                discarded.append(kind)
        return discarded

    # -- resolution --------------------------------------------------------

    def _is_stale(self, ref: FocusRef) -> bool:
        return (self.turn_index - ref.turn) > STALE_TURNS or (time.time() - ref.at) > STALE_SECONDS

    def _candidates_for(self, kinds: Sequence[str]) -> List[FocusRef]:
        refs: List[FocusRef] = []
        seen: set = set()
        for kind in kinds:
            for ref in ([self.focus[kind]] if kind in self.focus else []) + list(self.history.get(kind) or []):
                key = (ref.kind, ref.value)
                if key in seen:
                    continue
                seen.add(key)
                refs.append(ref)
        return refs

    def resolve(self, text: str, entities: Optional[Sequence[Any]] = None,
                *, prefer_kind: Optional[str] = None) -> Resolution:
        """Resolve what a command is talking about.

        Explicit identifiers always win. Otherwise the referential wording
        ("that record", "the previous property", "the issue you found") selects
        between the current focus, the history for that kind, and the problems
        SA has reported. When the wording is not specific enough to pick one
        value, the result is marked ambiguous with the candidates attached, and
        the caller must ask instead of acting.
        """
        # 1. An explicit identifier needs no memory.
        for entity in entities or []:
            kind = getattr(entity, "type", None) if not isinstance(entity, dict) else entity.get("type")
            value = getattr(entity, "value", None) if not isinstance(entity, dict) else entity.get("value")
            if kind and value and (prefer_kind is None or kind == prefer_kind or prefer_kind == KIND_ISSUE):
                return Resolution(kind=kind, value=str(value), label=str(value),
                                  confidence=0.95, source="explicit")

        mentioned = _kinds_mentioned(text)
        kinds = [prefer_kind] if prefer_kind else mentioned
        if not kinds:
            kinds = list(_KIND_FALLBACK_ORDER)
        else:
            # "that property's documents" should still consider the property
            # itself, not only documents.
            kinds = list(dict.fromkeys(list(kinds) + list(_KIND_FALLBACK_ORDER)))

        candidates = self._candidates_for(kinds)
        if not candidates:
            return Resolution(source="none", reason="nothing in this conversation matches that reference")

        if not is_referential(text) and not prefer_kind:
            # No "that/it/the previous" wording and no identifier: SA should
            # ask rather than assume the current focus is the target.
            return Resolution(
                kind=candidates[0].kind, value=None, source="none", ambiguous=True,
                candidates=[ref.value for ref in candidates[:5]],
                reason="the command does not name a target and is not phrased as a follow-up",
            )

        previous = wants_previous(text)
        ordered = sorted(candidates, key=lambda ref: ref.turn, reverse=True)
        if previous:
            # "the previous property" = the most recent value that is not the
            # one currently in focus.
            current = ordered[0]
            older = [ref for ref in ordered[1:] if ref.value != current.value]
            if not older:
                return Resolution(
                    kind=current.kind, value=None, source="history", ambiguous=True,
                    candidates=[current.value],
                    reason="there is no earlier value of that kind in this conversation",
                )
            chosen = older[0]
            source = "history"
        else:
            chosen = ordered[0]
            source = "focus"

        # Issue references ("fix the issue you found") resolve to a reported
        # problem even when the wording also mentions records.
        if KIND_ISSUE in mentioned and not str(chosen.kind) == KIND_ISSUE:
            issue_ref = self.focus.get(KIND_ISSUE)
            if issue_ref:
                chosen = issue_ref
                source = "issue"

        stale = self._is_stale(chosen)
        distinct = [ref.value for ref in ordered if ref.kind == chosen.kind]
        ambiguous = False
        if not previous and len(distinct) > 1 and not str(chosen.kind) == KIND_ISSUE:
            # "that record" with several recent records of the same kind and no
            # other discriminator is genuinely ambiguous.
            generic = not any(word in (text or "").lower() for word in ("current", "last one", "the one i", "you just", "above"))
            if generic:
                ambiguous = True

        confidence = 0.9 if not stale else 0.6
        if ambiguous:
            confidence = 0.4
        if source == "history":
            confidence = min(confidence, 0.8)

        return Resolution(
            kind=chosen.kind,
            value=chosen.value,
            label=chosen.label or chosen.value,
            confidence=confidence,
            source=source,
            ambiguous=ambiguous,
            stale=stale,
            candidates=[ref.value for ref in ordered[:5]],
            reason="" if not ambiguous else "several recent values of that kind match",
        )

    def describe(self) -> str:
        """Short human-readable summary of what SA is currently holding."""
        parts: List[str] = []
        for kind, ref in self.focus.items():
            stale = " (stale)" if self._is_stale(ref) else ""
            parts.append(f"{kind}={ref.label}{stale}")
        if self.issues:
            parts.append(f"open_issues={len(self.issues)}")
        if self.pending_clarification:
            parts.append("awaiting_answer=yes")
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_MEMORY_TABLE_SQL = """CREATE TABLE IF NOT EXISTS sa_conversation_state(
    session_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    updated_at REAL NOT NULL
)"""


def _db():
    import server
    return server.get_db()


def ensure_memory_table() -> None:
    with _db() as db:
        db.execute(_MEMORY_TABLE_SQL)


def load_memory(session_id: str) -> ConversationMemory:
    """Load a session's memory. A failure must never break the SA turn."""
    memory = ConversationMemory(session_id)
    try:
        ensure_memory_table()
        with _db() as db:
            row = db.execute(
                "SELECT state FROM sa_conversation_state WHERE session_id=?", (session_id,)
            ).fetchone()
        if row:
            memory = ConversationMemory(session_id, json.loads(row["state"] or "{}"))
    except Exception:
        memory = ConversationMemory(session_id)
    return memory


def save_memory(memory: ConversationMemory) -> None:
    try:
        ensure_memory_table()
        with _db() as db:
            db.execute(
                """INSERT INTO sa_conversation_state(session_id,state,updated_at) VALUES(?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at""",
                (memory.session_id, json.dumps(memory.as_dict(), default=str), time.time()),
            )
    except Exception:
        pass


def clear_memory(session_id: str) -> None:
    try:
        ensure_memory_table()
        with _db() as db:
            db.execute("DELETE FROM sa_conversation_state WHERE session_id=?", (session_id,))
    except Exception:
        pass
