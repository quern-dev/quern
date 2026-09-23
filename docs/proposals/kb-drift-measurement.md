# Proposal: Measuring where the knowledge base and reality diverge

**Status:** proposal · **Raised:** 2026-09-23 · **Prompted by:** #274

## The problem

The knowledge base is a maintained record of what an app's screens *are* and
how they connect. It is built once — ideally by an agent with a human who knows
the app correcting the wrong generalisations — and after that first pass, small
corrections are what keep it current.

Today those corrections require a person in the room. An agent hits an
inconsistency, and a human says *"no, that is an error screen, not the expected
screen — add it."* That works, and it does not scale: the knowledge base stays
correct exactly as long as someone is watching.

**The goal is not to replace that judgement with a learner.** It is to spend it
better. Observation cannot produce meaning — it cannot know that a screen is an
*error* screen, and that classification is most of what makes the knowledge
base worth having. What observation can do is produce *evidence about
structure*: which transitions actually happen, how often, and where the
recorded graph has stopped matching the app. That evidence should direct a
human's attention rather than substitute for it.

## The hard part is not confidence

The obvious framing is a confidence score: act automatically when sure, ask a
human when not. That framing misses where the difficulty is.

When an action lands somewhere other than the recorded destination, three
different things look identical from a single observation:

1. **The knowledge base is stale.** The app changed. The edge should be
   corrected.
2. **The knowledge base is incomplete.** A conditional path — a permission
   dialog, first-run onboarding, an error state. An alternative should be
   *added*. Correcting the edge here would be actively wrong.
3. **The app is broken right now.** A bug, bad test data, a dead network.
   Nothing should be recorded at all.

A learner that treats every surprise as (1) corrupts the graph, and it corrupts
it in the direction of encoding bugs as expected behaviour. This is the reason
a human is currently required: not because the judgement is hard, but because
one observation does not contain the information needed to make it.

What does separate them is cheap to capture, and it is not confidence:

- **(2) recurs intermittently**, and the recorded destination still happens
  too. Both outcomes are real.
- **(1) is monotonic.** After some point, only the new outcome occurs.
- **(3) does not recur**, or recurs only inside a single session or
  environment.

Frequency alone cannot tell them apart. Frequency across *distinct sessions,
over days* mostly can. That is the discriminator worth building on.

## An edge is a distribution, not a value

Most of the interruption disappears under one change of shape. Instead of

    Login --tap(Sign In)--> Map

record

    Login --tap(Sign In)--> { Map, ErrorBanner, Onboarding }   with counts

Landing on a known member of the set is corroboration, and nobody is asked
anything. The exchange that happens constantly today — *"that is the error
screen"* — stops being an event, because the error screen is already a recorded
outcome of that edge.

"Unexpected" then acquires a precise meaning: **an outcome not in the known
set.** That is a much rarer event, and a much more informative one.

## Provenance, not confidence, is what stops drift

Three grades of knowledge, and the rule between them matters more than any
threshold:

| grade | meaning | may be changed by |
|---|---|---|
| `declared` | a human wrote or approved it | a human |
| `observed` | accumulated evidence, promotable by review | a human, or accumulation |
| `candidate` | new, unpromoted | a human, or further evidence |

**Observation may add candidates and accumulate evidence against a declared
edge. It may never rewrite one.** That single rule is what keeps the graph from
wandering: a human's correction is sticky, and no amount of contrary
observation silently undoes it. Evidence against a declaration surfaces as a
question, never as an edit.

This is the same instinct the codebase already applies elsewhere — *a trust
record is not trust* (`is_cert_installed`), and the decision in #274 not to
write `scrollable: false` back to the knowledge base from the sweep's own
observation, because a screen with too little content to scroll is
indistinguishable from one that cannot.

## What still needs a human

After the above, two things, and only two:

- **An outcome matching no known screen.** It needs a name, and naming is not
  something observation can do.
- **Sustained contradiction of a declared edge.** "You recorded A→B; it has
  been A→C twelve times, across four days and six sessions."

