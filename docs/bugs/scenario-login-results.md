# Geocaching Login Scenario — Network & Log Capture Results

**Date:** 2026-02-11
**Device:** iPhone 16 Pro (Simulator, iOS 18.6)
**UDID:** 45395D76-AF20-4CEF-8966-9B1C43BF9475
**App:** Geocaching (com.groundspeak.GeocachingIntro) — Staging environment
**Proxy:** mitmproxy on port 8080, capturing via ios-debug-server

---

## Scenario Steps

### Step 1: Launch App
**Action:** `simctl launch com.groundspeak.GeocachingIntro`
**Time:** ~02:53:42Z
**Screen:** Login/signup screen (Continue with Apple, Facebook, Google, Sign up, Log in)

#### API Routes (12 flows)

| # | Method | Status | Host | Path | Notes |
|---|--------|--------|------|------|-------|
| 1 | GET | 304 | clientservices.googleapis.com | /chrome-variations/seed | Chrome config (system) |
| 2 | GET | --- | build-macmini01 | /app/rest/ui/server | Internal build server (no response) |
| 3 | GET | 200 | ep2.facebook.com | /v17.0/222303401139936/server_domain_infos | Facebook SDK init |
| 4 | GET | 200 | ep2.facebook.com | /v17.0/222303401139936 | Facebook SDK — app config (feature bitmask, share mode) |
| 5 | GET | 200 | ep2.facebook.com | /v17.0/222303401139936/mobile_sdk_gk | Facebook SDK — gatekeepers |
| 6 | GET | 200 | ep2.facebook.com | /v17.0/222303401139936 | Facebook SDK — app events config |
| 7 | GET | 200 | ep2.facebook.com | /v17.0/222303401139936/model_asset | Facebook SDK — ML model assets |
| 8 | GET | 200 | ep2.facebook.com | /v17.0/222303401139936/ios_skadnetwork_conversion_config | Facebook SDK — SKAdNetwork config |
| 9 | POST | 200 | firebaseinappmessaging.googleapis.com | /v1/sdkServing/projects/144786287350/eligibleCampaigns:fetch | Firebase in-app messaging |
| 10 | GET | 200 | init.itunes.apple.com | /bag.xml | iTunes/App Store init (system) |
| 11 | POST | 200 | fcmtoken.googleapis.com | /register | Firebase Cloud Messaging token registration |
| 12 | POST | 200 | fcmtoken.googleapis.com | /register | Firebase Cloud Messaging token registration (2nd) |

#### Logs
No app-specific logs during cold launch to login screen.

---

### Step 2: Tap "Log in"
**Action:** `tap_element(label="Log in")` → identifier `_LogIn button` at (295.5, 740.33)
**Time:** ~02:55:50Z
**Screen:** Login form (Username, Password, Log in, Forgot something?, Terms of use links)

#### API Routes
None — local navigation only.

#### Logs
None.

---

### Steps 3–4: Enter Username
**Action:** `tap_element(identifier="_Username textField")` then `type_text("deptest18")`
**Time:** ~02:55:52Z

#### API Routes
None — local input only.

---

### Steps 5–6: Enter Password
**Action:** `tap_element(identifier="_Password textField")` then `type_text("deptest")`
**Time:** ~02:55:55Z

#### API Routes
None — local input only.

---

### Step 7: Submit Login
**Action:** `tap_element(identifier="_Log in button")` at (201.0, 307.33)
**Time:** ~02:55:59Z
**Screen:** Loading → "Be safe" onboarding with system "Save Password?" dialog overlay

#### API Routes (15 flows)

