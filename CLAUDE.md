# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository contains two main integration components for Google Agent Assist with Genesys Cloud:

1. **Genesys AudioHook Backend** (`genesyscloud/genesyscloud-audiohook/`) - Processes real-time audio streams from Genesys Cloud via WebSocket and sends them to Dialogflow for speech-to-text and Agent Assist suggestions
2. **Agent Assist UI Connector** (`aa-integration-backend/ui-connector/`) - Flask/SocketIO proxy server that connects Agent Assist UI Modules to Dialogflow API, handles authentication, and manages real-time updates via Redis Pub/Sub

## Development Commands

### Genesys AudioHook Service
```bash
cd genesyscloud/genesyscloud-audiohook
pip install -r requirements.txt
python main.py
```

### UI Connector Service
```bash
cd aa-integration-backend/ui-connector
pip install -r requirements.txt
python main.py
```

Production deployment uses Gunicorn (see requirements.txt).

## Architecture

### Genesys AudioHook Flow
1. **WebSocket Connection** - Genesys Cloud connects via `/audiohook` endpoint with AudioHook protocol
2. **Audio Processing** - Incoming interleaved PCMU audio is separated into agent/customer streams
3. **Dialogflow Streaming** - Two parallel threads stream audio to Dialogflow v2beta1 `StreamingAnalyzeContent` API
4. **Conversation State** - Redis stores conversation state; handles pause/resume cycles
5. **Thread Management** - Critical: threads can only start once; create new Thread objects on resume if previous threads finished

### UI Connector Flow
1. **Authentication** - JWT-based auth via `/register` or `/register-app` endpoints
2. **Dialogflow Proxy** - Routes authenticated requests to Dialogflow API (supports location-specific endpoints)
3. **WebSocket Events** - SocketIO handles real-time events for Agent Assist UI Modules
4. **Redis Pub/Sub** - Publishes conversation updates to specific server instances via pattern `{SERVER_ID}:*`
5. **Room Management** - Conversations join/leave rooms by conversation name (location-stripped)

## Key Files

### Genesys AudioHook Service
- `main.py` - Flask app entry point, registers audiohook blueprint
- `audiohook_blueprint.py` - WebSocket handler at `/audiohook` endpoint
  - `process_open_conversation_message()` - Handles "open" message, creates conversation/participants
  - `process_ongoing_conversation_messages()` - Handles audio data and pause/resume
  - `OpenConversationState` - Stores thread references, participant objects, audio configs
- `dialogflow_api.py` - Dialogflow API wrapper
  - `DialogflowAPI.maintained_streaming_analyze_content()` - Main streaming loop with reconnection
  - `DialogflowAPI.streaming_analyze_content()` - Individual 60-second streaming sessions
- `audio_stream.py` - Audio buffering and conversion (PCMU to LINEAR16)
- `audiohook_config.py` - Configuration management
- `audiohook.py` - AudioHook protocol message parsing

### UI Connector Service
- `main.py` - Flask/SocketIO app with Redis Pub/Sub integration
  - Routes: `/`, `/status`, `/register`, `/register-app`, `/conversation-name`
  - Dialogflow proxy routes: `/<version>/projects/<project>/locations/<location>/...`
  - SocketIO events: `connect`, `disconnect`, `join-conversation`, `leave-conversation`
- `dialogflow.py` - Dialogflow API client wrapper
- `auth.py` - JWT token generation and validation
- `config.py` - Configuration (CORS, Redis, etc.)

## Critical Implementation Details

### Threading Constraints
**Problem**: `RuntimeError: threads can only be started once`

**Solution** (implemented in `audiohook_blueprint.py`):
- Store Thread objects in `OpenConversationState`
- Check `thread.is_alive()` before attempting to start
- Create NEW Thread objects if old ones finished (don't reuse/restart)

### Audio Format
- **Input**: 8-bit PCMU (μ-law) at 8000 Hz
- **Dialogflow**: LINEAR16 PCM at configurable sample rate (default 8000 Hz)
- Conversion handled in `audio_stream.py`

### Logging Hygiene
Excessive logging has been reduced by commenting out:
- Streaming session start/close logs (`dialogflow_api.py:219, 230`)
- Streaming API logs (`dialogflow_api.py:243, 247, 251`)
- WebSocket data reception logs (`audiohook_blueprint.py:238`)
- Audio processing logs (`audiohook_blueprint.py:302, 317`)

Avoid uncommenting these without good reason - they flood logs during normal operation.

### Conversation Naming
- Genesys conversation IDs are prefixed with 'a' to ensure valid Dialogflow resource names
- Location IDs are stripped from conversation names for Redis keys and SocketIO rooms
- Format: `projects/{project}/locations/{location}/conversations/{id}`

### Redis Usage
**AudioHook Service**: Stores conversation state for pause/resume handling

**UI Connector**:
- Stores `conversationIntegrationKey` → `conversationName` mappings (hashed with SHA256)
- Stores `conversationName` → `SERVER_ID` mappings for routing Pub/Sub messages
- Pub/Sub pattern: `{SERVER_ID}:*` ensures messages reach correct server instance

### Dialogflow API Endpoints
Location-based endpoints are automatically determined:
- `global` → `dialogflow.googleapis.com`
- Regional (e.g., `us-central1`) → `{location}-dialogflow.googleapis.com`

## Configuration Requirements

Both services require:
- **Google Cloud credentials** (Application Default Credentials)
- **Redis** instance
- **Environment variables** (see `audiohook_config.py` and `config.py`)

Key configs:
- `conversation_profile_name` - Dialogflow conversation profile resource name
- `redis_host`, `redis_port` - Redis connection details
- `log_level` - Logging verbosity

## Agent Assist UI Modules Integration

The UI Connector service is designed to work with Google's Agent Assist UI Modules (see README.md for full documentation). These are Web Components that render Agent Assist features:

- Smart Reply
- Knowledge Assist (Article Suggestion, FAQ, Article Search)
- Conversation Summarization

The connector handles authentication, proxies Dialogflow API calls, and manages real-time updates via SocketIO and Redis Pub/Sub.
