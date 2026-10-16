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
- No attribution: commit messages carry no `Co-Authored-By` trailer, and PR bodies carry no "Generated with Claude Code" line.
