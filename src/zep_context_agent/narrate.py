"""Narration: prose about the facts, never a source of them.

The brief's facts are assembled in code. A model may optionally
turn them into a short summary paragraph — that is the only thing
a model does in this blueprint. The discipline:

- No model configured → a deterministic **template** narrative,
  labelled ``template``.
- Model configured → its paragraph is checked by
  :func:`guardrails.verify_narrative`: every number it uses must
  be a number the cited facts contain. A paragraph that invents a
  figure is **discarded**, the template is used, and the brief
  carries a flag saying the model narrative was rejected — the
  rejection is never silent.
- A model that is configured but unreachable raises the same
  question as a missing Zep key, with the opposite answer: the
  brief does not need the narrative, so the failure is flagged on
  the brief and the template serves. The facts were never the
  model's to lose.

Configuration: ``OPENAI_API_KEY`` (required for the model path),
``OPENAI_BASE_URL`` (any OpenAI-compatible endpoint — a self-hosted
NVIDIA NIM container or the hosted NIM catalog included),
``OPENAI_MODEL``.
"""

from __future__ import annotations

import httpx

from .config import env_str
from .guardrails import verify_narrative
from .schemas import Brief

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


def template_narrative(brief: Brief) -> str:
    name = brief.account_name
    facts = {f.predicate: f for f in brief.current_facts}
    parts = [f"Account brief for {name}, assembled as of {brief.as_of.isoformat()}."]
    if "risk_class" in facts:
        parts.append(f"Risk class: {facts['risk_class'].value}.")
    if "annual_revenue_usd" in facts:
        parts.append(
            f"Annual revenue USD {facts['annual_revenue_usd'].value:,}."
        )
    if "policy_limit_usd" in facts:
        parts.append(
            f"Current policy limit USD {facts['policy_limit_usd'].value:,}."
        )
    if "claims_count_3yr" in facts:
        parts.append(
            f"Claims in the last three years: {facts['claims_count_3yr'].value}."
        )
    parts.append(
        f"The brief rests on {len(brief.sources)} source document(s), "
        f"with {len(brief.current_facts)} current fact(s) and "
        f"{len(brief.superseded_facts)} superseded fact(s) shown separately."
    )
    if brief.missing_predicates:
        parts.append(
            "Not resolved from the file: " + ", ".join(brief.missing_predicates) + "."
        )
    return " ".join(parts)


def _model_narrative(brief: Brief, *, client: httpx.Client | None = None) -> str:
    api_key = env_str("OPENAI_API_KEY")
    base_url = (env_str("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    model = env_str("OPENAI_MODEL") or DEFAULT_MODEL
    fact_lines = "\n".join(
        f"- {f.predicate}: {f.value}{(' ' + f.unit) if f.unit else ''}"
        for f in brief.current_facts
    )
    prompt = (
        "Write a 3-4 sentence summary for an underwriter from ONLY "
        "these assembled facts. Use no number that is not in the "
        f"facts.\n\nAccount: {brief.account_name}\n{fact_lines}"
    )
    http = client or httpx.Client(timeout=30.0)
    response = http.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": "You summarise assembled underwriting facts. "
                    "You never add facts or figures.",
                },
                {"role": "user", "content": prompt},
            ],
        },
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


def narrate(brief: Brief, *, client: httpx.Client | None = None) -> Brief:
    """Attach the narrative to ``brief`` (mutates and returns it).

    Template by default; the model path only when a key is set, and
    only when its paragraph survives verification.
    """
    brief.narrative = template_narrative(brief)
    brief.narrative_source = "template"
    if not env_str("OPENAI_API_KEY"):
        return brief
    model = env_str("OPENAI_MODEL") or DEFAULT_MODEL
    try:
        candidate = _model_narrative(brief, client=client)
    except Exception as exc:  # configured but unreachable: flag, template serves
        brief.flags.append(
            f"Narrative model configured but unavailable ({exc}); "
            "the template narrative is used."
        )
        return brief
    invented = verify_narrative(candidate, brief)
    if invented:
        brief.flags.append(
            "Model narrative discarded: it used figure(s) the cited "
            f"facts do not contain ({', '.join(invented)}). The "
            "template narrative is used."
        )
        return brief
    brief.narrative = candidate
    brief.narrative_source = f"model:{model}"
    return brief
