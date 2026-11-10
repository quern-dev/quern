# Auditing an Existing Knowledge Base

*`quern://app-knowledge-guide/auditing` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

A knowledge base written before landmark conventions were versioned declares nothing, and is read as v1: it loads and matches as before, including across backends. To bring one up to date:

1. `load_landmarks` it and read the `conventions` block: counts for every file, and each file not reported `current` with the landmarks at fault and why.
2. Fix the findings — usually an identifier on a type-only or generic landmark, or a landmark moved off a `navigationBar` onto the screen title.
3. Add `landmark_conventions: 2` to each file you have fixed, and reload until it reports `current`.
4. Where you can, identify the live screen twice — on the default backend, then after `start_driver` — to confirm the landmarks hold on both.

`grep -L "landmark_conventions: 2" .quern/knowledge/screens/*.md` lists the files still to migrate without loading anything. The full reference is [Auditing a knowledge base](https://github.com/quern-dev/quern/blob/main/docs/screen-landmarks.md#auditing-a-knowledge-base).
