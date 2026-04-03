# Authentication & Security

## Purpose
5-layer defense protecting bot access: authentication, directory isolation, input validation, rate limiting, and audit logging.

## Requirements

### Requirement: Multi-provider authentication
The system SHALL authenticate users via a multi-provider chain. AuthenticationManager tries providers in sequence; first success creates a session with configurable expiry (default 24h).

#### Scenario: First message from unknown user
- **WHEN** an unauthenticated user sends a message
- **THEN** the system attempts authentication via all providers in order
- **AND** on success, creates a session and sends a welcome message

#### Scenario: Authenticated user sends a message
- **WHEN** an authenticated user sends a message
- **THEN** the session is refreshed and the request proceeds

### Requirement: Whitelist authentication
The system SHALL support whitelist-based authentication. WhitelistAuthProvider checks user_id against ALLOWED_USERS (comma-separated Telegram IDs). Fallback allow_all_dev mode for development.

#### Scenario: Whitelisted user authenticates
- **WHEN** a user whose Telegram ID is in ALLOWED_USERS sends a message
- **THEN** authentication succeeds

### Requirement: Token authentication
The system SHALL support token-based authentication. TokenAuthProvider generates URL-safe tokens (32 bytes), hashes with SHA256 + secret. Tokens expire after 30 days. Requires ENABLE_TOKEN_AUTH=true and AUTH_TOKEN_SECRET.

#### Scenario: User authenticates with valid token
- **WHEN** a user provides a valid, non-expired token
- **THEN** authentication succeeds and a session is created

### Requirement: Input validation
The system SHALL validate all user input for dangerous patterns. SecurityValidator blocks path traversal, shell injection, and access to secrets. Relaxed with DISABLE_SECURITY_PATTERNS=true.

#### Scenario: User sends input with shell injection
- **WHEN** a message contains shell metacharacters like `;` or `$()`
- **THEN** the input is rejected before reaching Claude

### Requirement: Tool monitoring
The system SHALL monitor Claude tool calls against an allowlist. ToolMonitor validates tool names, file path boundaries, and dangerous bash patterns. Bypassed with DISABLE_TOOL_VALIDATION=true.

#### Scenario: Claude attempts a disallowed tool
- **WHEN** Claude calls a tool not in CLAUDE_ALLOWED_TOOLS
- **THEN** the tool call is blocked and logged

### Requirement: Rate limiting
The system SHALL enforce per-user rate limits. Token bucket algorithm with configurable requests/window/burst. Per-user cost-based limiting via CLAUDE_MAX_COST_PER_USER.

#### Scenario: User exceeds rate limit
- **WHEN** a user exceeds the configured request rate
- **THEN** subsequent messages are rejected until the bucket refills

### Requirement: Audit logging
The system SHALL audit all security-relevant events. Auth attempts, commands, and security events logged to SQLite audit_log table via structlog.

#### Scenario: Authentication attempt occurs
- **WHEN** any authentication attempt is made
- **THEN** the attempt is recorded in the audit log with user_id, success status, and method

### Requirement: Directory isolation
The system SHALL isolate file access to APPROVED_DIRECTORY. All file operations constrained with path traversal prevention in both user input and tool call validation.

#### Scenario: Path traversal attempt
- **WHEN** a user or tool call references a path outside APPROVED_DIRECTORY
- **THEN** the operation is blocked
