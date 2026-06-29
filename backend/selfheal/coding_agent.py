"""Coding agent: reads + patches a generated agent file using the Anthropic API.

Invocation method (non-interactive, programmatic):
    anthropic.Anthropic().messages.create() with tool use — the standard agentic
    loop from the Anthropic Python SDK (>=0.50.0, tested at 0.112.0).

Why not the claude CLI?
    The CLI (`claude -p "..."`) works for one-shot prompts but gives no way to
    enforce strict file-path restrictions from Python — the coding agent could
    accidentally edit any file the process can reach. The SDK tool-use approach
    lets us enforce the restriction in _execute_tool() before any write goes
    through: both write_file and str_replace check the target path before touching
    the filesystem.

Tool set (three tools):
    read_file  — allowed paths: the generated agent file + CONTRACT.md
    str_replace — replace an exact substring in the generated agent file (PREFERRED)
    write_file  — overwrite the entire generated agent file (fallback for large restructures)

    IMPORTANT: str_replace is preferred. It emits only the changed block, which
    drastically reduces output size and eliminates truncation risk. write_file is
    a fallback for cases where a large structural change is needed.

Path restrictions:
    - str_replace and write_file enforce the same allowed-path guard via
      pathlib.Path.resolve() before any write reaches disk, including ../traversal
      protection.
    - str_replace also checks that old_str appears exactly once (no ambiguous edits).

Token budget:
    MAX_AGENT_OUTPUT_TOKENS = 16_000. claude-sonnet-4-6 supports up to 64k output
    tokens; 16k gives comfortable headroom for a full-file rewrite (~5-6k tokens)
    plus preamble reasoning, preventing max_tokens truncation on large patches.
    If max_tokens is still hit (shouldn't be), the turn is treated as failed —
    no partial write is applied, and the circuit-breaker in heal.py handles it.
"""

import logging
import pathlib
from typing import Any

import anthropic

log = logging.getLogger(__name__)

MODEL                  = "claude-sonnet-4-6"
MAX_AGENT_TURNS        = 12
MAX_AGENT_OUTPUT_TOKENS = 16_000   # well above full-file rewrite (~5-6k tokens)


def run_coding_agent(
    goal: str,
    agent_file_path: pathlib.Path,
    contract_path: pathlib.Path,
    api_key: str,
) -> dict:
    """Run a coding agent that reads and patches agent_file_path based on goal.

    Returns:
        {
            "success": bool,
            "turns":   int,      — number of LLM turns consumed
            "error":   str|None, — set on failure
        }
    """
    client = anthropic.Anthropic(api_key=api_key)

    # Resolve paths once to ensure consistent comparison in _execute_tool
    agent_path_resolved    = str(agent_file_path.resolve())
    contract_path_resolved = str(contract_path.resolve())

    allowed_read: set[str] = {agent_path_resolved, contract_path_resolved}
    writable: str          = agent_path_resolved

    tools: list[dict[str, Any]] = [
        {
            "name": "read_file",
            "description": (
                "Read the contents of an allowed file. "
                f"Allowed paths: {sorted(allowed_read)}"
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to read.",
                    }
                },
                "required": ["path"],
            },
        },
        {
            "name": "str_replace",
            "description": (
                "PREFERRED EDIT TOOL. Replace an exact substring in the generated agent "
                "file with a new string. Use this instead of write_file whenever the fix "
                "targets a specific block — it emits only the changed section and is "
                "much less likely to hit output-token limits. "
                f"ONLY allowed path: {writable}. "
                "old_str must appear exactly once in the file."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": f"Must be exactly: {writable}",
                    },
                    "old_str": {
                        "type": "string",
                        "description": (
                            "The exact substring to replace. Must appear exactly once "
                            "in the current file content."
                        ),
                    },
                    "new_str": {
                        "type": "string",
                        "description": "The replacement string.",
                    },
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
        {
            "name": "write_file",
            "description": (
                "Overwrite the generated agent file with corrected content. "
                "Use this ONLY when a large structural change is required; "
                "prefer str_replace for targeted fixes. "
                f"ONLY allowed path: {writable}. "
                "Write the COMPLETE file content — this replaces the entire file."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": f"Must be exactly: {writable}",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete corrected file content (entire file, not a diff).",
                    },
                },
                "required": ["path", "content"],
            },
        },
    ]

    system = (
        "You are a coding agent that fixes a Temporal workflow file.\n\n"
        "ALLOWED READS:\n"
        + "\n".join(f"  - {p}" for p in sorted(allowed_read))
        + f"\n\nALLOWED WRITES:\n  - {writable}\n\n"
        "PREFERRED APPROACH:\n"
        "  Use str_replace to make a TARGETED edit — replace only the buggy block.\n"
        "  Only use write_file if you need to restructure large portions of the file.\n"
        "  str_replace is faster, less likely to hit output limits, and safer.\n\n"
        "RULES (enforced — violations will return an error from the tool):\n"
        "1. DO NOT write to any path other than the one listed above.\n"
        "2. Keep all workflow.execute_activity() calls intact — same S7 activity\n"
        "   functions, same import paths from backend.runtime.activities.*\n"
        "3. Make the SMALLEST change that fixes the listed failures.\n"
        "4. The result must be valid Python (passes py_compile).\n"
        "5. After writing the corrected file, stop (do not add unrequested features).\n"
    )

    messages: list[dict] = [{"role": "user", "content": goal}]
    turns = 0

    while turns < MAX_AGENT_TURNS:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_AGENT_OUTPUT_TOKENS,
            system=system,
            tools=tools,
            messages=messages,
        )
        turns += 1
        log.info(
            "coding_agent turn=%d stop_reason=%s blocks=%d",
            turns, response.stop_reason, len(response.content),
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            log.info("coding_agent: done (end_turn) after %d turns", turns)
            return {"success": True, "turns": turns, "error": None}

        if response.stop_reason == "max_tokens":
            # Response was truncated — any tool call in this turn is incomplete
            # and was never executed. The file is unchanged. Return failure so
            # the circuit-breaker in heal.py can handle it (revert backup, stall/stop).
            err = (
                f"Response truncated (max_tokens={MAX_AGENT_OUTPUT_TOKENS}) — "
                "patch incomplete; no write applied this turn."
            )
            log.warning("coding_agent: %s", err)
            return {"success": False, "turns": turns, "error": err}

        if response.stop_reason != "tool_use":
            err = f"Unexpected stop_reason: {response.stop_reason!r}"
            log.warning("coding_agent: %s", err)
            return {"success": False, "turns": turns, "error": err}

        # Execute all tool calls in the response
        tool_results: list[dict] = []
        for block in response.content:
            if not hasattr(block, "type") or block.type != "tool_use":
                continue
            result_text = _execute_tool(block.name, block.input, allowed_read, writable)
            log.info(
                "coding_agent tool=%s path=%r → %d chars result",
                block.name, block.input.get("path", "?"), len(result_text),
            )
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     result_text,
            })

        messages.append({"role": "user", "content": tool_results})

    err = f"Max agent turns ({MAX_AGENT_TURNS}) reached without completion"
    log.warning("coding_agent: %s", err)
    return {"success": False, "turns": turns, "error": err}


