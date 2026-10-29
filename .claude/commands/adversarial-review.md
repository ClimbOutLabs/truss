---
description: Adversarial review of a Truss PR against REVIEW.md, posted as a PR comment with a merge verdict
argument-hint: <PR number>
---

Run an adversarial review of pull request #$ARGUMENTS in ClimbOutLabs/truss.

1. Read `REVIEW.md` at the repo root. It is the checklist and the verdict scale.
2. Fetch the PR, its head branch and its base. If the base is not `main`, find the
   parent PRs in the stack.
3. Run every `truss-server/test_*.py` on the head. Then merge the head into its base
   (and into `main` together with any parent PRs, in merge order) in a scratch worktree
   and run them again. Record conflicts by file.
4. Diff the head against `main` and against its base. Go through every section of
   `REVIEW.md`. Your goal is to break the PR: construct concrete inputs that would cause
   a wrong all-clear, a lost or duplicated alert, a non-Meta or contributor-tier call, a
   non-stdlib import in `truss-server/`, a lone "Spark", a leaked key, or a silent revert
   of code already on `main`. Check each candidate finding against the code before
   reporting it; drop anything you cannot point to a line for.
5. Post one PR comment that starts with `Adversarial review: MERGE`, `MERGE AFTER <condition>`,
   or `DO NOT MERGE`, then Blockers, Should fix, Notes (each with file:line and the
   failing scenario), then the test results. End with the Claude Code footer.
6. Reply with the verdict and the one-line reason.
