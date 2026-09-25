---
name: quern-worktrees
description: Naming, moving and cleaning up quern worktrees and mutation-test scratch copies -- the ~/Dev/quern-<session>-<what> convention, why the session prefix exists and when not to trust it, cross-device moves, and where scratch copies live. Use before creating, re-homing, sweeping or deleting a worktree or a git-archive copy of this repo.
---

# Naming a worktree

Moved out of `CONTRIBUTING.md` so it loads only when worktree work is under
way. This file is the only copy; the safety rules around worktrees (never
restore with `git checkout`, the primary checkout stays on `main`, the
half-success check on `git worktree remove`) stay in `CONTRIBUTING.md`.

```text
~/Dev/quern-<session>-<what>          <what> is the branch slug, or pr<N> for a review
~/Dev/quern-scratch/<session>-<what>/ mutation copies, which are not worktrees
```

`<session>` carries no hyphen of its own: `quern-dev69-...`, not
`quern-dev-69-...`, which reads as a double hyphen and leaves the field
ambiguous to parse. `ListAgents` reports the name *with* the hyphen, so this
needs saying or the two forms both get produced.

**The session prefix is the point, and its beneficiary is never the author.**
Nobody is unsure which trees are theirs; the prefix exists for whoever is doing
a sweep, who by definition is someone else. So "it reads fine without one" is
true of every tree taken individually and the aggregate is still unusable --
which is why it has to be a convention rather than each session's judgement.
The cost is a longer path for the owner; the benefit goes to everyone else.

That cost buys the removal of a specific, measured blockage. One session left
nine finished worktrees untouched because two were `locked` and they could not
tell whose sessions were live; another refused to delete five clean,
fully-merged trees for the same reason, and needed to be *told* one of them was
ours before removing it. Anonymous paths do not cause confusion, they make the
cautious choice the wrong one, and cleanup then never happens.

Git cannot answer it either: commits here carry no AI attribution, so every
commit is the same author. The path is the only place ownership can live.

**But the name is a creation-time hint, not a claim.** Worktrees outlive the
sessions that make them -- `quern-media-engine` was created by one session and
inherited by another -- and session names are not unique over time. Every one
seen here is `dev-` plus two hex digits (`dev-3a`, `dev-d6`, `dev-34`), which
is a space of 256: two sessions share a suffix at better than even odds by the
twentieth, and at 83% by the thirtieth. So a prefix can be confidently wrong
rather than merely stale, and by collision rather than by anyone reusing a
name deliberately. A stale prefix read as
current fact is this repo's "a trust record is not trust" rule arriving by a
new route. So:

- the **name** says whom to ask,
- `ListAgents` says whether that session is still there,
- `git log -1 --format='%cr' <branch>` (or the directory's mtime) says whether
  it is worth asking.

Never the name alone -- and note that a prefix match is a hint about *whom to
ask*, not proof of whose tree it is. The collision is concurrent as well as
historical: two live sessions can hold the same prefix at the same instant, so
a match may name either of them. `ListAgents` is built for this, appending a
`[ref]` when two rows share a name, which is as plain a statement as you could
want that the name alone does not identify anyone.

**Re-homing a tree you have adopted is explicitly
allowed** -- a stale prefix is not somebody's claim on a tree you are the one
using, and leaving it there to be polite is how it ages into the confusion the
prefix exists to remove.

**`<what>` is one field with two uses, not two rules.** A branch slug for
branch work, `pr<N>` for a review. Two naming rules would make a reader
classify a directory before they could parse it, and the classification is not
recoverable from the path. The *lifecycles* differ -- a review tree is
re-pointed with `checkout --detach` as the branch moves and is cleared when the
PR merges -- but that is not something the name has to carry.

**The number goes in at creation or never.** It is right when the issue
preceded the branch, which is common: #270 was filed, then branched, and the
number never churned. It is wrong to add later -- one branch here ran eight
days with no PR before becoming #164, so a path would have been absent, then
wrong, then correct -- and renaming breaks shell history for a handle people
already use. `gh pr list --head <branch>` derives it whenever it is wanted.
Numbers earn their place by matching how the work is actually discussed
("261, then 256"), not by saving a lookup.

**`~/Dev`, not `/tmp`.** Not because /tmp is being cleaned -- this machine has
no `periodic/daily/*clean-tmps` and eight days of uptime, and /tmp worktrees
here hold thousands of files intact. The reason is that its lifetime is not
ours to depend on, and the failure it would cause is a quiet one: a registered
worktree whose contents have gone still passes `worktree list` and `prune`, and
a suite run inside it reports on files that are no longer there. "103 tests
passed" from a tree missing half its suite is the house failure shape, arriving
as what looks like a git problem.

**Moving an existing worktree needs more than `git worktree move`.** That is
`rename(2)`, so it cannot cross filesystems -- and `/tmp` (`disk3s5` here) and
`~/Dev` (`disk7s1`) are different ones, which is precisely the migration this
section asks for:

```text
fatal: failed to move ... : Cross-device link
```

When the tree is clean and pushed -- check `git status --porcelain` is empty
and `git rev-list --count origin/<branch>..HEAD` is 0, because this discards
the tree rather than moving it:

```sh
git worktree remove --force "$src"
[ -d "$src" ] && rm -rf "$src"          # the half-success check, which does fire
git worktree add "$dst" "$branch"
```

With uncommitted work, `cp -r` and then `git worktree repair "$dst"` instead;
it preserves the tree. (Removals that left debris behind: twice in about ten.)

**Scratch copies need the parent directory, not a name.** Mutation testing
works from `git archive` extracts, which no worktree check will ever report --
so one `ls ~/Dev/quern-scratch/` is the only thing that covers the class. It
must be a sibling of the checkouts and never inside one, or a mutated copy
turns up in someone's `git status`. Delete them when the review ends: outside
/tmp they no longer expire on their own, and they run to ~12MB each.

A harness built on `mktemp -d` defeats this without meaning to: it lands in
`/var/folders`, so it is invisible to a sweep of `quern-scratch` *and* to one
of `/tmp` -- three places to look instead of one. Point it at the single place
with `TMPDIR=~/Dev/quern-scratch mktemp -d`.
