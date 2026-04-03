# Bot Features

## Purpose
Feature-flagged capabilities: voice transcription, file uploads, git integration, image handling, quick actions, session export. Each independently enabled/disabled via FeatureRegistry.

## Requirements

### Requirement: Voice transcription
The system SHALL transcribe voice messages via configurable providers. ENABLE_VOICE_MESSAGES=true (default). VOICE_PROVIDER=mistral|openai|local. Mistral uses Voxtral (MISTRAL_API_KEY). OpenAI uses Whisper (OPENAI_API_KEY). Local uses whisper.cpp + ffmpeg. Max size VOICE_MAX_FILE_SIZE_MB (default 20).

#### Scenario: User sends a voice message
- **WHEN** a user sends a voice message under the size limit
- **THEN** it is downloaded, transcribed via the configured provider, and the transcript is forwarded to Claude

#### Scenario: Voice provider is unavailable
- **WHEN** the configured voice provider lacks API keys or binaries
- **THEN** the user receives provider-specific setup guidance

### Requirement: File uploads and archives
The system SHALL handle file uploads and archives. ENABLE_FILE_UPLOADS=true (default). Documents saved to working directory. Archive extraction supported. MAX_FILE_UPLOAD_SIZE_MB (default 100).

#### Scenario: User uploads a document
- **WHEN** a user uploads a file
- **THEN** the file is downloaded, saved to the working directory, and Claude is notified

### Requirement: Image upload processing
The system SHALL process image uploads for analysis. ENABLE_IMAGE_UPLOADS=true (default). Photos sent to Claude as image attachments. Caption text used as additional prompt context.

#### Scenario: User sends a screenshot with caption
- **WHEN** a user sends a photo with caption text
- **THEN** the image and caption are forwarded to Claude for analysis

### Requirement: Read-only git operations
The system SHALL provide read-only git operations. ENABLE_GIT_INTEGRATION=true (default). Operations: status, log, diff, branch. Timeout GIT_OPERATIONS_TIMEOUT (default 30s). Agentic mode delegates to Claude.

#### Scenario: User runs /git status in classic mode
- **WHEN** a user runs /git status
- **THEN** the system returns the git status of the current working directory

### Requirement: Configurable output verbosity
The system SHALL support configurable output verbosity. VERBOSE_LEVEL=0|1|2 (default 1). Overridable per-session via /verbose. Level 0: quiet. Level 1: tool names + reasoning. Level 2: detailed with input summaries.

#### Scenario: User sets verbosity to 2
- **WHEN** a user runs /verbose 2
- **THEN** subsequent Claude executions show tool names with input summaries and longer reasoning text
