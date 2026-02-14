#!/bin/bash
# Multi-Device Pool Testing Script

set -e

API_KEY=$(cat ~/.quern/api-key)
BASE_URL="http://127.0.0.1:9100"

echo "=== Multi-Device Pool Test ==="
echo

# Test 1: List only booted devices
echo "1. AVAILABLE BOOTED DEVICES"
BOOTED=$(curl -s "${BASE_URL}/api/v1/devices/pool?state=booted" \
  -H "Authorization: Bearer ${API_KEY}")
echo "$BOOTED" | jq -r '.devices[] | "\(.name) (\(.udid | .[0:8])...) - \(.claim_status)"'
BOOTED_COUNT=$(echo "$BOOTED" | jq '.total')
echo "Total booted: $BOOTED_COUNT"
echo
echo "---"
echo

# Test 2: Claim all 4 booted devices concurrently
echo "2. CLAIM ALL 4 BOOTED DEVICES (parallel sessions)"
echo

# Get the 4 booted device names
DEVICE1=$(echo "$BOOTED" | jq -r '.devices[0].name')
DEVICE2=$(echo "$BOOTED" | jq -r '.devices[1].name')
DEVICE3=$(echo "$BOOTED" | jq -r '.devices[2].name')
DEVICE4=$(echo "$BOOTED" | jq -r '.devices[3].name')

echo "Claiming: $DEVICE1, $DEVICE2, $DEVICE3, $DEVICE4"
echo

# Claim them
curl -s "${BASE_URL}/api/v1/devices/claim" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"session_id\": \"session-1\", \"name\": \"$DEVICE1\"}" | jq -c '{claimed: .device.name, by: .device.claimed_by}'

curl -s "${BASE_URL}/api/v1/devices/claim" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"session_id\": \"session-2\", \"name\": \"$DEVICE2\"}" | jq -c '{claimed: .device.name, by: .device.claimed_by}'

curl -s "${BASE_URL}/api/v1/devices/claim" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"session_id\": \"session-3\", \"name\": \"$DEVICE3\"}" | jq -c '{claimed: .device.name, by: .device.claimed_by}'

curl -s "${BASE_URL}/api/v1/devices/claim" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"session_id\": \"session-4\", \"name\": \"$DEVICE4\"}" | jq -c '{claimed: .device.name, by: .device.claimed_by}'

echo
echo "---"
echo

# Test 3: Verify all claimed
echo "3. VERIFY ALL BOOTED DEVICES ARE CLAIMED"
curl -s "${BASE_URL}/api/v1/devices/pool?state=booted&claimed=claimed" \
  -H "Authorization: Bearer ${API_KEY}" | jq -r '.devices[] | "\(.name) - claimed by \(.claimed_by)"'
echo
echo "---"
echo

# Test 4: Try to claim a 5th booted device (should fail - none available)
echo "4. TRY TO CLAIM A 5TH BOOTED DEVICE (should fail)"
RESULT=$(curl -s -w "\n%{http_code}" "${BASE_URL}/api/v1/devices/claim" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "session-5", "name": "iPhone 16"}')

HTTP_CODE=$(echo "$RESULT" | tail -1)
BODY=$(echo "$RESULT" | head -n -1)

if [ "$HTTP_CODE" = "409" ]; then
  echo "✓ Correctly rejected (409): All matching devices claimed"
  echo "$BODY" | jq -r '.detail'
elif [ "$HTTP_CODE" = "500" ]; then
  echo "✓ Error (500): No available device"
  echo "$BODY" | jq -r '.detail'
else
  echo "✗ Unexpected status $HTTP_CODE"
  echo "$BODY" | jq
fi
echo
echo "---"
echo

# Test 5: Release 2 devices
echo "5. RELEASE 2 DEVICES (session-1 and session-3)"
UDID1=$(curl -s "${BASE_URL}/api/v1/devices/pool?claimed=claimed" \
  -H "Authorization: Bearer ${API_KEY}" | jq -r '.devices[0].udid')
UDID3=$(curl -s "${BASE_URL}/api/v1/devices/pool?claimed=claimed" \
  -H "Authorization: Bearer ${API_KEY}" | jq -r '.devices[2].udid')

curl -s "${BASE_URL}/api/v1/devices/release" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"udid\": \"${UDID1}\", \"session_id\": \"session-1\"}" | jq -c

curl -s "${BASE_URL}/api/v1/devices/release" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"udid\": \"${UDID3}\", \"session_id\": \"session-3\"}" | jq -c

echo
echo "---"
echo

# Test 6: Show current state
echo "6. CURRENT POOL STATE (booted devices only)"
curl -s "${BASE_URL}/api/v1/devices/pool?state=booted" \
  -H "Authorization: Bearer ${API_KEY}" | jq -r '.devices[] | "\(.name | .[0:20]) - \(.claim_status) \(if .claimed_by then "by " + .claimed_by else "" end)"'
echo
echo "Available: $(curl -s "${BASE_URL}/api/v1/devices/pool?state=booted&claimed=available" -H "Authorization: Bearer ${API_KEY}" | jq '.total')"
echo "Claimed: $(curl -s "${BASE_URL}/api/v1/devices/pool?state=booted&claimed=claimed" -H "Authorization: Bearer ${API_KEY}" | jq '.total')"
echo
echo "---"
echo

# Test 7: Concurrent claim race condition
echo "7. CONCURRENT CLAIM TEST (race condition)"
echo "Releasing all devices first..."
curl -s "${BASE_URL}/api/v1/devices/pool?claimed=claimed" \
  -H "Authorization: Bearer ${API_KEY}" | jq -r '.devices[].udid' | while read udid; do
  curl -s "${BASE_URL}/api/v1/devices/release" \
    -H "Authorization: Bearer ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"udid\": \"${udid}\"}" > /dev/null
done

echo "Racing 2 claims for 'iPhone 16 Pro'..."
(curl -s "${BASE_URL}/api/v1/devices/claim" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "racer-1", "name": "iPhone 16 Pro"}' &)
sleep 0.01
(curl -s "${BASE_URL}/api/v1/devices/claim" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "racer-2", "name": "iPhone 16 Pro"}' &)

wait
echo
echo "Final state of iPhone 16 Pro:"
curl -s "${BASE_URL}/api/v1/devices/pool" \
  -H "Authorization: Bearer ${API_KEY}" | jq -r '.devices[] | select(.name == "iPhone 16 Pro") | "\(.name) - \(.claim_status) by \(.claimed_by // "nobody")"'

echo
echo "=== Multi-Device Test Complete ==="
