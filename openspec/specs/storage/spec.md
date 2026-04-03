# Storage

## Purpose
SQLite database with async access, connection pooling, repository pattern, and automatic schema migrations. Stores all persistent bot state.

## Requirements

### Requirement: Async SQLite with connection pooling
The system SHALL manage the database with async SQLite and connection pooling. aiosqlite with configurable pool size (DB_POOL_SIZE, default 5). Schema versioning with automatic migrations (current: v5). WAL journal mode. detect_types=PARSE_DECLTYPES for datetime conversion.

#### Scenario: Bot starts with outdated schema
- **WHEN** the bot starts and the DB schema version is below current
- **THEN** migrations run automatically to bring the schema up to date

### Requirement: Defined table persistence
The system SHALL persist data across defined tables. Tables: user_stats, sessions (with in_flight tracking, chat_id, message_thread_id), messages, tool_usage, audit_log, cost_tracking, project_threads, webhook_events, scheduled_jobs.

#### Scenario: Session is created and persisted
- **WHEN** Claude returns a response with a session_id
- **THEN** the session is stored/updated in the sessions table with cost, turns, and message count

### Requirement: Typed domain repositories
The system SHALL provide typed repositories for domain access. Repository per domain (UserRepository, SessionStorage, ProjectThreadRepository). StorageFacade provides unified access. from_row() methods guard fromisoformat() with isinstance checks.

#### Scenario: Project thread mapping queried
- **WHEN** a message arrives with a thread ID
- **THEN** ProjectThreadRepository.get_by_chat_thread returns the matching active mapping or None

### Requirement: Timezone-aware UTC datetimes
The system SHALL use timezone-aware UTC datetimes. All datetimes use datetime.now(UTC). Never datetime.utcnow() (deprecated). SQLite adapters auto-convert TIMESTAMP/DATETIME columns.

#### Scenario: Session expiry check
- **WHEN** a session's last_used is checked against the timeout
- **THEN** the comparison uses timezone-aware UTC timestamps
