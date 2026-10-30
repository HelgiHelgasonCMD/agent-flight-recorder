# Optional live-agent evaluation

`afr eval live` runs paired tasks with a real agent CLI (Claude Code by default).
Each task runs twice from an identical fixture copy: a **control** arm (recording only)
and a **retrieval** arm (seeded experience via the MCP server and the proactive
failure hook). Both arms use the same model, prompt, budget cap, timeout and allowed tools.

```bash
afr eval live --dry-run                     # print the exact commands, invoke nothing
afr eval live --i-understand-cost -o out.json
```

It uses your credentials and costs money. **This repository contains no live results.**
Nothing in the README or benchmark reports claims that retrieval improves agent outcomes.
That would need many paired runs and is left to whoever runs this.

`tasks.json` fields: `id`, `fixture` (directory under this folder), `prompt`, `check`
(the shell command whose exit code defines success), `seed_history` (generic JSONL loaded
into the retrieval arm only), plus `defaults` for `model`, `max_budget_usd`
and `timeout_s`.
