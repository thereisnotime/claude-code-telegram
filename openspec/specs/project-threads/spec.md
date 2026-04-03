# Project Threads

## Purpose
Maps Telegram topics to project directories. Each topic is an isolated workspace with its own session state. Supports private mode (bot DM topics) and group mode (supergroup forum topics).

## Requirements

### Requirement: YAML project registry
The system SHALL load project definitions from a YAML registry. PROJECTS_CONFIG_PATH defines projects with slug, name, directory, enabled flag. Notification-only projects (no directory) supported for webhook delivery topics.

#### Scenario: Projects loaded at startup
- **WHEN** the bot starts with ENABLE_PROJECT_THREADS=true
- **THEN** the YAML registry is loaded and validated against APPROVED_DIRECTORY

### Requirement: Topic synchronization
The system SHALL synchronize topics via /sync_threads. Creates, reuses, renames, reopens, closes, and deactivates topics. Pacing via PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS (default 1.1s). Stale mappings deactivated.

#### Scenario: Fresh sync with empty database
- **WHEN** /sync_threads runs with no existing mappings and Telethon disabled
- **THEN** new forum topics are created via create_forum_topic and stored in DB

#### Scenario: Re-sync with stale thread IDs
- **WHEN** /sync_threads runs and Telethon resolves a different thread_id than stored
- **THEN** the DB mapping is updated with the correct Telethon-resolved ID

### Requirement: Telethon topic resolution
The system SHALL resolve existing topics via Telethon when enabled. ENABLE_TOPIC_RESOLUTION=true uses MTProto (channels.getForumTopics) to discover existing topics. Prevents duplicate creation. Requires TELEGRAM_API_ID and TELEGRAM_API_HASH. Largest (newest) ID wins when names collide.

#### Scenario: Telethon discovers existing topics
- **WHEN** /sync_threads runs with Telethon enabled and topics already exist in the chat
- **THEN** existing topics are adopted (reused) instead of creating duplicates

### Requirement: Private chat topics
The system SHALL support private chat topics (PROJECT_THREADS_MODE=private). Topics in the bot's private chat with the user. No startup sync (chat_id unknown at boot). Thread ID extracted via message_thread_id (Bot API 9.3+) or direct_messages_topic.topic_id. Requires python-telegram-bot >=22.7.

#### Scenario: User sends message in a private chat topic
- **WHEN** a user sends a message in a mapped private chat topic
- **THEN** the thread ID is extracted, project resolved, and working directory set

### Requirement: Group forum topics
The system SHALL support group forum topics (PROJECT_THREADS_MODE=group). Topics in a supergroup forum (PROJECT_THREADS_CHAT_ID required). Startup sync and Telethon resolution run automatically.

#### Scenario: User sends message in a forum topic
- **WHEN** a user sends a message in a mapped forum topic
- **THEN** the thread ID is extracted, project resolved, and working directory set

### Requirement: Per-thread session isolation
The system SHALL isolate session state per thread. Per-thread state: current_directory, claude_session_id, project_slug. state_key format: "{chat_id}:{message_thread_id}". force_new_session scoped per-thread.

#### Scenario: User runs /new in one thread
- **WHEN** a user runs /new in thread A
- **THEN** only thread A's session is cleared; thread B retains its session
