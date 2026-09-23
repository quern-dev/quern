# Proposal: Telling an agent its knowledge base is missing, incomplete or wrong

**Status:** proposal · **Raised:** 2026-09-23 · **Prompted by:** #274

## The problem

A knowledge base fails quietly in three different ways, and all three look the
same from inside a session: nothing happens differently.

1. **Missing.** No knowledge base at all. Every screen is unidentified, and
   every feature built on identification silently does nothing.
2. **Incomplete.** A screen the agent is standing on is not recorded, or is
   recorded without the field being asked for.
3. **Malformed.** A file loaded, and one of its fields does not say what its
   author wrote.

The third is the one that prompted this. `scrollable: "true"` is a string, and
anything that is not a literal boolean is read as "nobody has said" — a typo
must never be read as consent to swipe someone's screen. That coercion is
right and it is invisible: the file loads, `validate_landmarks` reports no
collisions, and the author believes autoscroll is on for a screen where it will
never fire.

## What is silently discarded today

Four, in one parser, all deliberate and all unreported:

| `server/device/landmarks.py` | what is dropped |
|---|---|
| `scrollable` coercion | a non-boolean value, read as unset |
| landmark entry not a dict | the entry, `continue` |
| landmark fails model validation | the entry, `continue` — naming neither an element nor a URL |
| `web_content` malformed | the hint, deliberately: "a bad hint must never stop a knowledge base loading" |

Each rule is correct on its own. The second and third matter most: a screen
written with three landmarks can load with **two**, and then identifies on
weaker evidence than its author intended — which is a collision risk they
cannot see, on the mechanism everything else is built on.

`skipped` already reports files that produced *nothing*. There is no channel
for a file that loaded and is wrong, and `list_landmarks` returns only counts.

## Why this is worth more than a validation report

The obvious shape is a `warnings` array on `validate_landmarks`, and that was
built and reverted while writing this — it is necessary and not sufficient. It
only helps an agent that thinks to ask, and the agent that most needs telling
is the one that does not know a knowledge base is involved at all.

The more valuable version is a **nag**: the moment quern cannot answer a
question *because* the knowledge base is missing, incomplete or malformed, the
response says so and names the fix. Some of this already exists and can be the
model for the rest — `tap_element` returns:

    "scroll": {
      "attempted": false,
      "reason": "scrollability_unknown",
      "detail": "no sweep was attempted because nothing records whether this
                 screen scrolls. If the element may be off-screen, retry with
                 scroll_to_find=true; to make that automatic, add
                 `scrollable: true` to the screen's knowledge-base entry."
    }

That is the pattern: the fact, the immediate workaround, and the durable fix,
delivered at the moment the gap bites rather than in a report nobody ran.

## Where a nag belongs, and where it does not

**Where it earns its place:** a request quern could have answered better with
knowledge it does not have. Identification returning `none` when landmarks are
loaded. A screen matching no known screen. A field consulted and absent.

**Where it does not:** everywhere else. A nag on every response is noise, and
noise is how the one that matters gets missed — the same reason `tap_element`
omits `scroll` entirely when no sweep ran rather than reporting an empty one.
An installation that has deliberately never adopted the knowledge base must not
be told so on every call.

That line is the design work. A plausible rule: nag when a *specific* question
went unanswered, never merely because the knowledge base is empty.

## Sketch

- **`warnings` on `validate_landmarks`** — the four discards above, each naming
  the file, the screen, the field and the value. This is the reporting half and
  the cheap half.
- **An `unknown_screen` signal** where identification runs and matches nothing,
  carrying what *was* on screen so the entry can be written from the response.
  Related to `docs/proposals/kb-drift-measurement.md`, which needs the same
  observation for a different purpose.
- **A "no knowledge base" note at most once per session**, on the first request
  that would have used one — not on every call.

## Open questions

- **What distinguishes "this installation has no knowledge base" from "this
  app's knowledge base is missing a screen"?** The first is a setup state and
  probably deserves silence after the first mention; the second is actionable
  every time.
- **Does a nag belong in the action response, or in a separate channel** an
  agent can consult when it chooses? The response is what gets read; a channel
  is what avoids noise. The `scroll` object suggests the response wins when the
  gap actually blocked something.
- **Who writes the fix?** A response naming the exact YAML to add invites an
  agent to write it, and `kb-drift-measurement.md` argues observation must
  never write to the knowledge base unprompted. Naming the edit and making it
  are different acts, and the line between them needs stating.
- **Is `validate_landmarks` the right home for the reporting half at all?** It
  is documented as collision detection, and its MCP tool took a path argument
  that never worked until #277 fixed the transport — so it may be less used
  than its existence suggests.
