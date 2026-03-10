# Workflow: Investigating a Crash

A user reports a crash — maybe a TestFlight tester, maybe your own device, maybe a crash report from your analytics dashboard. Here's how to go from "it crashed" to "here's why" using your AI agent and Quern.

## 1. Get the Crash Report

If it happened on a physical device connected to your Mac:

> "Pull the latest crash report from my iPhone"

If it happened on a simulator:

> "Show me the latest crash"

Quern watches for simulator crashes automatically. For physical devices, it pulls crash reports on demand.

## 2. Understand the Crash

Your agent reads the crash report and gives you the essentials:

- **Exception type**: EXC_BAD_ACCESS (memory), SIGABRT (assertion/exception), SIGTRAP (Swift runtime trap)
- **Top stack frames**: Where it crashed and the call chain that led there
- **The faulting thread**: Which thread crashed and what it was doing

> "What does this crash mean? Walk me through the stack trace."

Your agent can explain the crash in context of your code — it knows your codebase and can connect the crashing function to the logic around it.

## 3. Cross-Reference with Logs

The crash tells you *what* happened. Logs tell you *why*.

> "Show me the app logs from the 10 seconds before this crash"

Your agent uses the crash timestamp to query the log buffer. You'll often see the breadcrumbs: a failed API call, a nil value logged, a state transition that shouldn't have happened.

## 4. Cross-Reference with Network Traffic

If the proxy was capturing traffic:

> "Were there any failed network requests around the time of the crash?"

A 500 response, a timeout, or a malformed JSON payload can trigger a crash if error handling is incomplete. Seeing the actual response body that preceded the crash often makes the root cause obvious.

## 5. Reproduce It

Now that you have a theory, reproduce it:

> "Can you get the app into a state where this would happen?"

If the crash was triggered by a bad API response, your agent can mock that exact response:

> "Mock the /users/me endpoint to return the same 500 response we saw in the crash logs, then navigate to the profile screen"

If it was a state issue, your agent can manipulate the app's state:

> "Set the cached_token key to an empty string in the app preferences, then launch the app"

## 6. Verify the Fix

After you fix the code:

> "Restore the app to the crash state and try again — does it still crash?"

If you saved the crash-triggering state as a checkpoint, restoration is instant. If you're using mocked responses, the mock is still active. One sentence to verify.

## The Full Loop

```
Crash report  →  "What crashed and why?"
    ↓
Logs          →  "What was the app doing before it crashed?"
    ↓
Network       →  "Did a bad response trigger this?"
    ↓
Reproduce     →  Mock the bad response / set the bad state
    ↓
Fix           →  Change code, rebuild
    ↓
Verify        →  Reproduce again — no crash
```

Each step is a sentence to your agent. The agent coordinates crash reports, log queries, network flows, mocking, state management, and UI automation — tools that would normally mean switching between 4-5 different apps.

## When You Don't Have Logs

If the crash happened in the wild and you don't have Quern logs from the original incident, you can still:

1. Read the crash report for the immediate cause
2. Look at your code to understand the conditions that would trigger it
3. Ask your agent to set up those conditions (mock responses, manipulate state)
4. Reproduce locally with full logging enabled
5. Now you have the logs you need
