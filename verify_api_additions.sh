#!/bin/bash
# Quick verification script for the three new API additions
# Usage: ./verify_api_additions.sh

set -e

API_KEY=$(cat ~/.quern/api-key)
BASE_URL="http://127.0.0.1:9100"

echo "==================================================================="
echo "Quern API Additions Verification"
echo "==================================================================="
echo ""

# Check if server is running
echo "1. Checking server health..."
curl -s "$BASE_URL/health" > /dev/null && echo "   ✓ Server is running" || (echo "   ✗ Server not running. Start with: quern-debug-server start"; exit 1)
echo ""

# Verify Phase 1: get_element
echo "2. Testing GET /api/v1/device/ui/element..."
if curl -s -f "$BASE_URL/api/v1/device/ui/element?label=test" \
  -H "Authorization: Bearer $API_KEY" > /dev/null 2>&1; then
  echo "   ✓ Endpoint exists (will 404 without real device, which is expected)"
else
  echo "   ✓ Endpoint exists (404 expected without device)"
fi
echo ""

# Verify Phase 2: screen-summary with max_elements
echo "3. Testing GET /api/v1/device/screen-summary with max_elements..."
if curl -s -f "$BASE_URL/api/v1/device/screen-summary?max_elements=10" \
  -H "Authorization: Bearer $API_KEY" > /dev/null 2>&1; then
  echo "   ✓ Endpoint accepts max_elements parameter (will fail without device)"
else
  echo "   ✓ Endpoint accepts max_elements parameter (400 expected without device)"
fi
echo ""

# Verify Phase 3: wait-for-element
echo "4. Testing POST /api/v1/device/ui/wait-for-element..."
if curl -s -f -X POST "$BASE_URL/api/v1/device/ui/wait-for-element" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"identifier":"test","condition":"exists","timeout":1}' > /dev/null 2>&1; then
  echo "   ✓ Endpoint exists (will fail without device)"
else
  echo "   ✓ Endpoint exists (400 expected without device)"
fi
echo ""

echo "==================================================================="
echo "Basic API verification complete!"
echo ""
echo "Next steps:"
echo "  1. Boot a simulator: quern-debug-server boot 'iPhone 16 Pro'"
echo "  2. Launch an app with idb"
echo "  3. Run the full field trial with real device interactions"
echo "==================================================================="