| # | Method | Status | Host | Path | Notes |
|---|--------|--------|------|------|-------|
| 13 | POST | 200 | staging.api.groundspeak.com | /mobile/v1/_users/login | **Auth: login request** (110ms, 1.0KB response) |
| 14 | GET | 200 | staging.api.groundspeak.com | /mobile/v1/iterable/token/{device_uuid} | Iterable push token registration (71ms) |
| 15 | GET | 200 | staging.api.groundspeak.com | /mobile/v1/user/settings/mobileexperiments | Feature flags / A-B test config (53ms) |
| 16 | POST | 200 | staging.api.groundspeak.com | /mobile/v1/user/flags | User feature flags sync (55ms) |
| 17 | POST | 200 | firebaseremoteconfig.googleapis.com | /v1/projects/geocaching-test-1/namespaces/firebase:fetch | Firebase Remote Config fetch |
| 18 | GET | 200 | staging.api.groundspeak.com | /mobile/v1/geocaches/unlocksettings | Geocache unlock/premium settings (77ms) |
| 19 | GET | 200 | staging.api.groundspeak.com | /mobile/v1/profileview | User profile data |
| 20 | GET | 304 | configuration.ls.apple.com | /config/defaults | Apple system config (cached) |
| 21 | GET | 200 | gspe1-ssl.ls.apple.com | /pep/gcc | Apple Maps — geo config |
| 22 | GET | 304 | gspe35-ssl.ls.apple.com | /geo_manifest/dynamic/config | Apple Maps — geo manifest (1st) |
| 23 | GET | 304 | gspe35-ssl.ls.apple.com | /geo_manifest/dynamic/config | Apple Maps — geo manifest (2nd) |
| 24 | POST | 200 | gsp-ssl.ls.apple.com | /dispatcher.arpc | Apple Maps — tile dispatcher |
| 25 | POST | 200 | gsp64-ssl.ls.apple.com | /hvr/v3/use | Apple Maps — vector tile rendering |
| 26 | GET | 200 | staging.api.groundspeak.com | /mobile/v1/friendrequests?skip=0 | Friend requests check |
| 27 | POST | 200 | gsp64-ssl.ls.apple.com | /hvr/v3/use | Apple Maps — vector tile rendering (2nd) |

#### Logs (Network)
```
POST /mobile/v1/_users/login -> 200 OK (110ms, 1.0KB)
GET  /mobile/v1/iterable/token/2d363d86-cdde-48ce-9adc-fedc196f80d9 -> 200 OK (71ms, 259B)
GET  /mobile/v1/user/settings/mobileexperiments -> 200 OK (53ms, 809B)
POST /mobile/v1/user/flags -> 200 OK (55ms, 941B)
POST /v1/projects/geocaching-test-1/namespaces/firebase:fetch -> 200 (63ms)
GET  /mobile/v1/geocaches/unlocksettings -> 200 OK (77ms, 233B)
GET  /mobile/v1/profileview -> 200 OK (90ms)
GET  /mobile/v1/friendrequests?skip=0 -> 200 OK
```

#### Logs (System)
```
[notice] chronod: Lazy refresh timer fired - pending descriptors to fetch:
    [com.groundspeak.GeocachingIntro::com.groundspeak.GeocachingIntro.GeocachingWidget]
[notice] chronod: scheduling query for GeocachingWidget
[notice] chronod: task submitted — standard refresh for GeocachingWidget
```

---

### Step 8: Dismiss "Save Password?" Dialog
**Action:** `tap(x=200, y=770)` — coordinate tap on "Not Now" (system dialog not accessible via idb)
**Time:** ~02:57:10Z
**Screen:** "Be safe" onboarding screen with "I understand" button

#### API Routes (1 flow)

| # | Method | Status | Host | Path | Notes |
|---|--------|--------|------|------|-------|
| 28 | PUT | 200 | news-edge.apple.com | /v1/configs | Apple News config (system background) |

#### Logs
None specific to this action.

---

### Step 9: Tap "I understand" (Onboarding)
**Action:** `tap_element(label="I understand")` → identifier `_Slides continue button` at (201.0, 784.0)
**Time:** ~02:58:11Z
**Screen:** Map view (429 map pins, tab bar, map controls)

#### API Routes (6 flows)

| # | Method | Status | Host | Path | Notes |
|---|--------|--------|------|------|-------|
| 29 | POST | 200 | gsp-ssl.ls.apple.com | /dispatcher.arpc | Apple Maps — tile dispatcher |
| 30 | HEAD | 302 | staging.geocaching.com | /account/documents/termsofuse?culture=en | Terms of use check (redirect) |
| 31 | HEAD | 200 | staging.geocaching.com | /policies/en/terms-of-use/ | Terms of use (followed redirect) |
| 32 | GET | 200 | staging.api.groundspeak.com | /mobile/v2/map/search?geocachesTake=300&latitude=47.654&longitude=-122.350&skip=0 | **Map: geocache search (1st page)** (983ms) |
| 33 | GET | 200 | staging.api.groundspeak.com | /mobile/v2/map/search?geocachesTake=300&latitude=47.654&longitude=-122.350&skip=0 | **Map: geocache search (2nd call)** (1020ms) |
| 34 | GET | 200 | staging.api.groundspeak.com | /mobile/v1/user/settings/communication/touEtag | Terms of use ETag check |

