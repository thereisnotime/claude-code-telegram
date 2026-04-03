# Claude Integration

## Purpose
Facade wrapping ClaudeSDKManager for async streaming communication with Claude Code via claude-agent-sdk. Sessions persisted in SQLite with auto-resume, cost tracking, and retry logic.

## Requirements

### Requirement: SDK integration
The system SHALL integrate with Claude Code via the Python SDK. ClaudeSDKManager uses claude-agent-sdk with async streaming. Supports API key auth (ANTHROPIC_API_KEY) and CLI auth. Configurable model, max turns (CLAUDE_MAX_TURNS), timeout (CLAUDE_TIMEOUT_SECONDS), and per-request cost cap (CLAUDE_MAX_COST_PER_REQUEST).

#### Scenario: User sends a text message
- **WHEN** a user sends a text message in an active session
- **THEN** the prompt is forwarded to Claude SDK with session context
- **AND** the streaming response is parsed and sent back as Telegram messages

#### Scenario: SDK call times out
- **WHEN** a Claude SDK call exceeds CLAUDE_TIMEOUT_SECONDS
- **THEN** the system returns a timeout error with retry guidance

### Requirement: Session auto-resume
The system SHALL manage sessions with auto-resume. Sessions identified by Claude's ResultMessage.session_id. Per user+directory tracking in SQLite. Expiry after SESSION_TIMEOUT_HOURS (default 24h). In-flight tracking for crash recovery.

#### Scenario: User continues a conversation
- **WHEN** a user sends a message and a non-expired session exists for their user+directory
- **THEN** the system resumes that session via options.resume = session_id

#### Scenario: User runs /new then /resume
- **WHEN** a user runs /new (clearing the session) then /resume
- **THEN** the most recent non-expired session is restored and force_new_session is cleared

### Requirement: Retry with exponential backoff
The system SHALL retry transient SDK errors with exponential backoff. Max 3 attempts, base delay 1s, backoff factor 3x, max delay 30s. Configurable via CLAUDE_RETRY_* env vars.

#### Scenario: Transient SDK error occurs
- **WHEN** the SDK returns a transient error
- **THEN** the system retries with exponential backoff up to CLAUDE_RETRY_MAX_ATTEMPTS
- **AND** if all retries fail, returns an error to the user

### Requirement: Streaming with configurable verbosity
The system SHALL stream Claude responses with configurable verbosity. VERBOSE_LEVEL 0-2 controls real-time activity display. Typing indicator heartbeat every ~2s. Optional stream draft mode (ENABLE_STREAM_DRAFTS).

#### Scenario: User sets verbose level to 0
- **WHEN** VERBOSE_LEVEL=0 or user runs /verbose 0
- **THEN** only the final response is shown, with typing indicator active during processing

### Requirement: Per-session cost tracking
The system SHALL track per-session costs. Total cost, turns, and message count accumulated per session. Per-user lifetime budget via CLAUDE_MAX_COST_PER_USER.

#### Scenario: User approaches cost limit
- **WHEN** a user's cumulative cost approaches CLAUDE_MAX_COST_PER_USER
- **THEN** the rate limiter rejects further requests

### Requirement: Interrupt in-progress calls
The system SHALL support interrupting in-progress SDK calls. Stop button (inline keyboard) sets an interrupt_event to cancel the current SDK call. Interrupted sessions can be resumed.

#### Scenario: User clicks stop button
- **WHEN** a user clicks the stop button during Claude execution
- **THEN** the SDK call is cancelled and partial results are displayed