def _execute_tool(
    name: str,
    input_dict: dict,
    allowed_read: set[str],
    writable: str,
) -> str:
    raw_path = input_dict.get("path", "")
    # Resolve to catch "../" traversal attempts
    try:
        path = str(pathlib.Path(raw_path).resolve())
    except Exception:
        path = raw_path

    if name == "read_file":
        if path not in allowed_read:
            return (
                f"ERROR: path {path!r} is not in the allowed read list. "
                f"Allowed: {sorted(allowed_read)}"
            )
        try:
            return pathlib.Path(path).read_text(encoding="utf-8")
        except Exception as exc:
            return f"ERROR reading {path!r}: {exc}"

    if name == "str_replace":
        if path != writable:
            return (
                f"ERROR: can only edit {writable!r}, not {path!r}. "
                "Do not attempt to modify other files."
            )
        old_str = input_dict.get("old_str", "")
        new_str = input_dict.get("new_str", "")
        if not old_str:
            return "ERROR: old_str must not be empty."
        try:
            current = pathlib.Path(path).read_text(encoding="utf-8")
        except Exception as exc:
            return f"ERROR reading {path!r}: {exc}"
        count = current.count(old_str)
        if count == 0:
            # Return a short excerpt to help the agent identify the right string
            return (
                "ERROR: old_str not found in file. "
                "Check that it matches the file content exactly (whitespace, indentation). "
                "Use read_file to inspect the current content."
            )
        if count > 1:
            return (
                f"ERROR: old_str appears {count} times in the file — be more specific "
                "(add more surrounding context so the target is unique)."
            )
        patched = current.replace(old_str, new_str, 1)
        try:
            pathlib.Path(path).write_text(patched, encoding="utf-8")
            return f"OK: replaced 1 occurrence; file is now {len(patched)} chars"
        except Exception as exc:
            return f"ERROR writing {path!r}: {exc}"

    if name == "write_file":
        if path != writable:
            return (
                f"ERROR: can only write to {writable!r}, not {path!r}. "
                "Do not attempt to modify other files."
            )
        content = input_dict.get("content", "")
        try:
            pathlib.Path(path).write_text(content, encoding="utf-8")
            return f"OK: wrote {len(content)} chars to {path}"
        except Exception as exc:
            return f"ERROR writing {path!r}: {exc}"

    return f"ERROR: unknown tool {name!r}"
