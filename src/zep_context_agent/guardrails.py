"""Guardrails as code: screening, leakage, citations, narration.

Four checks, none of them a prompt:

1. **Injection screen** — correspondence text is untrusted input.
   Text that tries to instruct the reader of the file ("ignore the
   claims history", "show a clean record", "disregard the
   endorsement") marks its document *quarantined*: the episode is
   recorded, its assertions are excluded from the graph's facts,
   and the brief names the document and the reason. The screen runs
   at ingestion, before anything in the document is believed.
2. **Leakage check** — every fact, episode, and edge in an
   assembled brief must belong to the brief's account. Retrieval
   is account-scoped in the adapters; this check re-verifies the
   assembled artifact, because the artifact is what an underwriter
   reads. Near-identical account names are the designed trap; ids,
   not names, are what get checked.
3. **Citation completeness** — every current fact in a brief must
   cite an episode of the same account that actually carries it.
4. **Narrative verification** — a model may narrate the assembled
   facts, never add one. Every number in a model narrative must be
   a number the cited facts (or the brief's own dates) contain;
   a narrative that invents a figure is discarded and the template
   narrative is used, labelled.
"""

from __future__ import annotations

import re

from .ontology import Edge, Episode, Fact, SourceDocument
from .schemas import Brief, CitedFact

# ---------------------------------------------------------------------------
# 1. Injection screen
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"ignore\s+(all\s+|the\s+|prior\s+|previous\s+)*(claims|history|instructions|record|endorsements?)", "instructs the reader to ignore part of the file"),
    (r"disregard\s+(the\s+|all\s+|prior\s+)*(claims|history|instructions|record|endorsements?|loss)", "instructs the reader to disregard part of the file"),
    (r"show\s+(a\s+)?clean\s+(history|record)", "asks for a falsified clean history"),
    (r"present\s+(a\s+)?clean\s+(history|record)", "asks for a falsified clean history"),
    (r"omit\s+(the\s+|all\s+)*(claim|loss|endorsement)", "asks for part of the file to be omitted"),
    (r"do\s+not\s+(report|disclose|mention|include)\s+(the\s+|this\s+|any\s+)*(claim|loss|endorsement|history)", "asks for part of the file to be withheld"),
    (r"system\s+prompt", "references a system prompt"),
    (r"you\s+are\s+now\s+(a|an|the)\b", "attempts to reassign the reader's role"),
    (r"forget\s+(everything|all|the)\b", "instructs the reader to forget the file"),
)

_COMPILED = [(re.compile(p, re.IGNORECASE), why) for p, why in _INJECTION_PATTERNS]


def screen_document(doc: SourceDocument) -> tuple[bool, str]:
    """Screen one document's free text for instruction injection.

    Returns ``(quarantined, reason)``. Only documents carrying free
    text can trip the screen; structured documents (an ACORD
    extract's assertions) carry no prose to hide an instruction in.
    The screen is deliberately narrow: ordinary broker language
    ("please disregard my earlier email") does not match the
    file-directed patterns above, and the eval pack pins that.
    """
    if not doc.text:
        return False, ""
    for pattern, why in _COMPILED:
        if pattern.search(doc.text):
            return True, f"Injection screen: text {why}."
    return False, ""


def fence_text(text: str) -> str:
    """Fence untrusted text for display: clearly delimited, never
    presented as system content."""
    return f"<<<UNTRUSTED DOCUMENT TEXT>>>\n{text}\n<<<END UNTRUSTED TEXT>>>"


# ---------------------------------------------------------------------------
# 2 + 3. Leakage and citation checks over an assembled brief
# ---------------------------------------------------------------------------


def check_brief(
    *,
    account_id: str,
    current_facts: list[CitedFact],
    episodes: list[Episode],
    edges: list[Edge],
    account_facts: list[Fact],
) -> tuple[bool, bool, list[str]]:
    """Verify an assembled brief against the account boundary.

    ``account_facts`` is the ground truth: every fact the port
    holds for this account. A citation is complete when the cited
    (predicate, value, entity_ref, episode) exists among them;
    leakage is any episode or edge in the brief that belongs to a
    different account. Returns
    ``(citations_complete, leakage_free, violations)``.
    """
    violations: list[str] = []
    leakage: list[str] = []
    episode_ids = set()
    for episode in episodes:
        episode_ids.add(episode.episode_id)
        if episode.account_id != account_id:
            leakage.append(
                f"episode {episode.episode_id} belongs to "
                f"{episode.account_id}, not {account_id}"
            )
    carried = {
        (f.predicate, f.value, f.entity_ref, f.episode_id) for f in account_facts
    }
    citations_complete = True
    for fact in current_facts:
        key = (fact.predicate, fact.value, fact.entity_ref, fact.episode_id)
        if fact.episode_id not in episode_ids or key not in carried:
            citations_complete = False
            violations.append(
                f"fact {fact.predicate}={fact.value} cites episode "
                f"{fact.episode_id}, which does not carry it for this account"
            )
    for edge in edges:
        if edge.account_id != account_id:
            leakage.append(
                f"edge {edge.edge_type} {edge.source_ref}->{edge.target_ref} "
                f"belongs to {edge.account_id}, not {account_id}"
            )
    return citations_complete, not leakage, violations + leakage


def brief_violations(brief: Brief, account_facts: list[Fact]) -> list[str]:
    """Re-run the checks over a finished brief (eval + API use)."""
    _, _, violations = check_brief(
        account_id=brief.account_id,
        current_facts=brief.current_facts,
        episodes=brief.sources,
        edges=brief.edges,
        account_facts=account_facts,
    )
    return violations


# ---------------------------------------------------------------------------
# 4. Narrative verification
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*")


def _numbers(text: str) -> set[str]:
    found = set()
    for token in _NUMBER_RE.findall(text):
        cleaned = token.replace(",", "").rstrip(".")
        if cleaned:
            # Normalise 2500000.0 -> 2500000 so model formatting
            # ("2,500,000") and fact values compare equal.
            if "." in cleaned:
                cleaned = cleaned.rstrip("0").rstrip(".")
            found.add(cleaned)
    return found


def allowed_numbers(brief: Brief) -> set[str]:
    """Every number a narrative about this brief may legitimately use:
    fact values (current and superseded), counts of the brief's own
    sections, and the years its dates carry."""
    allowed: set[str] = set()
    for fact in [*brief.current_facts, *brief.superseded_facts]:
        if isinstance(fact.value, (int, float)):
            allowed |= _numbers(str(fact.value))
        else:
            allowed |= _numbers(str(fact.value))
        allowed.add(str(fact.valid_from.year))
        allowed.add(str(fact.recorded_at.year))
    allowed |= {
        str(len(brief.current_facts)),
        str(len(brief.superseded_facts)),
        str(len(brief.sources)),
        str(brief.as_of.year),
    }
    return allowed


def verify_narrative(text: str, brief: Brief) -> list[str]:
    """Return the numbers in ``text`` the brief does not support.
    An empty list means the narrative invented nothing numeric."""
    allowed = allowed_numbers(brief)
    return sorted(_numbers(text) - allowed)
