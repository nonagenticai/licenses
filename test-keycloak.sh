#!/bin/bash
# Test Keycloak integration for Licensing API

set -e

echo "🔐 Testing Keycloak Integration"
echo "================================"
echo ""

# Load environment variables
source .env

echo "1. Testing service account authentication (client credentials flow)..."
echo ""

# Get access token using client credentials
# Using CA cert for SSL verification
CA_CERT_PATH="../keycloak/certs/ca-cert.pem"
if [ ! -f "$CA_CERT_PATH" ]; then
  echo "⚠️  CA certificate not found at $CA_CERT_PATH, using insecure mode"
  CURL_OPTS="--insecure"
else
  CURL_OPTS="--cacert $CA_CERT_PATH"
fi

TOKEN_RESPONSE=$(curl -s -X POST "${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token" \
  $CURL_OPTS \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -d "client_id=${KEYCLOAK_CLIENT_ID}" \
  -d "client_secret=${KEYCLOAK_CLIENT_SECRET}")

# Extract access token
ACCESS_TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.access_token')

if [ "$ACCESS_TOKEN" = "null" ] || [ -z "$ACCESS_TOKEN" ]; then
  echo "❌ Failed to get access token!"
  echo "Response: $TOKEN_RESPONSE"
  exit 1
fi

echo "✅ Successfully obtained access token"
echo "Token (first 50 chars): ${ACCESS_TOKEN:0:50}..."
echo ""

echo "2. Decoding token to check roles..."
echo ""

# Decode JWT token (base64 decode the payload)
PAYLOAD=$(echo "$ACCESS_TOKEN" | cut -d '.' -f 2)
# Add padding if needed
case $((${#PAYLOAD} % 4)) in
  2) PAYLOAD="${PAYLOAD}==" ;;
  3) PAYLOAD="${PAYLOAD}=" ;;
esac

DECODED=$(echo "$PAYLOAD" | base64 -d 2>/dev/null | jq '.')

echo "Token payload:"
echo "$DECODED" | jq '{
  sub: .sub,
  azp: .azp,
  realm_access: .realm_access,
  resource_access: .resource_access,
  scope: .scope,
  exp: .exp
}'
echo ""

# Check if license-admin role is present
HAS_ADMIN_ROLE=$(echo "$DECODED" | jq -r '.realm_access.roles | contains(["license-admin"])')

if [ "$HAS_ADMIN_ROLE" = "true" ]; then
  echo "✅ Service account has license-admin role"
else
  echo "⚠️  Service account does NOT have license-admin role"
  echo "Available roles:"
  echo "$DECODED" | jq -r '.realm_access.roles[]'
fi

echo ""
echo "3. Testing userinfo endpoint..."
echo ""

# Get user info
USERINFO=$(curl -s -X GET "${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/userinfo" \
  $CURL_OPTS \
  -H "Authorization: Bearer ${ACCESS_TOKEN}")

echo "User info:"
echo "$USERINFO" | jq '.'

echo ""
echo "================================"
echo "✅ Keycloak integration test complete!"
echo ""
echo "You can now use this token in your API requests:"
echo "curl -H 'Authorization: Bearer ${ACCESS_TOKEN:0:50}...' https://licenses.example.com/api/endpoint"
