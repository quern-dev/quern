# Workflow: Multi-Device Testing

Testing your app across multiple devices simultaneously — different screen sizes, OS versions, or even iOS and Android side by side. Your agent handles the orchestration.

## Boot the Fleet

> "Boot three iPhone simulators: an iPhone SE, an iPhone 16, and an iPhone 16 Pro Max"

or more broadly:

> "Make sure I have three iOS 18 simulators running"

Your agent uses the device pool to find or boot matching devices. It picks the best candidates — already-booted devices first, then boots what's missing.

## Build Once, Install Everywhere

> "Build my app and install it on all three simulators"

Quern builds once for the simulator architecture, then installs in parallel across all targets. Three devices doesn't mean three builds.

For mixed physical + simulator testing:

> "Install on the two simulators and my iPhone"

Quern builds twice (one simulator build, one device build), then installs all three in parallel.

## Run a Scenario Across All Devices

> "On each device, launch the app, navigate to the profile screen, and tell me what you see"

Your agent works through each device, performing the same actions and reporting the results. You'll catch:

- **Layout issues**: Does the SE truncate text that looks fine on the Pro Max?
- **OS version differences**: Does the older device handle a deprecated API differently?
- **Performance differences**: Is the list smooth on the Pro Max but janky on the SE?

## Compare Network Behavior

If proxy capture is active on all simulators:

> "Show me the network traffic from each simulator side by side — are they all making the same API calls?"

Each simulator's traffic is automatically tagged with its UDID, so your agent can compare them. Useful for catching:

- Different behavior based on screen size (loading different image resolutions?)
- Race conditions that only appear on slower devices
- Unexpected extra API calls on specific configurations

## Save Known-Good State

Once you have all three devices in a state you want to test from:

> "Save the app state on all three simulators as 'pre-test-baseline'"

Now you can restore all three to this baseline instantly before each test run.

## Practical Example: Testing a New Feature

1. **"Boot an iPhone SE and an iPhone 16 Pro Max"**
2. **"Build and install my app on both"**
3. **"Set up proxy capture for both simulators"**
4. **"On both: launch the app, log in as testuser, navigate to the new Feed screen"**
5. **"Compare: does the Feed look correct on both screen sizes?"**
6. **"Compare: are the API calls identical on both?"**
7. **"Mock the feed endpoint to return an empty list. Does both devices show the empty state correctly?"**
8. **"Mock it as a 500. Do both show the error state?"**

In about 10 minutes, you've tested the happy path, empty state, and error state across two screen sizes — with network verification at each step.

## Tips

- **Start with 2-3 devices, not 10.** Each device adds context for your agent to manage. Start small and scale up once the workflow is smooth.
- **Name your simulators distinctively.** "iPhone 16 - Test A" and "iPhone 16 - Test B" are easier for both you and the agent to track than two devices with the same default name.
- **Use checkpoints liberally.** Save state before and after key transitions. It's cheap and makes re-running specific parts of a test much faster.
- **Physical + simulator mixing works**, but remember that physical devices need USB, WDA, and potentially proxy configuration — more setup than simulators.
