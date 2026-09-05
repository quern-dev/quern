# Quern Debug Server — agent notes

@CONTRIBUTING.md

That file is the single source of truth for this project: architecture, layout,
code conventions, and the design decisions worth knowing. Read it before
changing anything here. Nothing from it is repeated below, deliberately — two
copies of a convention drift, and the copy an agent happens to load wins.

## What gets got wrong most often

Each of these is covered in `CONTRIBUTING.md`; they are listed here only because
they are the ones that actually cost time, not because they are more important.

- **Error paths, on the first pass.** See *Code conventions*. Nine consecutive
  review findings landed there and none touched feature logic, so budget for
  them up front rather than treating them as polish.
- **Merging a PR.** Use `scripts/merge-pr.sh <number>`. A raw `gh pr merge`
  skips the staleness check, and a "0 unresolved" reading taken before the
  latest push looks exactly like all-clear.
- **Commit messages.** No `Co-Authored-By`, no AI attribution. The `commit-msg`
  hook enforces it; enable hooks with
  `git config core.hooksPath scripts/git-hooks`.
- **Exit codes from the review scripts.** Don't pipe `pr-review-status.py` if
  you care about its status — `| sed` or `| tee` reports the pipe's exit code,
  not the script's, and that reads as success.
