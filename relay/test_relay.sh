#!/bin/bash
# Test script for the relay
# Sends simulated webhook deliveries to the relay to verify filtering
#
# Reads BOT_USERNAME from config.env to make test payloads identity-agnostic.

set -e
RELAY_URL="${1:-http://127.0.0.1:9191}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Read BOT_USERNAME from config.env
BOT_USERNAME="bot-not-set"
if [[ -f "$SCRIPT_DIR/config.env" ]]; then
  BOT_USERNAME=$(grep -E '^BOT_USERNAME=' "$SCRIPT_DIR/config.env" | cut -d= -f2)
fi
if [[ -z "$BOT_USERNAME" ]]; then
  BOT_USERNAME="bot-not-set"
fi

SECRET=""
if [[ -f "$SCRIPT_DIR/config.env" ]]; then
  SECRET=$(grep -E '^GITHUB_WEBHOOK_SECRET=' "$SCRIPT_DIR/config.env" | cut -d= -f2)
fi

# --- Test 1: Ping (should return pong) ---
echo "=== Test 1: Ping ==="
PAYLOAD='{"zen":"Keep it logically awesome.","hook_id":12345,"repository":{"full_name":"security-alliance/frameworks"}}'
SIG=""
if [ -n "$SECRET" ]; then
    SIG="sha256=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')"
fi
curl -s -X POST "$RELAY_URL" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: ping" \
  -H "X-GitHub-Delivery: test-ping-001" \
  ${SIG:+-H "X-Hub-Signature-256: $SIG"} \
  -d "$PAYLOAD"
echo ""

# --- Test 2: Wrong repo (should be ignored) ---
echo "=== Test 2: Wrong repo ==="
PAYLOAD='{"action":"assigned","assignee":{"login":"'"$BOT_USERNAME"'"},"sender":{"login":"test-sender"},"repository":{"full_name":"other/repo"},"issue":{"number":1,"title":"Test"}}'
if [ -n "$SECRET" ]; then
    SIG="sha256=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')"
fi
curl -s -X POST "$RELAY_URL" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: issues" \
  -H "X-GitHub-Delivery: test-wrong-repo-001" \
  ${SIG:+-H "X-Hub-Signature-256: $SIG"} \
  -d "$PAYLOAD"
echo ""

# --- Test 3: Non-whitelisted sender (should be ignored) ---
echo "=== Test 3: Non-whitelisted sender ==="
PAYLOAD='{"action":"assigned","assignee":{"login":"'"$BOT_USERNAME"'"},"sender":{"login":"random-user"},"repository":{"full_name":"security-alliance/frameworks"},"issue":{"number":1,"title":"Test issue"}}'
if [ -n "$SECRET" ]; then
    SIG="sha256=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')"
fi
curl -s -X POST "$RELAY_URL" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: issues" \
  -H "X-GitHub-Delivery: test-non-whitelisted-001" \
  ${SIG:+-H "X-Hub-Signature-256: $SIG"} \
  -d "$PAYLOAD"
echo ""

# --- Test 4: Bot self-event (should be ignored) ---
echo "=== Test 4: Bot self-event ==="
PAYLOAD='{"action":"assigned","assignee":{"login":"'"$BOT_USERNAME"'"},"sender":{"login":"'"$BOT_USERNAME"'"},"repository":{"full_name":"security-alliance/frameworks"},"issue":{"number":1,"title":"Test issue"}}'
if [ -n "$SECRET" ]; then
    SIG="sha256=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $NF}')"
fi
curl -s -X POST "$RELAY_URL" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: issues" \
  -H "X-GitHub-Delivery: test-self-event-001" \
  ${SIG:+-H "X-Hub-Signature-256: $SIG"} \
  -d "$PAYLOAD"
echo ""

# --- Test 5: Valid issue assigned (should be accepted -- WILL SPAWN HERMES) ---
echo "=== Test 5: Valid issue assigned (WILL SPAWN HERMES) ==="
echo "Skipping live test to avoid accidental Hermes spawn."
echo "To test this manually, send:"
echo "  curl -X POST $RELAY_URL \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -H 'X-GitHub-Event: issues' \\"
echo "    -H 'X-GitHub-Delivery: test-valid-001' \\"
echo "    -d '{\"action\":\"assigned\",\"assignee\":{\"login\":\"$BOT_USERNAME\"},\"sender\":{\"login\":\"test-sender\"},\"repository\":{\"full_name\":\"security-alliance/frameworks\"},\"issue\":{\"number\":999,\"title\":\"Test issue\"}}'"

echo ""
echo "=== All filter tests complete ==="
