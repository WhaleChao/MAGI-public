# MAGI Tools API Contract

**Service**: MAGI Tools API (三哲人)
**Port**: 5003
**Version**: 1.0
**Last Updated**: 2026-03-19

## Table of Contents

- [Overview](#overview)
- [Authentication](#authentication)
- [Authorization](#authorization)
- [Rate Limiting](#rate-limiting)
- [CORS Policy](#cors-policy)
- [Endpoints](#endpoints)
- [Error Handling](#error-handling)

## Overview

MAGI Tools API provides HTTP access to the three sage system:
- **CASPER**: Decision & Orchestration (this API)
- **MELCHIOR**: Vision & Code (GPU)
- **BALTHASAR**: Summarization (Apple Intelligence)

## Authentication

### API Key Authentication

For external integrations, use `X-API-Key` header or `api_key` query parameter:

```bash
curl -H "X-API-Key: your-api-key" https://api.example.com/osc/external/chat
curl "https://api.example.com/osc/external/chat?api_key=your-api-key"
```

**Configuration**:
- `MAGI_API_KEY`: The API key value (secret)
- `MAGI_API_KEY_REQUIRED`: Set to "1" (default) to enforce API key validation
- Alternative: `Authorization: Bearer <key>` header

### Session-Based Authentication (Internal)

Internal routes use Flask session cookies and `flask_login`:

```python
# Server enforces:
# - SESSION_COOKIE_HTTPONLY = True
# - SESSION_COOKIE_SAMESITE = 'Lax'
# - SESSION_COOKIE_SECURE = True (when MAGI_FORCE_HTTPS=1)
```

### Webhook Authentication

LINE, Discord, Telegram webhooks use signature verification:
- Endpoints: `/line/webhook`, `/callback`, `/telegram/webhook`, `/discord/webhook`
- These endpoints are exempt from API key and CSRF validation
- Signature verification is handled by webhook libraries

## Authorization

### Role-Based Access Control (RBAC)

Three roles with hierarchy:

| Role | Permissions | Usage |
|------|-------------|-------|
| `admin` | Full access to all endpoints | Internal operators, trusted integrations |
| `operator` | Operational endpoints (status, logs) | Support staff |
| `viewer` | Read-only access to non-sensitive data | Public/limited access |

**Hierarchy**: admin > operator > viewer

**Endpoint Annotations**:

- **Public (no auth)**: `/health`, `/sages`, `/melchior/health`, `/summarize/health`
- **API Key Required**: `/osc/external/*`, `/skills/*`, `/code/*`, `/legal/*`
- **Role: viewer**: `/connections`, `/definitions`, `/council/core/pending`
- **Role: operator**: `/meetings`, `/alert`
- **Role: admin**: `/api/audit_log*`, `/iron-dome/*`, `/laf/*`

### Admin Elevation

For some endpoints, role can be auto-elevated via verification:

```python
# Example: /osc/external/chat with role="user"
# If user_id matches MAGI_ADMIN_LINE_IDS or DISCORD_ADMIN_IDS:
# - Role is automatically elevated to "admin"
# - See MAGI_LINE_AUTO_ADMIN_LAST_SENDER config
```

## Rate Limiting

Lightweight rate limiter (in-process):

| Category | Limit | Window | Policy |
|----------|-------|--------|--------|
| webhook | 120 req/min | 60s | Per IP, for /line/webhook /callback /telegram/webhook |
| api | 60 req/min | 60s | Per IP, for general API endpoints |

**Response on Rate Limit**:
```json
{
  "error": "rate_limit_exceeded",
  "retry_after": 60
}
```

**Environment Variables**:
- `MAGI_WEBHOOK_RATE_LIMIT_PER_IP`: Override webhook limit
- `MAGI_API_RATE_LIMIT_PER_IP`: Override API limit

## CORS Policy

**Default Origins** (localhost only):
```
http://localhost:3000
http://localhost:5002
http://127.0.0.1:3000
http://127.0.0.1:5002
```

**Configuration**:
```bash
export MAGI_CORS_ORIGINS="https://app.example.com,https://api.example.com"
```

**CORS Headers Applied**:
- `Access-Control-Allow-Origin`: From MAGI_CORS_ORIGINS
- `Access-Control-Allow-Credentials`: true (for cookie-based auth)
- `Access-Control-Allow-Methods`: GET, POST, PUT, DELETE, OPTIONS
- `Access-Control-Max-Age`: 3600

## Endpoints

### Health & Status

#### GET /health
Public health check.

```json
{
  "status": "ok",
  "service": "MAGI Tools API (三哲人)"
}
```

#### GET /sages
Get status of all three sages.

**Auth**: Public
**Rate Limit**: api (60 req/min)

```json
{
  "casper": {
    "online": true,
    "role": "Decision & Governor"
  },
  "melchior": {
    "online": true,
    "role": "Scientist (Vision/Code)",
    "gpu": "RTX 3060",
    "models": ["llava", "qwen-vl"]
  },
  "balthasar": {
    "online": true,
    "role": "Council (Review Only)"
  }
}
```

#### GET /connections
Get status of external connections (LINE, Discord, Telegram, etc).

**Auth**: viewer role or API key
**Rate Limit**: api (60 req/min)

**Response**:
```json
{
  "line": {
    "enabled": true,
    "admin_ids_configured": true,
    "admin_ids_count": 5
  },
  "discord": {
    "enabled": true,
    "admin_ids_configured": true
  },
  "telegram": {
    "enabled": false
  }
}
```

### CASPER / OSC External

#### POST /osc/external/chat
Process user message with CASPER orchestrator.

**Auth**: API key required
**Rate Limit**: api (60 req/min)
**Timeout**: 120 seconds

**Request**:
```json
{
  "message": "What is the weather today?",
  "user_id": "user123",
  "platform": "web",
  "role": "admin"
}
```

**Response**:
```json
{
  "success": true,
  "response": "The weather today...",
  "route": "casper_default",
  "exec_time_ms": 1234
}
```

**Error Response**:
```json
{
  "success": false,
  "error": "unauthorized: invalid api key",
  "code": 401
}
```

#### POST /osc/external/case_status
Get status of a case in OSC system.

**Auth**: API key required
**Rate Limit**: api (60 req/min)

**Request**:
```json
{
  "case_id": "2024-ABC-001"
}
```

**Response**:
```json
{
  "case_id": "2024-ABC-001",
  "status": "in_progress",
  "last_updated": "2026-03-19T10:30:00Z"
}
```

#### GET /osc/external/ui
Get UI configuration for external OSC interface.

**Auth**: API key required
**Rate Limit**: api (60 req/min)

**Response**:
```json
{
  "title": "MAGI Case Manager",
  "theme": "light",
  "features": ["search", "filter", "export"]
}
```

#### GET /osc/external/health
Health check for OSC external endpoints.

**Auth**: API key required
**Rate Limit**: api (60 req/min)

**Response**:
```json
{
  "status": "healthy",
  "dependencies": {
    "database": "ok",
    "cache": "ok"
  }
}
```

### Vision & Analysis

#### POST /vision
Vision analysis on image (uses MELCHIOR).

**Auth**: API key or session
**Rate Limit**: api (60 req/min)
**Timeout**: 60 seconds

**Request**:
```json
{
  "image_path": "/path/to/image.jpg",
  "prompt": "Describe what you see"
}
```

**Response**:
```json
{
  "success": true,
  "analysis": "The image shows...",
  "model": "llava-13b",
  "exec_time_ms": 2500
}
```

#### POST /summarize
Text summarization (uses BALTHASAR).

**Auth**: Public
**Rate Limit**: api (60 req/min)
**Timeout**: 30 seconds

**Request**:
```json
{
  "text": "Long text to summarize...",
  "max_tokens": 100
}
```

**Response**:
```json
{
  "success": true,
  "summary": "Summary of the text...",
  "provider": "balthasar",
  "tokens_used": 87
}
```

#### GET /summarize/health
Health check for summarization service.

**Auth**: Public
**Response**:
```json
{
  "status": "healthy",
  "circuit_breaker": "closed"
}
```

### Search & Fetch

#### POST /search
Search query processing.

**Auth**: API key or session
**Rate Limit**: api (60 req/min)

**Request**:
```json
{
  "query": "Python best practices"
}
```

#### POST /research
Research task (more comprehensive than search).

**Auth**: API key or session
**Rate Limit**: api (60 req/min)

#### POST /fetch
Fetch and parse URL content.

**Auth**: API key or session
**Rate Limit**: api (60 req/min)

**Request**:
```json
{
  "url": "https://example.com/article"
}
```

### Skills Management

#### GET /skills
List all available skills.

**Auth**: API key or session
**Rate Limit**: api (60 req/min)

**Response**:
```json
{
  "skills": [
    {
      "name": "web_scraper",
      "version": "1.0.0",
      "status": "stable"
    }
  ]
}
```

#### POST /skills
Create new skill.

**Auth**: API key + admin role
**Rate Limit**: api (60 req/min)

#### POST /skills/run
Execute a skill.

**Auth**: API key or session
**Rate Limit**: api (60 req/min)

**Request**:
```json
{
  "skill_name": "web_scraper",
  "params": {
    "url": "https://example.com"
  }
}
```

#### POST /skills/discover
Discover skills from external sources.

**Auth**: API key + operator role
**Rate Limit**: api (60 req/min)

#### POST /skills/install
Install skill from discovered source.

**Auth**: API key + admin role
**Rate Limit**: api (60 req/min)

#### POST /skills/versions
Get skill version history.

**Auth**: API key or session
**Rate Limit**: api (60 req/min)

#### POST /skills/rollback
Rollback skill to previous version.

**Auth**: API key + admin role
**Rate Limit**: api (60 req/min)

#### GET /skills/release
Get current release state.

**Auth**: API key or session
**Rate Limit**: api (60 req/min)

#### POST /skills/stable
Set skill as stable.

**Auth**: API key + admin role
**Rate Limit**: api (60 req/min)

#### POST /skills/canary/start
Start canary deployment.

**Auth**: API key + operator role
**Rate Limit**: api (60 req/min)

#### POST /skills/canary/stop
Stop canary deployment.

**Auth**: API key + operator role
**Rate Limit**: api (60 req/min)

#### POST /skills/ci
Trigger skill CI/CD.

**Auth**: API key + operator role
**Rate Limit**: api (60 req/min)

#### GET /skills/events
Get skill lifecycle events.

**Auth**: API key or session
**Rate Limit**: api (60 req/min)

#### POST /skills/teach
Teach skill from prompt.

**Auth**: API key + operator role
**Rate Limit**: api (60 req/min)

#### POST /skills/teach/file
Teach skill from file.

**Auth**: API key + operator role
**Rate Limit**: api (60 req/min)

#### POST /skills/internalize
Internalize skill knowledge.

**Auth**: API key + admin role
**Rate Limit**: api (60 req/min)

#### POST /skills/internalize/codebase
Internalize codebase as skill knowledge.

**Auth**: API key + admin role
**Rate Limit**: api (60 req/min)

#### POST /skills/knowledge/stats
Get skill knowledge statistics.

**Auth**: API key or session
**Rate Limit**: api (60 req/min)

### Code Management

#### POST /code/autofix
Auto-fix code with AI assistance.

**Auth**: API key + operator role
**Rate Limit**: api (60 req/min)

**Request**:
```json
{
  "code": "def foo(x):\n  return x + 1",
  "issue": "Add type hints"
}
```

#### POST /code/skill-cycle
Run skill cycle for code.

**Auth**: API key + operator role
**Rate Limit**: api (60 req/min)

### Collaboration

#### POST /collab/chat
Collaborative chat.

**Auth**: API key or session
**Rate Limit**: api (60 req/min)

#### POST /collab/music
Music generation/collaboration.

**Auth**: API key or session
**Rate Limit**: api (60 req/min)

#### POST /collab/transcribe
Transcribe audio.

**Auth**: API key or session
**Rate Limit**: api (60 req/min)

#### POST /collab/translate
Translate text.

**Auth**: API key or session
**Rate Limit**: api (60 req/min)

### Legal & Compliance

#### GET /legal
Get legal documents/terms.

**Auth**: API key or session (viewer role)
**Rate Limit**: api (60 req/min)

#### POST /legal/<skill_name>
Get legal/compliance info for specific skill.

**Auth**: API key + admin role
**Rate Limit**: api (60 req/min)

### Governance & Council

#### GET /council/core/pending
Get pending council approvals.

**Auth**: viewer role
**Rate Limit**: api (60 req/min)

#### POST /council/core/approve
Approve pending item.

**Auth**: admin role
**Rate Limit**: api (60 req/min)

#### POST /council/core/reject
Reject pending item.

**Auth**: admin role
**Rate Limit**: api (60 req/min)

### Security & Hardening

#### GET /iron-dome/patterns
List security patterns.

**Auth**: admin role
**Rate Limit**: api (60 req/min)

#### POST /iron-dome/patterns
Add security pattern.

**Auth**: admin role
**Rate Limit**: api (60 req/min)

#### POST /iron-dome/auto-harden
Run automatic security hardening.

**Auth**: admin role
**Rate Limit**: api (60 req/min)

### Audit & Monitoring

#### GET /api/audit_log
Get audit log entries.

**Auth**: admin role
**Rate Limit**: api (60 req/min)

#### POST /api/audit_log/restore/<log_id>
Restore from audit log snapshot.

**Auth**: admin role
**Rate Limit**: api (60 req/min)

### Meetings & Scheduling

#### GET /meetings
List meetings.

**Auth**: operator role
**Rate Limit**: api (60 req/min)

#### POST /meetings
Create meeting.

**Auth**: operator role
**Rate Limit**: api (60 req/min)

### Alerts

#### POST /alert
Send alert.

**Auth**: operator role
**Rate Limit**: api (60 req/min)

**Request**:
```json
{
  "level": "warning",
  "message": "System alert"
}
```

### Clients & Definitions

#### GET /clients
Get registered clients.

**Auth**: operator role
**Rate Limit**: api (60 req/min)

#### POST /clients
Register new client.

**Auth**: admin role
**Rate Limit**: api (60 req/min)

#### GET /definitions
Get system definitions/schemas.

**Auth**: viewer role
**Rate Limit**: api (60 req/min)

### Memory & Recall

#### POST /remember
Store information in memory.

**Auth**: API key or session
**Rate Limit**: api (60 req/min)

**Request**:
```json
{
  "key": "important_fact",
  "value": "...",
  "ttl": 3600
}
```

#### POST /recall
Retrieve information from memory.

**Auth**: API key or session
**Rate Limit**: api (60 req/min)

**Request**:
```json
{
  "key": "important_fact"
}
```

### MELCHIOR (Vision/GPU)

#### GET /melchior/health
Health check for MELCHIOR service.

**Auth**: Public
**Rate Limit**: api (60 req/min)

#### POST /melchior/skills/sync
Sync skills with MELCHIOR.

**Auth**: API key + admin role
**Rate Limit**: api (60 req/min)

### Miscellaneous

#### POST /laf/smoke_login
LAF (Legal AI Framework) smoke test login.

**Auth**: admin role
**Rate Limit**: api (60 req/min)

#### GET /static/exports/<path:filename>
Download exported file.

**Auth**: Public (if file exists and accessible)
**Rate Limit**: api (60 req/min)

## Error Handling

### Standard Error Response

```json
{
  "error": "error_code",
  "message": "Human-readable error message",
  "code": 400
}
```

### Common Error Codes

| Code | Status | Meaning |
|------|--------|---------|
| 400 | Bad Request | Invalid request format/parameters |
| 401 | Unauthorized | Missing or invalid authentication |
| 403 | Forbidden | Insufficient role/permissions |
| 404 | Not Found | Resource not found |
| 429 | Too Many Requests | Rate limit exceeded |
| 500 | Internal Server Error | Server error |
| 503 | Service Unavailable | Service misconfiguration or dependency down |

### CSRF Error Response

```json
{
  "error": "forbidden: invalid CSRF token",
  "code": "csrf_validation_failed",
  "reason": "csrf_token_mismatch"
}
```

## Security Best Practices

1. **API Key Storage**: Store MAGI_API_KEY in secure environment (not in code/git)
2. **HTTPS**: Always use HTTPS in production (set MAGI_FORCE_HTTPS=1)
3. **Rate Limiting**: Respect rate limits; implement exponential backoff on 429
4. **CORS**: Restrict MAGI_CORS_ORIGINS to trusted domains only
5. **Session Security**: Ensure cookies sent only over HTTPS (SAMESITE=Lax)
6. **CSRF Protection**: Include CSRF token in POST/PUT/DELETE from browsers
7. **Audit Logging**: All access attempts are logged for compliance

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| MAGI_API_KEY | (unset) | API key for external integrations |
| MAGI_API_KEY_REQUIRED | "1" | Enforce API key validation |
| MAGI_EXTERNAL_API_KEY | (unset) | Alternative API key field (legacy) |
| MAGI_EXTERNAL_API_KEY_REQUIRED | "1" | Enforce external API key |
| MAGI_CORS_ORIGINS | localhost:3000,localhost:5002 | Allowed CORS origins |
| MAGI_FORCE_HTTPS | "0" | Enable HTTPS-only cookies |
| MAGI_ADMIN_LINE_IDS | (unset) | Comma-separated LINE admin user IDs |
| MAGI_ADMIN_DISCORD_IDS | (unset) | Comma-separated Discord admin user IDs |
| MAGI_LINE_AUTO_ADMIN_LAST_SENDER | "0" | Auto-elevate last sender on LINE |
| FLASK_SECRET_KEY | (required) | Session encryption key |

---

**For security updates or questions**: Contact security@magi-internal.example.com
