# Licensing Service Setup Guide

This guide will help you set up and run the Licensing Service.

## Prerequisites

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) (dependency management — there is no `requirements.txt`)
- PostgreSQL 18 (a PgBouncer in front of it is optional but supported)
- Docker and Docker Compose (optional, for containerized deployment)
- Keycloak instance (for authentication)

## Installation

### 1. Install Dependencies

```bash
# Install uv if not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install the locked dependency set (creates .venv)
uv sync --frozen
```

### 2. Configure Environment Variables

Create a `.env` file based on `.env.example`:

```bash
cp .env.example .env
```

Edit `.env` and update the following:

- `POSTGRES_PASSWORD_LICENSES`: Your PostgreSQL password
- `DATABASE_URL`: Your PostgreSQL connection string
- `KEYCLOAK_URL`: Your Keycloak instance URL
- `REALM_NAME`: Your Keycloak realm name

### 3. Initialize Database

Run the SQL initialization script to create tables:

```bash
# Connect to PostgreSQL
psql -U postgres -h localhost -p 5432

# Create database and user
CREATE DATABASE licenses;
CREATE USER licenses_user WITH PASSWORD 'your_password';
GRANT ALL PRIVILEGES ON DATABASE licenses TO licenses_user;

# Connect to licenses database
\c licenses

# Run initialization script
\i init.sql
```

### 4. Generate SSL Certificates (if needed)

If you don't have SSL certificates, generate self-signed ones:

```bash
# Generate CA certificate
openssl req -x509 -newkey rsa:4096 -nodes \
  -keyout licenses-ca-key.pem -out licenses-ca-cert.pem \
  -days 365 -subj "/CN=Licenses CA"

# Generate server certificate
openssl req -newkey rsa:4096 -nodes \
  -keyout licenses-key.pem -out licenses-csr.pem \
  -subj "/CN=licenses.example.com"

# Sign with CA
openssl x509 -req -in licenses-csr.pem \
  -CA licenses-ca-cert.pem -CAkey licenses-ca-key.pem \
  -CAcreateserial -out licenses-cert.pem -days 365
```

## Running the Service

### Development Mode

```bash
# Run the service
uv run python main.py

# Or activate the virtual environment and run directly
source .venv/bin/activate
python main.py
```

The API will be available at `https://localhost:8544`

### Production Mode with Docker

```bash
# Build and start the container
docker-compose up -d

# View logs
docker-compose logs -f

# Stop the container
docker-compose down
```

## API Documentation

Once running, visit:
- Swagger UI: `https://localhost:8544/docs`
- ReDoc: `https://localhost:8544/redoc`

## Keycloak Setup

### Required Roles

Create the following roles in your Keycloak realm:

- `license-admin`: Full access to all endpoints
- `license-manager`: Can create/update licenses and tokens
- `license-reader`: Read-only access to licenses, apps, and plans

### Client Configuration

1. Create a new client in Keycloak for the Licensing Service
2. Enable "Client authentication"
3. Add service account roles
4. Configure redirect URIs if needed

## API Usage

### Authentication

All endpoints (except `/health` and `/license/introspect`) require a valid JWT token from Keycloak.

Include the token in the Authorization header:

```
Authorization: Bearer <your-jwt-token>
```

### Token Introspection (Public)

Check if a license token is valid:

```bash
curl -X POST https://localhost:8544/license/introspect \
  -H "Content-Type: application/json" \
  -d '{"token": "your-license-token"}'
```

### Create an App

```bash
curl -X POST https://localhost:8544/apps \
  -H "Authorization: Bearer <keycloak-jwt>" \
  -H "Content-Type: application/json" \
  -d '{
    "key": "my_app",
    "name": "My Application",
    "description": "An awesome app"
  }'
```

### Create a Plan

```bash
curl -X POST https://localhost:8544/plans \
  -H "Authorization: Bearer <keycloak-jwt>" \
  -H "Content-Type: application/json" \
  -d '{
    "app_id": "app-uuid",
    "code": "trial",
    "name": "Trial Plan",
    "duration_days": 30
  }'
```

### Create a License

```bash
curl -X POST https://localhost:8544/licenses/user/<keycloak-sub> \
  -H "Authorization: Bearer <keycloak-jwt>" \
  -H "Content-Type: application/json" \
  -d '{
    "app_id": "app-uuid",
    "plan_id": "plan-uuid"
  }'
```

### Create a Token for a License

```bash
curl -X POST https://localhost:8544/licenses/<license-uuid>/tokens \
  -H "Authorization: Bearer <keycloak-jwt>" \
  -d "token_type=api&expires_days=365"
```

## Architecture

```
┌──────────────────────────────┐
│          Website              │
│  (Docs + Auth Portal)         │
└────────────┬─────────────────┘
             │ OAuth2
             ▼
    ┌──────────────────┐
    │    Keycloak       │
    │(GitHub, Google...)│
    └─────────┬────────┘
              │ OIDC/JWT
              ▼
┌──────────────────────────────┐
│     Licensing Service         │
│  (FastAPI + Postgres)         │
│  Tokens, Licenses, Revokes    │
└────────────┬─────────────────┘
             │                  |
             ▼                  ▼
    ┌──────────────────┐      ┌──────────────────┐
    │    Verdaccio      │     │    Other         │
    │ Private Registry  │     │     App          │
    └─────────┬────────┘      └─────────┬────────┘
              │
              ▼
    ┌──────────────────┐
    │     n8n User     │
    │  installs nodes  │
    └──────────────────┘
```

## Database Schema

The service uses the following tables:

- `region`: Geographic regions for licensing
- `subject`: Opaque user references (Keycloak sub)
- `app`: Licensed applications
- `plan`: Subscription plans per app
- `license`: User entitlements to apps
- `token`: API tokens for license validation

See `init.sql` for the complete schema.

## Troubleshooting

### Connection Issues

If you can't connect to PostgreSQL:

1. Check that PostgreSQL is running
2. Verify pgbouncer configuration
3. Check firewall rules
4. Verify DATABASE_URL in `.env`

### JWT Validation Errors

If JWT tokens aren't validating:

1. Verify KEYCLOAK_URL is correct
2. Check that REALM_NAME matches your Keycloak realm
3. Ensure SSL certificates are trusted
4. Check token expiration

### Database Migration Issues

If tables aren't created:

1. Check PostgreSQL logs
2. Verify user permissions
3. Run `init.sql` manually
4. Check for conflicting table names

## Development

### Running Tests

The suite needs a real PostgreSQL 18 — no SQLite, no fallback.

```bash
uv run pytest -q
```

### Code Formatting and Linting

```bash
uv run ruff check .
uv run ruff format .
```

### Type Checking

```bash
uv run ty check .
```

## License

Apache 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
