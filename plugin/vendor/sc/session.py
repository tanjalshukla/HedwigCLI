from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ClaudeSession:
    system_prompt: str
    messages: list[dict[str, str]] = field(default_factory=list)
    session_context: str = ""
    max_messages: int = 40
    memory_notes: list[str] = field(default_factory=list)

    def add_memory_note(self, note: str) -> None:
        normalized = " ".join(note.split()).strip()
        if not normalized:
            return
        self.memory_notes.append(normalized)
        if len(self.memory_notes) > 8:
            self.memory_notes = self.memory_notes[-8:]

    def set_session_context(self, text: str) -> None:
        self.session_context = text.strip()

    def effective_system_prompt(self) -> str:
        segments = [self.system_prompt]
        if self.memory_notes:
            memory_block = "\n".join(f"- {line}" for line in self.memory_notes[-4:])
            segments.append(f"## Pinned Memory\n{memory_block}")
        if self.session_context:
            segments.append(f"## This Session\n{self.session_context}")
        return "\n\n".join(segments)

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
        self._trim_messages()

    def add_assistant(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})
        self._trim_messages()

    def _trim_messages(self) -> None:
        if self.max_messages <= 0 or len(self.messages) <= self.max_messages:
            return
        if self.max_messages >= 2 and len(self.messages) > 1:
            first = self.messages[0]
            remaining_budget = self.max_messages - 1
            self.messages = [first] + self.messages[-remaining_budget:]
            return
        self.messages = self.messages[-self.max_messages :]
