# Notifications & Scheduler

## Purpose
NotificationService delivers rate-limited messages to Telegram. APScheduler runs persistent cron jobs. Commit notifier alerts project topics about new git commits.

## Requirements

### Requirement: Telegram notification delivery
The system SHALL deliver notifications to configured Telegram chats. NOTIFICATION_CHAT_IDS (comma-separated). Rate-limited delivery. HTML parse mode. message_thread_id for topic routing.

#### Scenario: Webhook triggers a notification
- **WHEN** an AgentResponseEvent is published after a webhook-triggered Claude run
- **THEN** the response is delivered to configured notification chats with rate limiting

### Requirement: Git commit notifications
The system SHALL notify project topics about new git commits. fire_commit_notifications checks each project's git repo for new commits. Notifications delivered to mapped topic threads. Runs after /sync_threads.

#### Scenario: New commits detected after sync
- **WHEN** /sync_threads completes and a project has new commits since last check
- **THEN** a commit summary notification is sent to that project's topic thread

### Requirement: Persistent scheduled jobs
The system SHALL support persistent scheduled jobs. ENABLE_SCHEDULER=true enables APScheduler with SQLite job store. Jobs persist across restarts. Can trigger Claude commands or notifications.

#### Scenario: Scheduled job fires
- **WHEN** a cron job's schedule is reached
- **THEN** the configured action (Claude command or notification) is executed
