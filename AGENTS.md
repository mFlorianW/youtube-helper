<!--
SPDX-FileCopyrightText: 2026 Florian Weßel <florianwessel@gmx.net>

SPDX-License-Identifier: GPL-2.0-only
-->

# AGENTS.md

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues on `mFlorianW/youtube-helper` (via the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Workflow

- Work each ticket on its own branch, named `<issue-number>-<short-slug>` (e.g. `13-walking-skeleton`), branched from `main`.
- Never commit directly to `main`.
- When the ticket's acceptance criteria are met and lint, type check and tests pass, push the branch and open a pull request with `gh pr create`.
- The PR body links the ticket with `Closes #<issue-number>`; use the `/pr` skill to write it.
- Commit messages are as short as possible while still understandable to a human reading the history: a concise summary line saying what changed (and why, if not obvious); add a body only when the summary alone isn't enough.
- Licensing: the project is GPL-2.0-only and must stay [REUSE](https://reuse.software)-compliant. Every new file gets an SPDX header via `reuse annotate --copyright "Florian Weßel <florianwessel@gmx.net>" --license GPL-2.0-only <file>`; files that can't carry comments (e.g. JSON) are covered in `REUSE.toml`. The `reuse` pre-commit hook rejects commits that break compliance.
- No attribution: commit messages carry no `Co-Authored-By` trailer, and PR bodies carry no "Generated with Claude Code" line.
