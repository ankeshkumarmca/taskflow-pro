"""
AI-augmented dependency suggestion.

Design principles (per the brief's AI/LLM evaluation criterion):
  1. GROUNDING: the model only ever sees the current board's task list. It
     is not allowed to invent tasks -- it must reference existing task IDs.
  2. STRUCTURED OUTPUT: the model returns JSON only (id + confidence +
     rationale), never free text we'd have to parse heuristically.
  3. NO AUTHORITY: this module never writes to task_dependencies directly.
     Every suggestion returned here still has to pass through
     dag_engine.validate_new_edge() (cycle check) in the API layer, and
     still requires explicit user acceptance before persistence. The LLM
     proposes; the DAG engine and the user decide.
"""

import json
import os

from .dag_engine import would_create_cycle
from .schemas import DependencySuggestion

SYSTEM_PROMPT = """You suggest likely task PREREQUISITES for a software project Kanban board.

You will be given the target task and a list of candidate tasks already on the board.
Identify which candidate tasks the target task most likely depends on (i.e. which
candidates must finish BEFORE the target task can start), based only on their
titles and descriptions.

Rules:
- Only reference task IDs from the candidate list provided. Never invent a task.
- Only suggest a dependency if there is a clear, concrete technical or logical reason
  (e.g. "requires the API this task builds on", "needs the schema this defines").
  If nothing clearly qualifies, return an empty list -- do not guess to fill space.
- For each suggestion, give a confidence of "high", "medium", or "low" and a short
  (one sentence) rationale grounded in the task descriptions you were given.

Respond with ONLY valid JSON, no other text, in this exact shape:
{"suggestions": [{"depends_on_task_id": <int>, "confidence": "high|medium|low", "rationale": "<string>"}]}
"""


def _build_user_prompt(target: dict, candidates: list[dict]) -> str:
    candidate_lines = "\n".join(
        f"- id={c['id']}: {c['title']} -- {c['description']}" for c in candidates
    )
    return (
        f"Target task: id={target['id']}: {target['title']} -- {target['description']}\n\n"
        f"Candidate tasks (existing on the board):\n{candidate_lines}\n\n"
        f"Which candidate task IDs is the target task's most likely prerequisite set?"
    )


def _call_claude(system_prompt: str, user_prompt: str) -> str:
    """Thin wrapper around the Anthropic API. Requires ANTHROPIC_API_KEY."""
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


def suggest_dependencies(
    target_task: dict,
    candidate_tasks: list[dict],
    existing_edges: list[tuple[int, int]],
) -> list[DependencySuggestion]:
    """
    Return a list of grounded, cycle-safe DependencySuggestion objects for
    target_task. Candidates that would create a cycle, or that duplicate an
    existing edge, or that reference an unknown task ID, are filtered out
    here -- the caller can still re-validate on accept, but suggestions are
    never shown for something we already know is invalid.
    """
    candidates = [c for c in candidate_tasks if c["id"] != target_task["id"]]
    if not candidates:
        return []

    user_prompt = _build_user_prompt(target_task, candidates)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        # No key configured (e.g. local dev/CI) -- fail closed with no
        # suggestions rather than silently faking model output.
        return []

    raw = _call_claude(SYSTEM_PROMPT, user_prompt)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []  # never surface malformed model output to the user

    candidate_ids = {c["id"] for c in candidates}
    existing_pairs = set(existing_edges)

    results: list[DependencySuggestion] = []
    for item in parsed.get("suggestions", []):
        dep_id = item.get("depends_on_task_id")
        if dep_id not in candidate_ids:
            continue  # model referenced a task outside the grounded set -- drop it
        if (target_task["id"], dep_id) in existing_pairs:
            continue  # already an edge, nothing to suggest
        if would_create_cycle(target_task["id"], dep_id, existing_edges):
            continue  # would violate the DAG -- never suggest an invalid edge
        results.append(
            DependencySuggestion(
                depends_on_task_id=dep_id,
                confidence=item.get("confidence", "low"),
                rationale=item.get("rationale", ""),
            )
        )
    return results
