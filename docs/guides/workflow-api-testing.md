# Workflow: Testing a New API Integration

You've added a new feature — say a "Load Profile" button that calls `GET /api/v1/users/me` and displays the result. Your code is written, the UI is built. Now you want to make sure it actually works, handles errors gracefully, and doesn't do anything unexpected on the network.

Here's how to do that with your AI agent and Quern, step by step.

## 1. Verify the Happy Path

Start by making sure the basic flow works:

> "Build and install my app on the simulator, then tap the Load Profile button and show me what happened on the network"

Your agent:
- Builds and installs your app
- Navigates to the screen with the button
- Taps it
- Waits a moment for the network call to complete
- Shows you the captured request and response

You immediately see:
- **Did the right endpoint get called?** Maybe you typo'd the path, or it's hitting staging instead of dev.
- **Are the headers correct?** Auth token present? Content-Type right?
- **What did the response look like?** Does the JSON structure match what your code expects?
- **Any unexpected extra calls?** Analytics SDKs, token refreshes, duplicate requests from a retain cycle?

This is already more verification than most developers do manually. And it took one sentence.

## 2. Verify the UI Updated Correctly

> "What does the screen show now? Does it display the user's name and email from the response?"

Your agent reads the screen's accessibility tree and tells you what's displayed. It can cross-reference the network response body with what appeared on screen — did the data actually make it from JSON to UI?

## 3. Test Error Handling

This is where it gets powerful. Your server returns 200 in development, but what about when it doesn't?

### 401 Unauthorized

> "Mock the /users/me endpoint to return a 401, then tap Load Profile again. What happens?"

Your agent sets up the mock, triggers the action, and observes the result. Does your app:
- Show a login prompt?
- Clear the stale token?
- Display a user-friendly error?
- Crash?

### 500 Internal Server Error

> "Now mock it as a 500 with body {'error': 'database unavailable'}. Tap the button and tell me what the app shows."

### Network Timeout / No Response

> "Intercept the /users/me request and hold it for 15 seconds before releasing. Does the app show a loading indicator? Does it time out gracefully?"

### Empty Response

> "Mock it as a 200 with an empty body. Does the app handle that without crashing?"

### Malformed JSON

> "Mock it as a 200 with body 'not json at all'. What happens?"

### Rate Limiting

> "Mock it as a 429 with a Retry-After: 30 header. Does the app respect it?"

## 4. Sweep Through Status Codes

Once you've tested the critical cases individually, you can ask your agent to be systematic:

> "Test how the app handles each of these responses from /users/me: 400, 401, 403, 404, 429, 500, 502, 503. For each one, mock the response, tap the button, and tell me what the app displays."

Your agent works through each one, reporting what the UI shows for every case. In a few minutes, you have a complete picture of your error handling coverage — something that would take an hour of manual testing with tools like Charles or Postman.

## 5. Check the Logs Too

For any case that looks wrong:

> "Show me the app logs around the time of that 500 response. Did it log the error correctly?"

Your agent cross-references the network flow timestamp with the log entries. You can verify that your error logging is working, not just your error UI.

## 6. Clean Up

> "Clear all the mocks"

Done. Your app is back to talking to the real server.

## Why This Works So Well

Traditional API testing workflow:
1. Open Charles/Proxyman, configure it
2. Set up a breakpoint or map-local rule
3. Trigger the action in the app
4. Switch to the proxy tool, find the request, inspect it
5. Modify the mock, repeat
6. Manually record what you observed
7. Repeat for each error case

With Quern + an AI agent:
1. Tell the agent what to test
2. Read the results

The agent does the tool-switching, the request matching, the observation, and the reporting. You focus on *what* to test and *whether the behavior is correct*.

## Variations

### POST/PUT Requests

Same pattern, but you can also verify the request body:

> "Tap Save Profile and show me exactly what JSON was sent to the server"

Your agent shows you the outbound request body. Did your code serialize the model correctly? Are there extra fields? Missing fields? Null where there should be a value?

### Pagination

> "Mock /users to return a page of 3 users with a next_page token. Tap Load More and show me if the app requests the second page with the right cursor."

### Authentication Flows

> "Watch the network traffic while I log in. Show me the full auth flow — every request in order."

Your agent captures the OAuth dance, token exchange, refresh flow — whatever your auth system does. Useful for verifying that tokens are stored correctly, refresh logic works, and logout actually clears everything.

### Recording Mock Fixtures with Capture Sessions

> "Record the API calls for each deep link so we can mock them in tests"

Your agent uses `start_capture_session` to bracket each UI action, automatically filtering out analytics and SDK traffic. The captured flows become mock fixtures — the exact request/response pairs needed to make each screen work in isolation. This pattern turns live API exploration into reproducible test infrastructure.

### Third-Party SDK Traffic

> "What network calls is my app making that aren't to our API?"

You might be surprised. Analytics, crash reporting, ad SDKs, feature flag services — all visible in the proxy. Good for auditing what data leaves your app.
