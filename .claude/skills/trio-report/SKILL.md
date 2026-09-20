---
name: trio-report
description: Turn a trio-test-harness run (screenshots + step log) into deliverables — a self-contained HTML report published as an Artifact, and a GitHub PR comment or PR-description section with the screenshots hosted on GitHub. Use after testing Trio when results must be shared with the user or attached to a pull request.
---

# trio-report

`cd ~/repos/trio-test-harness`; `th` = `./bin/th`. Runs live in `runs/<timestamp>-<name>/` with `screenshots/`,
`steps.jsonl`, `run.json`. `th run list`, `th run steps`, `th run use <dir>` select a run.

## Steps
1. Make sure the run is closed with a verdict: `th run finish passed|failed|partial --summary "<what was verified / what failed>"`.
   Add observations first with `th run note "…"` if needed.
2. `th report html` → `runs/<run>/report.html` (dark/light aware, screenshots embedded as JPEG thumbnails, < 16 MB).
   Publish it with the **Artifact** tool (`file_path` = that HTML, icon `phone`) and give the user the link. One artifact per run.
3. PR delivery — always ask first (AskUserQuestion: post a comment with the report? yes/no), then:
   - `th report pr <PR URL | owner/repo#N | N> --mode comment --artifact-url <claude.ai artifact link>` posts a comment: verdict, commit, submodule overrides, Xcode, summary, report link,
     and a 3-column screenshot table. Images are uploaded to `TH_ARTIFACT_REPO` (public GitHub repo, created on first use) and
     referenced by raw URL — GitHub's API cannot attach images directly.
   - `--mode description` appends/replaces a `<!-- trio-test-harness -->` section in the PR body instead.
   - `--max-shots N` / `--columns N` to trim. Preview without posting: `th report markdown --upload`.
4. In chat, give: verdict, commit/branch, the key screenshots' meaning, untested areas, and links (artifact, PR comment).

## Rules
- Screenshots must come from simulator devices / fake data only. Never include UDIDs, Apple IDs, tokens, or real CGM data.
- Don't rewrite a PR description unless asked; comments are the default.
