# Workflow: Onboarding onto a Project

You've just joined a team, cloned the repo, and need to get productive fast. Here's how Quern and your AI agent can compress what usually takes a day of setup and exploration into an hour.

## Get the App Running

Before anything else, get the app on a screen:

> "Boot a simulator, build this project, and install the app"

Your agent discovers the Xcode project, figures out the scheme, builds, and installs. If there are build errors, it parses them and helps you fix missing dependencies, signing issues, or environment problems.

> "Launch the app and take a screenshot so I can see it"

Now you're looking at the app. You haven't opened Xcode yet.

## Explore the App

Let your agent drive while you observe:

> "Walk me through the main screens of this app — take a screenshot of each tab"

Your agent taps through tab bars, navigation items, and major screens, capturing screenshots as it goes. In a few minutes, you have a visual tour of the app without reading any code or documentation.

> "What are all the buttons and interactive elements on this screen?"

The accessibility tree tells your agent everything that's on screen — buttons, labels, text fields, switches. It's like a UI inventory.

## Understand the API Surface

Set up proxy capture and explore:

> "Set up network capture, then go through the main screens of the app. Show me a summary of all the API calls."

Your agent navigates the app while the proxy captures every network request. The summary groups traffic by host and endpoint. In one pass, you know:

- What backend services the app talks to
- Which endpoints each screen uses
- Whether there are third-party SDKs making network calls
- What the authentication flow looks like

> "Show me the full request and response for the login call"

Now you can see the exact API contract without reading networking code or finding API documentation.

## Understand the Data Flow

For a specific feature you'll be working on:

> "Navigate to the Orders screen and show me what API calls happen, then show me what data the app displays. Does the UI match what came from the API?"

Your agent cross-references the network response with the screen content. You can see how JSON fields map to UI elements — the data flow from API to screen, verified live.

## Save a Starting Point

Once the app is in a useful state:

> "Save the app state as 'onboarding-baseline'"

Now you can always get back here. Broke something while exploring? Restore in seconds.

## Read the Code with Context

Now that you've *seen* the app running, reading the code is much more productive. You know what the screens look like, what APIs they call, and how data flows. Ask your agent:

> "I see the profile screen calls GET /api/v1/users/me. Show me the code that makes this request and handles the response."

Your agent can connect the network behavior you observed to the actual source code. Much more grounded than reading code cold.

## Set Up for Development

Before you start making changes:

> "Save the current state as 'pre-changes' so I can get back to it"

Then as you work:

> "Build and install my changes, then navigate to the screen I modified. Does it look right? Check the network traffic too — is the new endpoint being called correctly?"

## Tips

- **Don't read the README first.** Seriously — let the agent build and run the app. The README might be outdated. The app's actual behavior is always current.
- **Capture the API surface early.** A proxy capture session during a full app walkthrough gives you more accurate API documentation than most wikis.
- **Save states at meaningful points.** "Logged in as admin," "empty database," "mid-checkout" — these become your development workbench.
- **Ask the agent to explain unfamiliar patterns.** "Why is this screen making three API calls instead of one?" is a great question when you're onboarding.