Everything else accumulates quietly. The human moves from interrupt to review
queue — which is the shape quern already has for knowledge-base health:
`validate_landmarks` and `detect_collisions` report problems today without
fixing them.

## What cannot be automated, and should not be claimed

- **Naming a new screen.**
- **Deciding whether a new outcome is legitimate or a bug.** The system can say
  *"this is new, and it recurred across six sessions"*. It cannot say *"this is
  correct"*.
- **Semantic classification** — error versus expected, modal versus
  destination. This is the part of the knowledge base that carries meaning, and
  it is authored, not inferred.

## The constraint that reshapes this

**The measurement must be free.**

The departure screen costs nothing: `tap_element` already reads a tree to find
its target. The arrival screen does not — it needs a post-action read, roughly
1.8s on a simulator, and `include_screen_context` is opt-in and off by default.

A measurement that adds a read per action changes the behaviour it is
measuring, and slows every caller to gather data most of them will never look
at. So transitions are recorded only where a screen read is *already* being
paid for.

The consequence is a sampling bias toward flows that already ask for context,
which is to say toward careful ones. That is acceptable for a first number and
must be stated alongside it, rather than discovered later by someone reading it
as a whole-population rate.

## What to build first

Three steps, none of which requires anyone to author a graph.

1. **Auto-identify in `_capture_screen_context`.** Already specified in
   `docs/screen-identification-in-actions.md` and still unimplemented (~50–80
   lines plus tests). Valuable alone: after any action that returns screen
   context, the response says where you landed, with no agent burden. Every
   later step consumes it, and nothing can be observed until it exists.

2. **Record transitions where both ends are already known.** `(from, action,
   to, session, timestamp)`, appended, size-capped. A log, not a learner. It
   must not change what any action does or returns: if the recording is wrong,
   the cost is a bad number, never a bad tap.

3. **Score the dumbest possible predictor** — "the same outcome as last time
   for this `(from, action)`" — and report its miss rate.

## Why measure before designing the learner

Step 3 answers the question the whole design rests on, and which nobody
currently can: **are these distributions stable enough to learn from?**

- If last-outcome-wins is ~95% accurate, edges are effectively single-valued.
  The knowledge base can carry a destination per edge, and drift detection is
  the valuable part.
- If it is nearer 60%, edges are genuinely multi-modal. The distribution shape
  above is load-bearing, the review queue is the product, and a
  single-destination graph would have been wrong from the start.

Either answer redirects real effort. Designing the learner first produces a
clever system tuned to imagined behaviour — the failure this repository keeps
finding in other forms.

The miss rate is also a drift alarm on its own, before any learning exists: one
that sat at 5% and becomes 30% is the app changing underneath the knowledge
base, which is currently only noticed by someone being in the room.

## What this does not propose

- **Navigation recipes.** The server does not act on a prediction. The line
  drawn during the screen-landmarks review stands — *landmarks are assertions,
  recipes are actions* — and everything here is on the assertion side. See
  `docs/proposals/navigation-recipes.md`, which remains a separate decision.
- **Auto-correcting the knowledge base.** Nothing here writes to it.
- **Replacing authored knowledge with observation.** The knowledge base is the
  asset; observation is an instrument for maintaining it.

## Open questions

- **Where does session identity come from?** Distinguishing "six sessions" from
  "one session, six times" is load-bearing for the whole discriminator, and
  quern has no session concept yet. #254 (device sessions) may supply it; until
  then, server-process lifetime is a crude stand-in that would undercount.
- **Does an expectation help disambiguate identification?** #274 found that two
  apps loaded at once collide and identification goes ambiguous. An expected
  destination could break that tie — but only if corroboration is distinguished
  from assumption, or it becomes a confident wrong answer.
- **Where do the edges live in the file format?** Screens are one markdown file
  each with YAML frontmatter, and an edge belongs to a source screen, an action
  and a destination. `scrollable` (#274) is the first per-screen property
  beyond identity; transitions are a different shape and may not belong in the
  same place.
- **How is a recorded transition keyed to an action?** `tap_element(label="Sign
  In")` and `tap_element(identifier="_SignIn")` are the same user action and
  different call signatures.
