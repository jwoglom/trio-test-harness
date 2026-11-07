---
name: trio-test-ci
description: Run the Trio (or Loop) e2e test on the self-hosted macOS workers through GitHub Actions instead of on this Mac — trigger the "Trio e2e (simulator)" workflow with `./trio-test-ci`, follow it, download its run (report.html, screenshots, agent summary) and offer the PR comment. Use when the user asks to test a Trio, TandemKit or other submodule PR "in CI", "on GitHub Actions", "on the workers", "via the workflow", or wants an e2e run without tying up this Mac's simulator or phone. Never from inside a harness job.
---

# trio-test-ci — the e2e test on GitHub Actions

**Never from inside a harness job.** This skill starts a job; a job must not start another one. `trio-test-ci` refuses
when `TH_JOB_AGENT=1` (a job's agent, in CI or the web UI) or `GITHUB_ACTIONS=true`; do not work around that with
`gh workflow run`. This skill lives in `host-skills/`, not `.claude/skills/`, so job agents don't load it.

## Local or CI?

| | CI (this skill) | Local (`trio-test-e2e` skill) |
|---|---|---|
| Where | the self-hosted macOS workers | this Mac's simulator or test iPhone |
| Devices | iOS Simulator, optionally a paired watch simulator | simulator, jailbroken iPhone (rooted-vnc), any iPhone (iphone-usb) |
| Who tests | the workflow's sandboxed agent, from `--instructions` | you, interactively |
| Ties up this Mac | no | yes (builds take 30–60 min) |

Pick CI when the user asks for it, or for a simulator run they don't need to watch. Pick local for a real iPhone, or
when they want to steer the testing.

## 1. Trigger

```
cd ~/repos/trio-test-harness
./trio-test-ci <Trio PR|owner/repo#N|branch> [-s <submodule PR|NAME@branch>]... [options] --no-follow
```

| Request | Flag |
|---|---|
| the paired Apple Watch too | `--devices phone+watch` (`auto` lets a small model decide); `--watch-model 40mm` … `49mm` |
| Pump Simulator + simulated CGM | default (`--no-smoke` stops at the home screen) |
| Tandem against faketandem | `--pump faketandem [--pump-setup walk] [--pump-scenario recent-doses]` (trio-fake-devices skill) |
| Loop instead of Trio | `--app loop` |
| walk onboarding | `--walk-onboarding` |
| what to test | `--instructions "…"`: numbered flows with the expected result of each, what to screenshot, and which phone log lines prove it (`logs trio --grep …`). The agent sees only this and the PR. |
| enough time | `--agent-minutes N` (default 30; add the waits a flow needs, e.g. a 2 min timeout ×2) |
| model | `--model opus` (default sonnet) |
| run a harness branch's workflow | `--ref <branch>` (to try a harness change before merging it) |

Leave `--comment` off unless the user already said to post (see step 4). The flags map 1:1 to the workflow's inputs
(`.github/workflows/e2e.yml`); `./trio-test-ci --help` lists them.

## 2. Follow

The run takes 30–90 minutes. Do not poll. Run `./trio-test-ci --attach <run id> --compact` (or
`gh run watch <id> -R jwoglom/trio-test-harness --exit-status`) in the background and continue when it ends.
`gh run view <id> -R jwoglom/trio-test-harness --json jobs` lists each step's conclusion.

## 3. Read the result

```
mkdir -p <scratch>/ci-<id> && cd <scratch>/ci-<id>
gh run download <id> -R jwoglom/trio-test-harness -n trio-e2e-<id>     # agent-summary.md, report.html, screenshots/, steps.jsonl, pr.md
gh run view <id> -R jwoglom/trio-test-harness --log > job.log          # the agent's tool calls, if something went wrong
```
Read `agent-summary.md`, then look at the screenshots yourself before believing it. A job can succeed while the agent
finished `partial` or `failed`: the verdict is in the summary, not the job's colour. If the agent was blocked by the
harness (not by the PR), say so and fix or report the harness, not the PR. Publish `report.html` as an Artifact.

## 4. PR comment

Ask the user first (AskUserQuestion). Then post that run's report without re-testing:
`gh workflow run e2e.yml -R jwoglom/trio-test-harness -f trio=<PR> -f comment_on_run=<id> -f comment_body="$(cat comment.md)"`,
or re-run with `--comment` if they want the workflow's own comment.

## 5. Summarize

Verdict, the commit tested, the run link, what was exercised, what was not and why, the artifact link.
