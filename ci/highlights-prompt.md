You are reviewing a finished, automated test run of the Trio iOS app on the iOS Simulator. You did not run the
test and cannot run anything; you can only read files. Nobody can answer questions.

The run is in `{run_dir}`:
- `pr.md`: what is being tested (PR title, description, submodule info).
- `steps.jsonl`: every step of the run, one JSON object per line: `action`, `note`, and `screenshot` (a path relative
  to the run directory) when one was taken. Notes without a screenshot are the tester's observations.
- `agent-summary.md` (may be missing): the tester's own findings. Treat them as claims to check, not as facts.
{extra}
Task:
1. From `pr.md`, the instructions above, and the steps, work out what this run was supposed to demonstrate.
2. Choose only the screenshots that directly show the thing being tested: the changed screens and the states the
   instructions asked for. Leave out everything else, including the Trio home screen, screens passed through on the
   way to a setting (menus, lists, back navigation), and routine setup (launch, onboarding, adding the fake CGM/pump),
   unless that screen is itself what is being tested. Use as many as the change needs and no more: if the PR fixes
   one thing and one screenshot shows it, pick that one. Never add screenshots to reach a number; more are only
   justified by more distinct things to show, and never more than 10. Open each candidate with the Read tool, and do not pick one you have
   not looked at.
3. Describe what those screenshots actually show, and say whether they show the change working as the PR and the
   instructions expect. Base this only on what you see in the images. Where the images contradict, or do not
   support, a claim in the notes or `agent-summary.md`, say so. If the evidence is thin, say that too.

The app only ever shows simulated data (Glucose Simulator, Pump Simulator); do not treat values as health data.

Reply with ONLY a JSON object, no code fence and no other text:
{{"verdict": "works" | "broken" | "inconclusive",
 "summary": "markdown, 1 to 3 short paragraphs; refer to screenshots by their caption, not by file name",
 "highlights": [{{"screenshot": "screenshots/<file>.png", "caption": "one sentence: what this screenshot shows"}}]}}
