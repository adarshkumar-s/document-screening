"""Structured command understanding for the SA assistant.

SA used to decide what an administrator meant with chains of
``any(term in text)`` checks. That cannot express the difference between
"risk analysis of this village" and "risk analysis of one document", gives no
way to tell a confident parse from a guess, and silently degrades whenever two
keywords collide.

This module replaces that with slot filling:

1. ``normalize`` the raw text,
2. ``extract_entities`` pull typed entities out of it (document ids, survey
   numbers, villages, case numbers, thresholds, ...),
3. every intent in ``INTENT_CATALOG`` declares the verbs, nouns, phrases and
   entity slots that support it,
4. ``parse_command`` scores each intent, returns the winner with a confidence
   and the runner-up, and marks the parse ambiguous when the two are close.

Everything here is deterministic. The language model is never asked what a
command means -- only how to phrase an answer -- so a model outage, a
malformed response or a prompt-injection attempt cannot change which tools
run or whether a mutation is prepared.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

MAX_COMMAND_LENGTH = 4000

# ---------------------------------------------------------------------------
# Normalization and security pre-screening
# ---------------------------------------------------------------------------

# Patterns that indicate someone is trying to talk past SA's instructions
# rather than ask SA to do work. They are recorded and the text is still
# treated as data; SA never executes text, so detection is defensive only.
_INJECTION_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("instruction_override", r"ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+(?:instructions|prompts|rules)"),
    ("instruction_override", r"disregard\s+(?:all\s+)?(?:previous|prior|above|your|the)\s+(?:\w+\s+){0,2}(?:instructions|prompts|rules)"),
    ("system_prompt_probe", r"\b(?:system|developer)\s+(?:prompt|message|instructions)\b"),
    ("role_hijack", r"\byou are now\b|\bact as (?:an? )?(?:unrestricted|different|new)\b|\bpretend (?:to be|you are)\b"),
    ("secret_probe", r"\b(?:reveal|print|show|dump|repeat)\b[^.\n]{0,40}\b(?:api[_ ]?key|secret|token|password|credential)"),
    ("exfiltration", r"\bsend\b[^.\n]{0,40}\b(?:to|at)\b[^.\n]{0,40}\b(?:http|https|@)\b"),
    ("prompt_delimiter", r"<\s*/?\s*(?:system|assistant|user|tool)\s*>"),
    ("sql_attempt", r"\b(?:drop|truncate|alter)\s+table\b|\bdelete\s+from\b|\binsert\s+into\b|\bupdate\s+\w+\s+set\b|\bselect\b[\s\S]{0,80}\bfrom\b"),
    ("privilege_escalation", r"\bbypass\b[^.\n]{0,30}\b(?:approval|gate|permission|auth)\b|\bwithout\s+approval\b|\bskip\s+(?:the\s+)?approval\b"),
)


def normalize(text: Any) -> str:
    """Canonicalise administrator input before anything interprets it.

    NFKC folds compatibility characters (a full-width ``４５２`` becomes
    ``452``), control characters are dropped so they cannot be used to hide
    instructions from a reviewer, and whitespace is collapsed so the slot
    matchers see predictable text.
    """
    raw = "" if text is None else str(text)
    folded = unicodedata.normalize("NFKC", raw)
    without_controls = "".join(
        ch if (ch.isprintable() or ch in "\n\t") else " " for ch in folded
    )
    collapsed = re.sub(r"[ \t\r\f\v]+", " ", without_controls)
    collapsed = re.sub(r"\s*\n\s*", " \n ", collapsed)
    return collapsed.strip()[:MAX_COMMAND_LENGTH]


@dataclass(frozen=True)
class SecuritySignal:
    """A pre-screening hit. SA records it and treats the text as data."""

    kind: str
    pattern: str
    excerpt: str


def detect_injection(text: str) -> List[SecuritySignal]:
    """Flag instruction-override, secret-probe and SQL-shaped input.

    SA has no tool that accepts SQL and no tool that executes free text, so a
    hit here is a signal for the audit trail and for the administrator, not a
    decision point for execution.
    """
    normalized = normalize(text)
    lowered = normalized.lower()
    signals: List[SecuritySignal] = []
    for kind, pattern in _INJECTION_PATTERNS:
        match = re.search(pattern, lowered)
        if match:
            excerpt = normalized[max(0, match.start() - 20):match.end() + 20]
            signals.append(SecuritySignal(kind=kind, pattern=pattern, excerpt=excerpt.strip()))
    return signals


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Entity:
    """A typed value extracted from an administrator command."""

    type: str
    value: str
    raw: str
    confidence: float = 1.0

    def as_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "value": self.value, "raw": self.raw, "confidence": self.confidence}


# Words that terminate a free-text capture such as a village name.
# Words that must never be captured as a person/officer name. They are
# domain vocabulary that follows "owner"/"officer" in ordinary questions.
_CAPTURE_STOPWORDS = {
    "and", "or", "for", "with", "in", "of", "the", "a", "an", "please",
    "show", "find", "list", "check", "compare", "against", "from", "to",
    "that", "this", "which", "where", "when", "what", "how", "me", "my",
    "records", "record", "documents", "document", "properties", "property",
    "workload", "workloads", "task", "tasks", "queue", "pending", "available",
    "free", "capacity", "status", "detail", "details", "history", "risk",
    "verification", "officer", "officers", "is", "are", "was", "were",
}

_TAIL_STOPWORDS = {
    "and", "or", "for", "with", "in", "of", "the", "a", "an", "please",
    "show", "find", "list", "check", "compare", "against", "from", "to",
    "that", "this", "which", "where", "when", "what", "how", "me", "my",
    "records", "record", "documents", "document", "properties", "property",
}

_ENTITY_PATTERNS: Tuple[Tuple[str, str, float], ...] = (
    # Canonical land ids this application mints: LR-<sha1 prefix>.
    ("land_id", r"\b(LR-[0-9a-f]{6,16})\b", 1.0),
    # Property/parcel ids used by the mapping surface.
    ("property_id", r"\b((?:DEMO-)?PROP-[0-9A-Z]{1,}[0-9A-Z-]*)\b", 0.95),
    ("mutation_no", r"\b(MUT-[0-9]{4}-[0-9A-Z]{1,}|MUT-[0-9A-Z-]{4,})\b", 0.95),
    ("case_number", r"\b([A-Z]{2,6}[-/]\d{1,6}[-/]\d{2,4})\b", 0.9),
    ("survey", r"\b(?:survey|gat)\s*(?:no\.?|number|num|#)?\s*[:#=-]?\s*([0-9][0-9A-Za-z/_-]{0,15})", 0.9),
    ("khasra", r"\b(?:khasra|khata|plot)\s*(?:no\.?|number|num|#)?\s*[:#=-]?\s*([0-9][0-9A-Za-z/_-]{0,15})", 0.85),
    ("village", r"\b(?:village|mauza|gram)\s*(?:named|called|of|is|:)?\s*([A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,2})", 0.8),
    ("district", r"\b(?:district)\s*(?:named|called|of|is|:)?\s*([A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,2})", 0.8),
    ("person", r"\b(?:owner|owned by|in the name of|name)\s*(?:is|:)?\s*([A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,3})", 0.7),
    ("officer", r"\b(?:officer|verifier)\s*(?:id|no\.?|number|#)?\s*[:#=-]?\s*([A-Za-z0-9._@-]{3,40})", 0.7),
    ("case_number", r"\b(?:case|civil|writ|appeal)\s*(?:no\.?|number|#)?\s*[:#=-]?\s*([0-9A-Z][0-9A-Z/_-]{2,20})", 0.7),
    ("mutation_no", r"\bmutation\s*(?:no\.?|number|#)?\s*[:#=-]?\s*([0-9][0-9A-Z-]{2,20})", 0.7),
    ("threshold", r"\b(?:below|under|less than|above|over|greater than|at least|>=|<=|>|<)\s*(\d{1,3}(?:\.\d+)?)\s*%?", 0.8),
    ("threshold", r"\b(\d{1,3}(?:\.\d+)?)\s*%\s*(?:confidence|ocr)", 0.75),
    ("limit", r"\b(?:top|first|last|latest|recent)\s+(\d{1,3})\b", 0.8),
    ("year", r"\b((?:19|20)\d{2})\b", 0.6),
)


def _clean_capture(value: str) -> str:
    """Trim filler words off a free-text capture such as a village name."""
    parts = [p for p in str(value or "").split() if p]
    while parts and parts[-1].lower().strip(".,;:!?") in _TAIL_STOPWORDS:
        parts.pop()
    return " ".join(parts).strip(" .,;:!?")


def extract_entities(text: str) -> List[Entity]:
    """Pull every supported entity out of a normalized command.

    Document identifiers are delegated to the Admin Assistant's parser so the
    two assistants keep agreeing on what "record 123" means.
    """
    normalized = normalize(text)
    entities: List[Entity] = []
    seen: set = set()

    for entity_type, pattern, confidence in _ENTITY_PATTERNS:
        for match in re.finditer(pattern, normalized, re.I):
            raw = match.group(0).strip()
            value = _clean_capture(match.group(1))
            if not value:
                continue
            if entity_type in {"village", "district", "person", "officer"}:
                value = _clean_capture(value)
                if not value or value.lower().strip(".,;:!?") in _CAPTURE_STOPWORDS:
                    continue
            key = (entity_type, value.lower())
            if key in seen:
                continue
            seen.add(key)
            entities.append(Entity(type=entity_type, value=value, raw=raw, confidence=confidence))

    document_id = _document_reference(normalized)
    if document_id:
        entities.append(Entity(type="document_id", value=document_id, raw=document_id, confidence=0.95))

    return entities


def _document_reference(text: str) -> Optional[str]:
    """Reuse the Admin Assistant identifier parser (single source of truth)."""
    try:
        import admin_assistant
        return admin_assistant._record_id_from_prompt(text) or None
    except Exception:
        return None


def entity_value(entities: Sequence[Entity], entity_type: str) -> Optional[str]:
    for entity in entities:
        if entity.type == entity_type:
            return entity.value
    return None


def has_entity(entities: Sequence[Entity], *types: str) -> bool:
    wanted = set(types)
    return any(entity.type in wanted for entity in entities)


# ---------------------------------------------------------------------------
# Intent catalogue
# ---------------------------------------------------------------------------

_INSPECT_VERBS = (
    "show", "get", "display", "open", "view", "inspect", "detail", "details",
    "what is", "what's", "what are", "what about", "tell me about", "describe",
    "fetch", "read", "look up", "lookup", "find", "search", "locate", "list",
    "which", "how many", "count", "give me", "summarise", "summarize",
    "explain", "is there", "are there", "does", "do ", "has", "have", "any",
    "who", "when", "what", "how",
)
_ANALYZE_VERBS = (
    "analyse", "analyze", "analysis", "assess", "evaluate", "review", "audit",
    "check", "verify", "validate", "detect", "scan", "examine", "investigate",
    "cross-check", "crosscheck", "compare", "diff", "reconcile", "score",
    "diagnose", "troubleshoot", "why", "find", "search", "look for", "scan for",
)
_WRITE_VERBS = (
    "assign", "escalate", "reprocess", "prepare", "propose", "create", "set",
    "clear", "update", "flag", "raise", "add", "apply", "re-run", "rerun",
)

_DOCUMENT_NOUNS = (
    "document", "documents", "record", "records", "file", "files", "upload",
    "uploaded", "ocr", "scan", "scanned", "page", "pages", "extraction",
)
_LAND_NOUNS = (
    "land", "property", "properties", "parcel", "parcels", "plot", "plots",
    "survey", "khasra", "khata", "gat", "ownership", "owner", "title",
    "village", "mauza", "district", "tehsil", "taluka", "registry",
    "registration", "registered", "sub-registrar", "sro", "land record",
)
_RISK_NOUNS = (
    "risk", "risky", "verdict", "risky parcel", "due diligence", "confidence",
    "reliability", "trustworth",
)
_ANOMALY_NOUNS = (
    "anomaly", "anomalies", "mismatch", "mismatches", "discrepancy",
    "discrepancies", "inconsistency", "inconsistencies", "suspicious",
    "fraud", "fraudulent", "red flag", "red flags", "duplicate", "duplicates",
    "gap", "gaps", "missing field", "missing fields", "outlier", "outliers",
)
_LITIGATION_NOUNS = (
    "litigation", "court", "case", "cases", "lawsuit", "legal", "dispute",
    "hearing", "judgement", "judgment", "petition", "stay order",
)
_MAPPING_NOUNS = (
    "map", "mapping", "location", "coordinate", "coordinates", "pin", "gis",
    "latitude", "longitude", "geometry", "boundary", "where is",
)
_WORKFLOW_NOUNS = (
    "verification", "verified", "queue", "pending", "officer", "officers",
    "workload", "task", "tasks", "assignment", "assignments", "sla", "stale",
    "backlog", "capacity", "free officer", "unassigned",
)
_GOVERNANCE_NOUNS = (
    "proposal", "proposals", "approval", "approvals", "approval center",
    "governance", "evidence", "executed", "rejected", "audit trail",
)
_HEALTH_NOUNS = (
    "health", "healthy", "uptime", "status", "statistics", "stats",
    "performance", "slow", "latency", "error rate", "errors", "failing",
    "broken", "working", "snapshot",
)
_REPORT_NOUNS = (
    "report", "briefing", "summary", "export", "pdf", "download", "digest",
)
_HISTORY_NOUNS = (
    "history", "timeline", "chain", "previous owner", "earlier owner",
    "transfer", "transfers", "genealogy", "chronology", "past owner",
)
_MUTATION_NOUNS = ("mutation", "mutations", "mutated")
_ENCUMBRANCE_NOUNS = (
    "encumbrance", "encumbrances", "mortgage", "lien", "charge", "loan",
    "hypothecation", "bank charge",
)


@dataclass(frozen=True)
class IntentSpec:
    """Declares the slots that support one intent."""

    name: str
    domain: str
    description: str
    phrases: Tuple[str, ...] = ()
    verbs: Tuple[str, ...] = ()
    nouns: Tuple[str, ...] = ()
    entities: Tuple[str, ...] = ()
    requires_approval: bool = False


INTENT_CATALOG: Tuple[IntentSpec, ...] = (
    IntentSpec(
        name="GREETING", domain="conversation",
        description="Small talk and greetings.",
        phrases=("hi", "hello", "hey", "good morning", "good afternoon", "good evening", "thanks", "thank you"),
        verbs=(), nouns=(), entities=(),
    ),
    IntentSpec(
        name="CAPABILITY", domain="conversation",
        description="What SA can do.",
        phrases=("what can you do", "what can you help", "what do you do", "capabilities",
                 "features can you access", "help me", "who are you", "what are you"),
        verbs=(), nouns=(), entities=(),
    ),
    IntentSpec(
        name="CORRECTION", domain="conversation",
        description="The administrator is correcting the previous turn.",
        phrases=("no,", "no i meant", "not that", "not this", "i meant", "i mean", "wrong",
                 "instead of", "sorry, i meant", "not what i", "i was referring", "actually i meant"),
        verbs=(), nouns=(), entities=(),
    ),
    IntentSpec(
        name="DOCUMENT_INSPECT", domain="documents",
        description="Inspect one identified document/record.",
        verbs=_INSPECT_VERBS, nouns=_DOCUMENT_NOUNS, entities=("document_id",),
        phrases=("open record", "record detail", "document detail", "show me record"),
    ),
    IntentSpec(
        name="DOCUMENT_SEARCH", domain="documents",
        description="Search documents by owner, village, survey or free text.",
        verbs=_INSPECT_VERBS, nouns=_DOCUMENT_NOUNS,
        entities=("person", "village", "survey", "khasra", "document_id"),
        phrases=("search documents", "find documents", "which documents", "list documents"),
    ),
    IntentSpec(
        name="DOCUMENT_OPERATIONS", domain="documents",
        description="List the operations available for one record.",
        phrases=("what operation", "which operation", "what can be done", "available operation",
                 "available operations", "what actions", "which actions", "operations can be done",
                 "operations are available", "what are the operations", "list operations",
                 "operation can be done", "can i do with", "what can i do"),
        nouns=_DOCUMENT_NOUNS, entities=("document_id",),
    ),
    IntentSpec(
        name="OCR_ANALYSIS", domain="documents",
        description="Analyse OCR/extraction quality and low-confidence fields.",
        verbs=_ANALYZE_VERBS,
        nouns=("ocr", "extraction", "extracted field", "extracted fields", "confidence", "text quality", "mean confidence"),
        entities=("threshold", "document_id"),
        phrases=("low confidence", "poor ocr", "bad ocr", "ocr quality"),
    ),
    IntentSpec(
        name="RISK_ANALYSIS", domain="land",
        description="Deterministic land risk analysis for a parcel or village.",
        verbs=_ANALYZE_VERBS, nouns=_RISK_NOUNS,
        entities=("land_id", "survey", "khasra", "village", "property_id"),
        phrases=("risk analysis", "risk review", "risk score", "due diligence"),
    ),
    IntentSpec(
        name="ANOMALY_SCAN", domain="documents",
        description="Find anomalies, mismatches and inconsistencies.",
        verbs=_ANALYZE_VERBS, nouns=_ANOMALY_NOUNS,
        entities=("document_id", "village", "survey"),
        phrases=("look for anomalies", "find anomalies", "scan for anomalies", "any anomalies"),
    ),
    IntentSpec(
        name="LAND_LOOKUP", domain="land",
        description="Look up a land record by id, survey or village.",
        verbs=_INSPECT_VERBS, nouns=_LAND_NOUNS,
        entities=("land_id", "property_id", "survey", "khasra", "village", "district"),
        phrases=("land record", "property detail", "parcel detail"),
    ),
    IntentSpec(
        name="LAND_HISTORY", domain="land",
        description="Ownership history / chain of title for a property.",
        verbs=_INSPECT_VERBS, nouns=_LAND_NOUNS + _HISTORY_NOUNS,
        entities=("property_id", "land_id", "survey", "village"),
        phrases=("ownership history", "property history", "chain of title", "previous owners"),
    ),
    IntentSpec(
        name="LAND_TIMELINE", domain="land",
        description="Chronological timeline of a parcel.",
        verbs=_INSPECT_VERBS, nouns=_LAND_NOUNS + ("timeline", "chronology"),
        entities=("land_id", "survey", "village"),
        phrases=("build a timeline", "show the timeline", "chronology", "timeline"),
    ),
    IntentSpec(
        name="MUTATION_LIST", domain="land",
        description="List mutation applications.",
        verbs=_INSPECT_VERBS, nouns=_MUTATION_NOUNS,
        entities=("mutation_no", "land_id", "survey", "village"),
        phrases=("list mutations", "mutation status", "pending mutations"),
    ),
    IntentSpec(
        name="ENCUMBRANCE_LIST", domain="land",
        description="List encumbrances, mortgages and liens.",
        verbs=_INSPECT_VERBS, nouns=_ENCUMBRANCE_NOUNS,
        entities=("land_id", "survey", "village"),
        phrases=("list encumbrances", "active encumbrances", "any encumbrance"),
    ),
    IntentSpec(
        name="LITIGATION_SEARCH", domain="litigation",
        description="Search the litigation/court register.",
        verbs=_INSPECT_VERBS, nouns=_LITIGATION_NOUNS,
        entities=("case_number", "land_id", "survey", "village"),
        phrases=("court cases", "any litigation", "legal dispute", "case status"),
    ),
    IntentSpec(
        name="REGISTRATION_STATUS", domain="land",
        description="Registration / registry-entry status for a parcel.",
        verbs=_INSPECT_VERBS,
        nouns=("registration", "register", "registry entry", "register entry", "sub-registrar", "sro"),
        entities=("land_id", "survey", "village"),
        phrases=("registration status", "is it registered", "registry status"),
    ),
    IntentSpec(
        name="MAPPING_LOCATION", domain="mapping",
        description="Location, coordinates and map status for a property.",
        verbs=_INSPECT_VERBS, nouns=_MAPPING_NOUNS,
        entities=("property_id", "land_id", "survey", "village"),
        phrases=("where is", "show on map", "map location", "exact pin", "coordinates of"),
    ),
    IntentSpec(
        name="VERIFICATION_QUEUE", domain="workflow",
        description="Pending verification queue and faulty records.",
        verbs=_INSPECT_VERBS, nouns=_WORKFLOW_NOUNS,
        entities=("limit",),
        phrases=("pending verification", "verification queue", "awaiting verification", "needs attention",
                 "pending records"),
    ),
    IntentSpec(
        name="OFFICER_WORKLOAD", domain="workflow",
        description="Officer workload and availability.",
        verbs=_INSPECT_VERBS, nouns=_WORKFLOW_NOUNS,
        entities=("officer",),
        phrases=("workload", "who is free", "available officer", "officer capacity"),
    ),
    IntentSpec(
        name="TASK_LIST", domain="workflow",
        description="AI task inbox and assignments.",
        verbs=_INSPECT_VERBS, nouns=("task", "tasks", "inbox", "assignment", "assignments"),
        entities=("limit",),
        phrases=("list tasks", "open tasks", "task inbox"),
    ),
    IntentSpec(
        name="PROPOSAL_LIST", domain="governance",
        description="AI governance proposals awaiting approval.",
        verbs=_INSPECT_VERBS, nouns=_GOVERNANCE_NOUNS,
        entities=("limit",),
        phrases=("pending approval", "approval center", "awaiting approval", "list proposals",
                 "what proposals", "proposal queue", "proposals pending"),
    ),
    IntentSpec(
        name="HEALTH_CHECK", domain="system",
        description="System health, statistics and performance snapshot.",
        verbs=_INSPECT_VERBS + _ANALYZE_VERBS, nouns=_HEALTH_NOUNS,
        entities=(),
        phrases=("health check", "system health", "is everything working", "how is the system"),
    ),
    IntentSpec(
        name="REPORT_REQUEST", domain="reporting",
        description="Ask SA for a briefing or summary (SA cannot mint reports).",
        verbs=_INSPECT_VERBS, nouns=_REPORT_NOUNS,
        entities=("village", "survey", "land_id"),
        phrases=("generate a report", "give me a report", "weekly briefing", "daily briefing"),
    ),
    IntentSpec(
        name="SELF_HEAL", domain="system",
        description="Ask SA to diagnose and repair a known system problem.",
        verbs=_ANALYZE_VERBS + ("repair", "heal", "fix"),
        nouns=("problem", "problems", "issue", "issues", "missing table", "schema", "broken", "failing", "outage"),
        entities=(),
        phrases=("diagnose", "self heal", "self-heal", "repair the", "fix the problem", "what is broken"),
    ),
)

_INTENT_BY_NAME = {spec.name: spec for spec in INTENT_CATALOG}

# Weights for each slot class. A phrase is worth more than a noun because it
# is specific; an entity is worth more than a verb because it is rare. Verb
# and noun scores are per matched term (capped) so "proposals pending" beats
# an intent that only matched "pending".
_WEIGHTS = {"phrase": 1.2, "verb": 0.25, "noun": 0.3, "entity": 0.9}
_MAX_PHRASE_HITS = 2
_MAX_VERB_HITS = 2
_MAX_NOUN_HITS = 3
_COMBINATION_BONUS = 0.4
_MAX_SCORE = 2.0

# A follow-up usually stays in the same domain as the previous turn, so the
# previous domain gets a bonus instead of letting a bare pronoun parse as
# whatever keyword happens to appear.
_FOLLOWUP_INTENT_BONUS = 0.8
_FOLLOWUP_DOMAIN_BONUS = 0.4
_AMBIGUITY_MARGIN = 0.35

# Intents that must never run without an approved proposal. They are derived
# from the governed write actions, not hand-listed here.
_WRITE_INTENT_PREFIX = "WRITE_"


@dataclass
class ParsedCommand:
    """The structured result of interpreting one administrator command."""

    text: str
    normalized: str
    intent: str
    confidence: float
    entities: List[Entity] = field(default_factory=list)
    runner_up: Optional[str] = None
    runner_up_score: float = 0.0
    ambiguous: bool = False
    requires_approval: bool = False
    security: List[SecuritySignal] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    scores: Dict[str, float] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        spec = _INTENT_BY_NAME.get(self.intent)
        if spec:
            return spec.domain
        if self.intent.startswith(_WRITE_INTENT_PREFIX):
            return "governance"
        return "unknown"

    def entity(self, entity_type: str) -> Optional[str]:
        return entity_value(self.entities, entity_type)

    def has(self, *types: str) -> bool:
        return has_entity(self.entities, *types)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "intent": self.intent,
            "domain": self.domain,
            "confidence": round(self.confidence, 3),
            "entities": [e.as_dict() for e in self.entities],
            "runner_up": self.runner_up,
            "ambiguous": self.ambiguous,
            "requires_approval": self.requires_approval,
            "security": [s.kind for s in self.security],
            "notes": list(self.notes),
        }


def _phrase_hits(phrases: Sequence[str], lowered: str) -> List[str]:
    """Match phrases on word boundaries.

    Substring matching silently broke parsing: "hi" is a prefix of "history",
    so "show property history for DEMO-PROP-103-A" was parsed as a greeting.
    """
    hits: List[str] = []
    for phrase in phrases:
        try:
            if re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", lowered):
                hits.append(phrase)
        except re.error:
            if phrase in lowered:
                hits.append(phrase)
    return hits


def _distinct_hits(terms: Sequence[str], lowered: str) -> List[str]:
    """Matched terms with contained duplicates removed.

    "which records are below 60% confidence" matches both "record" and
    "records". Counting both inflated the document intents above the OCR
    intent that the command was actually about.
    """
    matched = [term for term in terms if term in lowered]
    return [
        term for term in matched
        if not any(other != term and term in other for other in matched)
    ]


def _earliest_phrase_position(spec: IntentSpec, lowered: str) -> Optional[int]:
    """Position of the first phrase hit, used to break ties by topic order."""
    positions = []
    for phrase in spec.phrases:
        match = re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", lowered)
        if match:
            positions.append(match.start())
    return min(positions) if positions else None


def score_intent(spec: IntentSpec, normalized: str, entities: Sequence[Entity]) -> float:
    """Score one intent against the command. Higher means better supported."""
    lowered = normalized.lower()
    score = 0.0
    score += _WEIGHTS["phrase"] * min(len(_phrase_hits(spec.phrases, lowered)), _MAX_PHRASE_HITS)
    score += _WEIGHTS["verb"] * min(len(_distinct_hits(spec.verbs, lowered)), _MAX_VERB_HITS)
    score += _WEIGHTS["noun"] * min(len(_distinct_hits(spec.nouns, lowered)), _MAX_NOUN_HITS)
    score += _WEIGHTS["entity"] * (1.0 if has_entity(entities, *spec.entities) else 0.0)
    if any(v in lowered for v in spec.verbs) and any(n in lowered for n in spec.nouns):
        score += _COMBINATION_BONUS
    return round(score, 3)


def _write_intent(text: str) -> Optional[str]:
    """Ask the governed write-action recogniser whether this is a mutation.

    Imported lazily: sa_agent owns the action vocabulary and imports this
    module, so the reference has to be resolved at call time.
    """
    try:
        import sa_agent
        action = sa_agent._write_action(text)
    except Exception:
        return None
    return (_WRITE_INTENT_PREFIX + action) if action else None


def parse_command(text: Any, *, memory: Any = None) -> ParsedCommand:
    """Interpret one administrator command deterministically.

    ``memory`` is optional: when a conversation memory is supplied, a bare
    follow-up ("and its documents?") is interpreted in the context of the
    previous turn instead of scoring as an unknown command.
    """
    raw = "" if text is None else str(text)
    normalized = normalize(raw)
    lowered = normalized.lower()
    entities = extract_entities(normalized)
    security = detect_injection(normalized)
    notes: List[str] = []

    def command(**kwargs: Any) -> ParsedCommand:
        base: Dict[str, Any] = {
            "text": raw,
            "normalized": normalized,
            "entities": entities,
            "security": security,
            "notes": notes,
        }
        base.update(kwargs)
        return ParsedCommand(**base)

    if not lowered:
        return command(intent="EMPTY", confidence=0.0)

    # 1. Conversation intents are exact and cheap: resolve them first so a
    #    greeting containing the word "report" is not parsed as reporting.
    for name in ("GREETING", "CAPABILITY", "CORRECTION"):
        spec = _INTENT_BY_NAME[name]
        if _phrase_hits(spec.phrases, lowered):
            return command(intent=name, confidence=1.0, requires_approval=False)

    # 2. A reference to a problem SA itself reported is a repair request, not
    #    repository work. Checked before the Arena hand-off because "fix the
    #    issue you found" is phrased like implementation work but must not be
    #    sent to a coding agent: the repair belongs to this deployment.
    if memory is not None:
        try:
            import sa_conversation
            open_issues = bool(memory.open_issues())
            mentions_issue = any(
                word in lowered for word in ("issue", "problem", "defect", "fault", "error")
            )
            if open_issues and mentions_issue and _is_referential(lowered):
                notes.append("interpreted as a request to repair a problem SA reported")
                return command(intent="SELF_HEAL", confidence=0.9, notes=notes)
        except Exception:
            pass

    # 3. Repository implementation work belongs to Arena, not to SA.
    try:
        import sa_agent
        if sa_agent._is_arena_task(normalized):
            return command(intent="ARENA_HANDOFF", confidence=1.0)
    except Exception:
        pass

    # 3. Governed mutations. These still need a resolved target and an
    #    approved proposal; the parse only says what was asked for.
    write_intent = _write_intent(normalized)
    if write_intent:
        return command(intent=write_intent, confidence=0.95, requires_approval=True)

    # 4. Score the catalogue.
    scores = {spec.name: score_intent(spec, normalized, entities) for spec in INTENT_CATALOG}

    # A bare follow-up ("and its documents?", "what about the previous one")
    # carries almost no keywords of its own, so it borrows the previous turn's
    # domain instead of being scored as unknown.
    followup = _looks_like_followup(lowered) or _is_referential(lowered)
    if followup and memory is not None:
        last = getattr(memory, "last_intent", None)
        last_spec = _INTENT_BY_NAME.get(last or "")
        if last_spec:
            # The previous intent is the strongest predictor for a bare
            # follow-up; the rest of its domain gets a smaller bonus.
            if last in scores:
                scores[last] = round(scores[last] + _FOLLOWUP_INTENT_BONUS, 3)
            for spec in INTENT_CATALOG:
                if spec.name != last and spec.domain == last_spec.domain:
                    scores[spec.name] = round(scores.get(spec.name, 0.0) + _FOLLOWUP_DOMAIN_BONUS, 3)
            notes.append(f"scored as a follow-up in the '{last_spec.domain}' domain")

    # Break ties by topic order: the intent whose phrase was mentioned first is
    # the primary topic ("find anomalies in low confidence records" is an
    # anomaly request, not an OCR request).
    ranked = sorted(
        scores.items(),
        key=lambda item: (
            item[1],
            _earliest_phrase_position(_INTENT_BY_NAME[item[0]], lowered) if _earliest_phrase_position(_INTENT_BY_NAME[item[0]], lowered) is not None else 10_000,
        ),
    )
    ranked = sorted(ranked, key=lambda item: item[1], reverse=True)
    best, best_score = ranked[0]
    runner_up, runner_up_score = ranked[1] if len(ranked) > 1 else (None, 0.0)

    # 5. A follow-up with no independent signal inherits the previous intent
    #    rather than being reported as unknown.
    if best_score < 0.6 and memory is not None:
        inherited = getattr(memory, "last_intent", None)
        if inherited and _looks_like_followup(lowered):
            notes.append("interpreted as a follow-up to the previous turn")
            return command(
                intent=inherited,
                confidence=0.5,
                runner_up=best,
                runner_up_score=best_score,
                notes=notes,
            )

    if best_score < 0.6:
        return command(
            intent="UNKNOWN",
            confidence=0.0,
            runner_up=best,
            runner_up_score=best_score,
            scores=scores,
        )

    ambiguous = bool(runner_up) and (best_score - runner_up_score) < _AMBIGUITY_MARGIN
    if ambiguous:
        notes.append(f"intent is close to {runner_up}")

    return command(
        intent=best,
        confidence=min(1.0, best_score / _MAX_SCORE),
        runner_up=runner_up,
        runner_up_score=runner_up_score,
        ambiguous=ambiguous,
        requires_approval=_INTENT_BY_NAME[best].requires_approval,
        scores=scores,
    )


_FOLLOWUP_STARTERS = (
    "and ", "also ", "what about", "how about", "its ", "it ", "that ", "this ",
    "the same", "same one", "them", "those", "these", "then ", "now ", "yes",
    "ok ", "okay", "why", "more", "any more", "again",
)


def _is_referential(lowered: str) -> bool:
    """True when the text refers back to something already mentioned."""
    try:
        import sa_conversation
        return sa_conversation.is_referential(lowered)
    except Exception:
        markers = ("that ", "this ", "it ", "its ", "the same", "previous", "last ")
        padded = " " + (lowered or "").strip()
        return any(marker in padded for marker in markers)


def _looks_like_followup(lowered: str) -> bool:
    """True only when the text actually points back at something.

    A short command is not evidence of a follow-up: "purple monkey dishwasher"
    has three words and means nothing, and treating it as a follow-up made SA
    answer the previous question again instead of saying it did not understand.
    """
    stripped = lowered.strip()
    if not stripped:
        return False
    if any(stripped.startswith(starter) for starter in _FOLLOWUP_STARTERS):
        return True
    return _is_referential(stripped)


def intent_description(name: str) -> str:
    spec = _INTENT_BY_NAME.get(name)
    if spec:
        return spec.description
    if name.startswith(_WRITE_INTENT_PREFIX):
        return "Governed write operation: SA prepares a proposal, approval executes it."
    return "Unrecognised command."


def catalog() -> List[Dict[str, Any]]:
    """Public description of what SA understands (used by tests and docs)."""
    return [
        {"name": spec.name, "domain": spec.domain, "description": spec.description,
         "requires_approval": spec.requires_approval}
        for spec in INTENT_CATALOG
    ]
