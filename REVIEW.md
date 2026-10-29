# Adversarial review for Truss pull requests

Every Truss PR gets an adversarial review before Sidd is asked to merge. The reviewer's
job is to break the PR, not to approve it: assume there is a bug that would hurt the
person wearing the glasses, and look for it. A PR goes to Sidd only with a verdict of
MERGE, and every finding below the "blocker" line either fixed or written down as
accepted.

The procedure is in [How to run a review](#how-to-run-a-review). The checklist is the
part that does not change from PR to PR.

## Verdicts

- **MERGE**: no blockers; tests pass on the PR head and on the merge result with its base.
- **MERGE AFTER**: mergeable once a named, small condition is met (a stacked base merges
  first, a rebase, one fix). The condition is stated in one line.
- **DO NOT MERGE**: at least one blocker.

Severity of a finding:

- **Blocker**: breaks a rule in section 1 or 2, a failing test, a merge that silently
  reverts code on main, or data loss.
- **Should fix**: a real bug off the safety path, or a missing test for new behavior.
- **Note**: style, naming, follow-up work. Never holds a merge.

## 1. Safety posture (blockers)

The failure designed against is a wrong all-clear, above all on the stove and the pills.

- [ ] **No wrong all-clear.** Any path from model output to a spoken "yes / it's off /
      you took them" must require an explicit, validated affirmative. Malformed JSON,
      a timeout, an empty reason, an unknown verdict string, a missing reference look,
      or an exception becomes `cannot_tell` (or an error), never `yes` or `no`.
- [ ] **Cannot tell instead of guessing.** `cannot_tell` is a first-class verdict all the
      way to the spoken line, with `missing` naming what would decide it.
- [ ] **Description over verdict.** Spoken lines say what was seen ("the knobs point to
      off"), not a safety judgment ("the stove is safe"). The guide verifies only
      presence, absence, count, position, order.
- [ ] **Evidence always kept with the answer.** Every fired answer has its evidence photo
      on disk before the answer is spoken or logged; non-firing frames are deleted.
      No path deletes evidence for a fired answer.
- [ ] **Missed-window alerts never instruct the act.** For safety questions an alert
      directs a state check ("check the Tuesday PM compartment before you take
      anything"), never "take your pills" / "turn off" / "feed" / "lock". A missed
      witness is not proof the act was missed (the glasses may have been off).
- [ ] **Coverage honesty.** Wording does not claim Truss was watching when frames did not
      arrive.

## 2. Project rules (blockers)

- [ ] **Meta models only in the demo path.** The default checker is the local Muse
      Glimmer 30B `llama-server` endpoint (or Muse Spark 1.3 over the Meta Model API).
      No non-Meta model (DeepSeek, GPT, Claude, Gemini, Aman's model) can be reached
      from a default config. Aman's bridge only behind an explicit opt-in, never default.
- [ ] **Never the contributor tier.** `muse-spark-1.3-contributor` (or any model name
      containing `contributor`) is refused, not just undocumented. Frames are never sent
      to a trained-on tier.
- [ ] **Every answer-log line names the model.**
- [ ] **Laptop server is Python standard library only.** No new imports outside stdlib
      in `truss-server/` (no `requests`, `openai`, `PIL`, `numpy`, `yaml`). Image work
      that needs a decoder belongs on the phone. (`eval/` may use third-party packages.)
- [ ] **Plain files, no database, no cloud account.** Question store stays one folder per
      question. Writes that a crash could tear are atomic (temp file + `os.replace`),
      and concurrent writers use the store's lock.
- [ ] **Naming.** "the DGX" for Sidd's machine, "Muse Spark" for Meta's model family.
      The word "Spark" never appears alone in code, comments, docs, or spoken lines.
- [ ] **No secrets.** API keys come from the environment and never reach logs, error
      messages, answer lines, or exceptions.
- [ ] **Do-not-build list respected**: no SAM gate before app sampling exists, no
      photo-a-second design, no contributor tier, no Aman in the demo loop.

## 3. Done-question semantics (blockers when touched)

- [ ] **Latch.** The witness is the first sighting of the yes-look inside the window,
      written once, atomically. A later sighting never overwrites the witnessed time.
- [ ] **Window edges.** Opening and closing times are inclusive/exclusive as documented;
      windows crossing midnight work; the DST change (2 AM Nov 1, repeated 1 AM hour)
      does not double-fire, skip, or misdate a sweep.
- [ ] **Redo reminder fires once** per window, on a return to the scene, and names the
      witnessed time.
- [ ] **Missed-window alert persists until delivered**: survives a restart, is cleared
      only when the phone confirms it was spoken, and is not created twice for one window.
- [ ] **Monotonic class only** in demo questions (pill organizer, deadbolt). Nothing
      treats a non-monotonic trace (a dog bowl) as a reliable latch.

## 4. Correctness and integration

- [ ] **Run every suite** in `truss-server/` on the PR head:
      `cd truss-server && for t in test_*.py; do python3 "$t" || echo "FAIL $t"; done`.
      Each prints `ALL PASS`. A failure is a blocker even if "unrelated".
- [ ] **Simulate the merge.** Merge the PR into its base (and, for a stacked PR, into
      `main` with its parents) and re-run the suites. Report conflicts by file.
- [ ] **Stale-copy reverts.** Diff every touched file against `main`, not just against
      the PR's base. A branch made from an old copy can silently undo merged work
      (this happened once with `server.py` plant and bedtime handling).
- [ ] **Interfaces stay stable.** `endpoint.ask_json`, `verdict.py` modes, the
      `prompts.txt` / `looks.txt` formats, and the HTTP routes the app calls do not
      change without every caller changing with them.
- [ ] **App and server agree.** If a route, field, or JSON shape changed, the Android
      client (`LaptopClient.kt`, `TrussViewModel.kt`) changed to match. Android compile
      is Sidd's gate on his Mac; say so if the PR touches `app/`.
- [ ] **Camera and Bluetooth audio are never open at the same time** in app changes.
- [ ] **Failure paths tested**: timeouts, unreachable endpoint, malformed model output,
      missing reference photos, empty or corrupt question folders.

## 5. Scope

- [ ] The PR does what its work item in the brief says and its acceptance criterion is
      demonstrated (test, or a stated live run still owed).
- [ ] No unrelated changes riding along.

## How to run a review

1. Fetch the PR head and its base. Read the PR body and the work item it claims.
2. Run section 4's suites on the head, then simulate the merge and run them again.
3. Read the full diff against `main` with this checklist open. Try to construct a
   concrete input that produces a wrong all-clear, a lost alert, a non-Meta call, or a
   reverted file. Every finding names a file and line and gives the failing scenario.
4. Post one PR comment headed `Adversarial review` with the verdict on the first line,
   then blockers, should-fix, notes, and the test results (suite name, pass/fail).
   No tool attribution, footers, or internal links in the comment.
5. When the author pushes fixes, review the delta the same way (a "delta round"),
   and post a new verdict. Repeat until MERGE.
6. Only then ask Sidd to merge, with the verdict and the merge order for stacked PRs.
   Squash-merge with a clean one-line message (no trailer lines).
