---
description: Study this session's decision traces and propose standing rules (preferences, guidelines, repo facts) for Hedwig to consider
---

Read how the developer has been steering you this session and propose standing
rules Hedwig should consider — coding-style guidelines, repo facts, or
when-to-pause preferences. Hedwig records them as PENDING candidates; nothing
changes behavior until the developer confirms one with `/hedwig-learn`.

Do this in two steps:

1. Read this session's decision traces (each line has an `[id]` to cite):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-notice.py" traces \
  "${CLAUDE_PROJECT_DIR:-$PWD}" "${CLAUDE_SESSION_ID:-}"
```

2. For each genuine, repeated pattern, propose a candidate citing the real
`[id]`s that back it, then pipe them in (see the `repo-hypotheses` skill for the
exact JSON shape and the three candidate types):

```bash
echo '{"cwd":"'"${CLAUDE_PROJECT_DIR:-$PWD}"'","session_id":"'"${CLAUDE_SESSION_ID:-}"'","candidates":[ ... ]}' \
  | python3 "${CLAUDE_PLUGIN_ROOT}/bin/hedwig-notice.py"
```

Every candidate must cite at least one real trace ID — uncited proposals are
dropped. Report the command's output verbatim. Don't invent patterns; only
propose what the traces actually show.
