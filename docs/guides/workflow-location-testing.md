# Workflow: Location Simulation

Simulators (and Android emulators) let you set the device's GPS location programmatically. Quern exposes this, which means your agent can move a device along a road, simulate a delivery route, or coordinate two devices seeing each other's locations in real time.

## Basic Location Setting

> "Set the simulator's location to the Golden Gate Bridge"

Your agent sets the GPS coordinates. Your app sees a location update as if the phone physically moved there. No mock CLLocationManager needed — this works at the OS level, so any app on the simulator sees the new location.

## Walking a Route

> "Move the simulator along the Embarcadero from the Ferry Building to AT&T Park, updating the location every 2 seconds"

Your agent generates a series of coordinates along the route and steps through them. Your app sees smooth location updates — perfect for testing:

- Map views and annotation positioning
- Distance calculations and ETA updates
- Geofence triggers (enter/exit notifications)
- Location-based content loading
- Fitness tracking and route recording

## Two-Device Coordination

This is where it gets interesting. Think of any app where two users see each other's locations:

- **Rideshare**: Driver app and rider app, both watching the driver approach
- **Delivery**: Courier and customer, tracking a package in transit
- **Social**: Two friends sharing live location
- **Fleet management**: Dispatch watching multiple vehicles

### The Setup

> "Boot two simulators. On the first one, install the driver app. On the second, install the rider app. Log in as driver on the first, rider on the second."

### The Simulation

> "Move the driver simulator along Market Street toward the rider's location, updating every second. Watch both screens — does the rider app show the driver approaching?"

Your agent:
1. Sets the driver simulator's location to the route start
2. Steps through coordinates toward the pickup point
3. After each update, checks both screens — the driver's map and the rider's "your driver is approaching" view
4. Verifies that the rider app updates in near-real-time
5. Optionally captures the network traffic to verify the location-sharing API calls

### With Network Verification

> "While the driver is moving, show me the API calls both apps are making. Is the driver posting location updates? Is the rider polling or using a WebSocket?"

The proxy captures traffic from both simulators independently (tagged by simulator UDID). You can see exactly how the location-sharing protocol works: update frequency, payload size, latency between the driver's location change and the rider's UI update.

## Geofence Testing

> "Set up a geofence test: place the simulator outside the delivery zone, then move it across the boundary. Does the app trigger the 'entering zone' notification?"

Your agent can walk the location right up to a geofence boundary, step across it, and verify that the app responds correctly. Then walk it back out and verify the exit trigger.

## Turn It Into a Script

Following the [test scripts workflow](workflow-test-scripts.md), ask your agent to encode a location test as a reusable script:

> "Write a pytest script that simulates a driver completing a delivery: start at the restaurant, move along the route to the customer, and verify that each stage (picked up, en route, arriving, delivered) triggers the correct UI state and API calls on both the driver and customer apps."

Run it on every build. Your two-device location coordination is now a regression test.

## Tips

- **Use real coordinates.** Don't just make up latitude/longitude — use actual road coordinates from your target areas. Apps that snap to roads or use geocoding will behave differently with realistic vs random coordinates.
- **Match realistic update intervals.** GPS updates at roughly 1Hz while moving. Setting locations faster than that may trigger unexpected behavior in apps that debounce or throttle location updates.
- **Test the edges.** Poor GPS areas (tunnels, urban canyons), sudden jumps (user gets on a subway, location jumps 3 miles), and location permission revocation are all valuable test cases.
- **Combine with mocking.** Mock your geocoding API to return specific addresses, or mock your routing API to return a specific path. Control both the location input and the server response for fully deterministic tests.