#### Logs (Network)
```
GET /mobile/v2/map/search?geocachesTake=300&latitude=47.654&longitude=-122.350&skip=0 -> 200 OK (983ms)
GET /mobile/v2/map/search?geocachesTake=300&latitude=47.654&longitude=-122.350&skip=0 -> 200 OK (1020ms)
GET /mobile/v1/user/settings/communication/touEtag -> 200 OK
HEAD /account/documents/termsofuse?culture=en -> 302
HEAD /policies/en/terms-of-use/ -> 200
```

---

## Unique API Routes Summary

### Geocaching API (staging.api.groundspeak.com) — 10 unique routes

| Method | Path | Step | Purpose |
|--------|------|------|---------|
| POST | /mobile/v1/_users/login | 7 (Login) | Authentication |
| GET | /mobile/v1/iterable/token/{device_uuid} | 7 (Login) | Push notification token |
| GET | /mobile/v1/user/settings/mobileexperiments | 7 (Login) | Feature flags |
| POST | /mobile/v1/user/flags | 7 (Login) | User flags sync |
| GET | /mobile/v1/geocaches/unlocksettings | 7 (Login) | Premium unlock config |
| GET | /mobile/v1/profileview | 7 (Login) | User profile data |
| GET | /mobile/v1/friendrequests?skip=0 | 7 (Login) | Friend requests |
| GET | /mobile/v2/map/search | 9 (Map) | Geocache map search |
| GET | /mobile/v1/user/settings/communication/touEtag | 9 (Map) | Terms of use version |

### Geocaching Web (staging.geocaching.com) — 2 unique routes

| Method | Path | Step | Purpose |
|--------|------|------|---------|
| HEAD | /account/documents/termsofuse | 9 (Map) | Terms of use check |
| HEAD | /policies/en/terms-of-use/ | 9 (Map) | Terms of use (redirect target) |

### Facebook SDK (ep2.facebook.com) — 6 unique routes

| Method | Path | Step | Purpose |
|--------|------|------|---------|
| GET | /v17.0/{app_id}/server_domain_infos | 1 (Launch) | Domain config |
| GET | /v17.0/{app_id} (fields=app_events_feature_bitmask) | 1 (Launch) | App config |
| GET | /v17.0/{app_id}/mobile_sdk_gk | 1 (Launch) | SDK gatekeepers |
| GET | /v17.0/{app_id} (fields=app_events_config) | 1 (Launch) | Events config |
| GET | /v17.0/{app_id}/model_asset | 1 (Launch) | ML model assets |
| GET | /v17.0/{app_id}/ios_skadnetwork_conversion_config | 1 (Launch) | SKAdNetwork config |

### Firebase / Google — 3 unique routes

| Method | Path | Step | Purpose |
|--------|------|------|---------|
| POST | /v1/sdkServing/projects/{id}/eligibleCampaigns:fetch | 1 (Launch) | In-app messaging |
| POST | /register (fcmtoken.googleapis.com) | 1 (Launch) | FCM push token |
| POST | /v1/projects/{id}/namespaces/firebase:fetch | 7 (Login) | Remote Config |

### Apple System — 6 unique routes

| Method | Path | Step | Purpose |
|--------|------|------|---------|
| GET | /bag.xml (init.itunes.apple.com) | 1 (Launch) | App Store init |
| GET | /config/defaults (configuration.ls.apple.com) | 7 (Login) | System config |
| GET | /pep/gcc (gspe1-ssl.ls.apple.com) | 7 (Login) | Maps geo config |
| GET | /geo_manifest/dynamic/config (gspe35-ssl.ls.apple.com) | 7 (Login) | Maps manifest |
| POST | /dispatcher.arpc (gsp-ssl.ls.apple.com) | 7 (Login) | Maps tile dispatch |
| POST | /hvr/v3/use (gsp64-ssl.ls.apple.com) | 7 (Login) | Maps vector tiles |

### Other — 2 unique routes

| Method | Path | Step | Purpose |
|--------|------|------|---------|
| GET | /chrome-variations/seed (clientservices.googleapis.com) | 1 (Launch) | Chrome variations (system) |
| PUT | /v1/configs (news-edge.apple.com) | 8 (Dismiss dialog) | Apple News config (background) |

---

## Totals

- **34 total HTTP flows** captured
- **29 unique API routes** across all hosts
- **10 unique Geocaching API routes** (the app's own backend)
- **77 system log entries** related to Geocaching
- **22 network proxy log entries**
- **9 scenario steps** from launch to map
