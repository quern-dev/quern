#!/bin/bash
# Device Pool Manual Testing Script

set -e

API_KEY=$(cat ~/.quern/api-key)
BASE_URL="http://127.0.0.1:9100"

echo "=== Device Pool Manual Test Suite ==="
echo

# Test 1: List all devices in pool
echo "1. LIST ALL DEVICES"
curl -s "${BASE_URL}/api/v1/devices/pool" \
  -H "Authorization: Bearer ${API_KEY}" | jq
echo
echo "---"
echo

# Test 2: List only booted devices
echo "2. LIST BOOTED DEVICES"
curl -s "${BASE_URL}/api/v1/devices/pool?state=booted" \
  -H "Authorization: Bearer ${API_KEY}" | jq
echo
echo "---"
echo

# Test 3: Claim a device
echo "3. CLAIM A DEVICE"
CLAIM_RESPONSE=$(curl -s "${BASE_URL}/api/v1/devices/claim" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "manual-test-session", "name": "iPhone"}')
echo "$CLAIM_RESPONSE" | jq
CLAIMED_UDID=$(echo "$CLAIM_RESPONSE" | jq -r '.device.udid')
echo
echo "Claimed UDID: $CLAIMED_UDID"
echo "---"
echo

# Test 4: List claimed devices
echo "4. LIST CLAIMED DEVICES"
curl -s "${BASE_URL}/api/v1/devices/pool?claimed=claimed" \
  -H "Authorization: Bearer ${API_KEY}" | jq
echo
echo "---"
echo

# Test 5: Try to claim the same device (should fail with 409)
echo "5. TRY TO CLAIM SAME DEVICE (should fail)"
curl -s -w "\nHTTP Status: %{http_code}\n" \
  "${BASE_URL}/api/v1/devices/claim" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "different-session", "name": "iPhone"}' | jq
echo
echo "---"
echo

# Test 6: Release the device
echo "6. RELEASE THE DEVICE"
curl -s "${BASE_URL}/api/v1/devices/release" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{\"udid\": \"${CLAIMED_UDID}\", \"session_id\": \"manual-test-session\"}" | jq
echo
echo "---"
echo

# Test 7: Verify it's available again
echo "7. VERIFY DEVICE IS AVAILABLE"
curl -s "${BASE_URL}/api/v1/devices/pool?claimed=available" \
  -H "Authorization: Bearer ${API_KEY}" | jq
echo
echo "---"
echo

# Test 8: Claim again to test cleanup
echo "8. CLAIM AGAIN (for cleanup test)"
curl -s "${BASE_URL}/api/v1/devices/claim" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "cleanup-test", "name": "iPhone"}' | jq
echo
echo "---"
echo

# Test 9: Manual cleanup (should find nothing since claim is fresh)
echo "9. RUN CLEANUP (should release nothing - claim is fresh)"
curl -s "${BASE_URL}/api/v1/devices/cleanup" \
  -H "Authorization: Bearer ${API_KEY}" | jq
echo
echo "---"
echo

# Test 10: Refresh pool
echo "10. REFRESH POOL FROM SIMCTL"
curl -s "${BASE_URL}/api/v1/devices/refresh" \
  -H "Authorization: Bearer ${API_KEY}" | jq
echo
echo "---"
echo

echo "=== Test Suite Complete ==="
echo
echo "Next steps:"
echo "1. Check server logs for device pool messages"
echo "2. Inspect state file: cat ~/.quern/device-pool.json | jq"
echo "3. Test MCP tools (see instructions below)"
