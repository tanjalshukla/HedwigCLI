from __future__ import annotations

import json
from typing import Any

from anthropic import AnthropicBedrock

from .schema import IntentDeclaration, ReadRequest
from .session import ClaudeSession

RUN_SYSTEM_PROMPT = """
MODE: CODE
You are a coding agent operating under a strict permission system.
You must first output a JSON declaration that matches the schema exactly.
planned_files must be minimal and repo-relative.
Do not include markdown or code fences.
You are not allowed to modify files outside planned_files.
When asked for file updates, output JSON only as instructed.
Minimize changes and avoid refactors or reformatting unrelated code.
Only touch lines required for the task. If more files are needed, explain in notes.
""".strip()

ASK_SYSTEM_PROMPT = """
MODE: ASK
You are a helpful software engineering assistant.
Answer questions clearly and concisely.
If file context is provided, use it.
Do not propose code changes unless asked.
Do not output JSON or patches.
""".strip()

DECLARE_SCHEMA = {
    "task_summary": "string",
    "planned_files": ["string"],
    "planned_actions": ["edit_code", "add_tests", "run_tests"],
    "planned_commands": ["pytest -q"],
    "notes": "string|null",
}

READ_REQUEST_SCHEMA = {
    "type": "read_request",
    "files": ["string"],
    "reason": "string|null",
}

class ClaudeClient:
    def __init__(self, model_id: str, region: str) -> None:
        self.model_id = model_id
        self.client = AnthropicBedrock(aws_region=region)

    def _response_text(self, response: Any) -> str:
        if hasattr(response, "content"):
            blocks = response.content
            if isinstance(blocks, list):
                chunks: list[str] = []
                for block in blocks:
                    if isinstance(block, dict):
                        chunks.append(block.get("text", "") or "")
                        continue
                    text = getattr(block, "text", None)
                    if text is None and hasattr(block, "get"):
                        text = block.get("text", "")
                    chunks.append(text or "")
                return "".join(chunks)
        if isinstance(response, dict):
            blocks = response.get("content")
            if isinstance(blocks, list):
                return "".join(block.get("text", "") for block in blocks)
        return str(response)

    def _call(self, session: ClaudeSession, max_tokens: int, temperature: float) -> str:
        response = self.client.messages.create(
            model=self.model_id,
            max_tokens=max_tokens,
            temperature=temperature,
            system=session.system_prompt,
            messages=session.messages,
        )
        return self._response_text(response)

    def declare_intent(
        self,
        session: ClaudeSession,
        task: str,
        max_tokens: int,
        temperature: float,
    ) -> IntentDeclaration | ReadRequest:
        schema_json = json.dumps(DECLARE_SCHEMA, indent=2)
        read_schema_json = json.dumps(READ_REQUEST_SCHEMA, indent=2)
        declaration_prompt = (
            "Return JSON only.\n"
            "You must return either an intent declaration OR a read request.\n"
            "Intent schema:\n"
            f"{schema_json}\n\n"
            "Read request schema:\n"
            f"{read_schema_json}\n\n"
            "Before responding, silently verify:\n"
            "1) Each planned file is strictly necessary.\n"
            "2) You cannot solve the task with fewer files.\n"
            "3) Planned actions are minimal and directly required.\n"
            "If any file is optional, remove it.\n\n"
            "If provided file contents appear truncated or insufficient, return a read_request instead of intent.\n\n"
            "In notes, include a short 1-3 step plan if helpful, otherwise null.\n\n"
            f"Task: {task}"
        )
        session.add_user(declaration_prompt)
        for attempt in range(2):
            raw = self._call(session, max_tokens=max_tokens, temperature=temperature)
            session.add_assistant(raw)
            try:
                return IntentDeclaration.model_validate_json(raw)
            except Exception:
                try:
                    return ReadRequest.model_validate_json(raw)
                except Exception:
                    if attempt == 1:
                        raise
                    session.add_user(
                        "Return valid JSON only. Must match intent schema or read request schema."
                    )
        raise RuntimeError("Failed to obtain valid intent declaration.")

    def generate_updates(
        self,
        session: ClaudeSession,
        declaration: IntentDeclaration,
        file_context: dict[str, str],
        max_tokens: int,
        temperature: float,
        repair_hint: str | None = None,
    ) -> dict[str, str]:
        decl_json = declaration.model_dump_json(indent=2)
        context_blocks: list[str] = []
        for path, content in file_context.items():
            context_blocks.append(f"FILE: {path}\n-----\n{content}\n-----")
        context_blob = "\n\n".join(context_blocks)
        patch_prompt = (
            "Return JSON only.\n"
            "Return a JSON object with key 'files' containing a list of objects:\n"
            "{ \"path\": \"...\", \"content\": \"...\" }\n"
            "The content must be a JSON string using \\n for newlines.\n"
            "Include only files that should change, and only from this list:\n"
            f"{json.dumps(declaration.planned_files)}\n\n"
            "Declaration JSON:\n"
            f"{decl_json}\n\n"
            "Current file contents:\n"
            f"{context_blob}"
        )
        if repair_hint:
            patch_prompt = (
                "Previous response was invalid.\n"
                f"Error: {repair_hint}\n"
                "Return valid JSON only. Use \\n in content strings.\n\n"
            ) + patch_prompt
        session.add_user(patch_prompt)
        for attempt in range(2):
            raw = self._call(session, max_tokens=max_tokens, temperature=temperature)
            session.add_assistant(raw)
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError("Response must be a JSON object.")
                files = payload.get("files")
                if not isinstance(files, list):
                    raise ValueError("Missing files array.")
                updates: dict[str, str] = {}
                for item in files:
                    if not isinstance(item, dict):
                        raise ValueError("Each file entry must be an object.")
                    path = item.get("path")
                    content = item.get("content")
                    if not isinstance(path, str) or not isinstance(content, str):
                        raise ValueError("path and content must be strings.")
                    updates[path] = content
                return updates
            except Exception as exc:
                if attempt == 1:
                    raise
                session.add_user(
                    f"Return valid JSON only. Error: {exc}. Use \\n in content."
                )
        raise RuntimeError("Failed to obtain valid file updates.")
