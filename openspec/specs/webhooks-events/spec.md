# Webhooks & Events

## Purpose
FastAPI server receives webhooks from external services. Events published to an async EventBus for decoupled processing. AgentHandler routes webhook events to Claude for automated responses.

## Requirements

### Requirement: FastAPI webhook server
The system SHALL accept webhooks via a FastAPI server. ENABLE_API_SERVER=true, API_SERVER_PORT (default 8080). POST /webhooks/{provider} endpoint. Health check endpoint.

#### Scenario: GitHub webhook arrives
- **WHEN** a POST request hits /webhooks/github with a valid HMAC-SHA256 signature
- **THEN** the payload is validated, deduplicated, and published to the EventBus

### Requirement: Webhook signature verification
The system SHALL verify webhook signatures. GitHub HMAC-SHA256 via GITHUB_WEBHOOK_SECRET. Generic Bearer token via WEBHOOK_API_SECRET.

#### Scenario: Webhook with invalid signature
- **WHEN** a webhook arrives with an invalid or missing signature
- **THEN** the request is rejected with 401/403

### Requirement: Webhook deduplication
The system SHALL deduplicate webhook deliveries. Atomic deduplication via webhook_events table. Duplicate delivery IDs rejected.

#### Scenario: Duplicate webhook delivery
- **WHEN** a webhook with a previously seen delivery_id arrives
- **THEN** the duplicate is rejected without re-processing

### Requirement: Async EventBus routing
The system SHALL route webhook events through an async EventBus. Typed events (WebhookEvent, AgentResponseEvent). EventSecurityMiddleware validates events. Subscribers registered by event type.

#### Scenario: Webhook event published
- **WHEN** a validated webhook event is published to the EventBus
- **THEN** AgentHandler receives it, routes to Claude, and publishes the response as AgentResponseEvent

### Requirement: Agent response delivery
The system SHALL deliver agent responses via NotificationService. NotificationService sends rate-limited responses to configured Telegram chats with topic routing.

#### Scenario: Claude responds to a webhook event
- **WHEN** AgentHandler publishes an AgentResponseEvent
- **THEN** NotificationService delivers it to the appropriate Telegram chat/topic
