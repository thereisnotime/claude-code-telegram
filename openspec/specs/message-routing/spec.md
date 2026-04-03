# Message Routing & Orchestration

## Purpose
MessageOrchestrator routes all Telegram updates through a middleware chain and dispatches to agentic or classic mode handlers. Handler groups: security (-3), auth (-2), rate limit (-1), commands (0), content (10).

## Requirements

### Requirement: Agentic mode routing
The system SHALL route messages through agentic mode by default. AGENTIC_MODE=true (default). Commands: /start, /new, /resume, /status, /status_all, /verbose, /repo, /version, /restart, /debug_show_config, /sync_threads. Text messages forwarded directly to Claude. Unknown /commands forwarded as natural language (filtered to exclude known commands).

#### Scenario: User sends a text message in agentic mode
- **WHEN** a user sends a text message
- **THEN** it passes through the middleware chain, thread enforcement, and is forwarded to Claude SDK

#### Scenario: User sends an unknown slash command
- **WHEN** a user sends a /command not in the known set
- **THEN** it is forwarded to Claude as natural language

### Requirement: Classic mode support
The system SHALL support classic mode with full command set. AGENTIC_MODE=false enables: /help, /continue, /end, /ls, /cd, /pwd, /projects, /status, /export, /actions, /git. Inline keyboard callbacks for project selection and quick actions.

#### Scenario: User runs /cd in classic mode
- **WHEN** a user runs /cd <directory> in classic mode
- **THEN** the working directory changes and the project session resumes

### Requirement: Thread routing enforcement
The system SHALL enforce thread routing when project threads are enabled. ENABLE_PROJECT_THREADS=true activates strict routing. Private mode requires messages in mapped topics. Group mode requires messages in the configured forum. Bypass for /sync_threads and /start.

#### Scenario: Message sent in a valid project topic
- **WHEN** a message arrives with a message_thread_id matching a DB mapping
- **THEN** the project is resolved, working directory set, and handler proceeds

#### Scenario: Message sent outside any topic
- **WHEN** a message arrives without a thread ID in thread-enforced mode
- **THEN** the message is rejected with guidance to use a project topic

### Requirement: Middleware chain ordering
The system SHALL execute the middleware chain in correct order. Group -3 security validation, group -2 authentication, group -1 rate limiting, group 0 commands, group 10 content handlers.

#### Scenario: Unauthenticated user sends a message
- **WHEN** an unauthenticated user sends a message
- **THEN** security validation runs first, then auth middleware rejects before reaching handlers

### Requirement: Telegram message limits
The system SHALL deliver responses respecting Telegram limits. Messages split at 4096-char limit. HTML parse mode with escaping. Reply quoting (REPLY_QUOTE=true). message_thread_id passed for topic delivery.

#### Scenario: Claude returns a long response
- **WHEN** Claude's response exceeds 4096 characters
- **THEN** the response is split into multiple messages delivered in order
