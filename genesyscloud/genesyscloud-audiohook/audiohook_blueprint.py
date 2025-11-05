# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""" Module for receiving audio streaming from Audiohook Monitor and
call Agent Assist backend
"""
import json
import logging
from dataclasses import dataclass, field
from threading import Thread
import base64

import numpy as np
from flask import Blueprint
from flask_sock import Sock
from google.api_core.exceptions import NotFound
from google.cloud import dialogflow_v2beta1 as dialogflow
from google.cloud import pubsub_v1
from simple_websocket import Server

from audio_stream import Stream
from audiohook import DEFAULT_CONVERSATION_ID, AudioHook
from audiohook_config import config
from dialogflow_api import (DialogflowAPI, await_redis, create_conversation_name,
                            find_participant_by_role, location_id, project, redis_client)

# Genesys Cloud SDK imports
import PureCloudPlatformClientV2
from PureCloudPlatformClientV2.rest import ApiException

audiohook_bp = Blueprint("audiohook", __name__)
sock = Sock(audiohook_bp)

# Initialize Pub/Sub publisher for summaries when no SERVER_ID
publisher = pubsub_v1.PublisherClient()
PUBSUB_TOPIC = f"projects/{project}/topics/dematic-aa-conversation-event-topic"


def get_genesys_conversation_details(conversation_id: str):
    """Fetch conversation details from Genesys Cloud API.

    Args:
        conversation_id: The Genesys conversation ID

    Returns:
        dict: Conversation details or None if fetch fails
    """
    try:
        # Configure Genesys Cloud region
        genesys_region = getattr(PureCloudPlatformClientV2.PureCloudRegionHosts, config.genesys_region, PureCloudPlatformClientV2.PureCloudRegionHosts.us_east_1)
        PureCloudPlatformClientV2.configuration.host = genesys_region.get_api_host()
        logging.info("🌍 Genesys Cloud Region: %s -> %s", config.genesys_region, PureCloudPlatformClientV2.configuration.host)

        # Get OAuth credentials from environment
        client_id = config.genesys_client_id
        client_secret = config.genesys_client_secret

        # Debug: Log credential status (NOT the actual values for security)
        logging.info("🔐 Genesys OAuth Credentials Check:")
        logging.info("  Client ID present: %s", bool(client_id))
        logging.info("  Client ID length: %s", len(client_id) if client_id else 0)
        logging.info("  Client Secret present: %s", bool(client_secret))
        logging.info("  Client Secret length: %s", len(client_secret) if client_secret else 0)

        if not client_id or not client_secret:
            logging.warning("⚠️ Genesys API credentials not configured - skipping conversation details fetch")
            return None

        # Authenticate
        logging.info("🔑 Attempting Genesys OAuth authentication...")
        api_client = PureCloudPlatformClientV2.api_client.ApiClient().get_client_credentials_token(
            client_id, client_secret
        )
        logging.info("✅ Genesys OAuth authentication successful")

        # Get conversation details
        conversations_api = PureCloudPlatformClientV2.ConversationsApi(api_client)
        conversation = conversations_api.get_conversation(conversation_id)

        # Log detailed participant information
        logging.info("=" * 80)
        logging.info("👥 GENESYS CONVERSATION DETAILS (via SDK):")
        logging.info("  Conversation ID: %s", conversation_id)

        if conversation.participants:
            for idx, participant in enumerate(conversation.participants):
                logging.info("  Participant %d:", idx + 1)
                logging.info("    - ID: %s", participant.id)
                logging.info("    - Purpose: %s", participant.purpose)
                logging.info("    - Name: %s", getattr(participant, 'name', 'N/A'))
                logging.info("    - User ID: %s", getattr(participant, 'user_id', 'N/A'))
                logging.info("    - Queue: %s", getattr(participant, 'queue_name', 'N/A'))
                logging.info("    - Attributes: %s", getattr(participant, 'attributes', {}))

        logging.info("  Conversation Attributes: %s", getattr(conversation, 'attributes', {}))
        logging.info("=" * 80)

        return conversation

    except ApiException as e:
        logging.error("❌ Genesys API error fetching conversation: %s", e)
        return None
    except Exception as e:
        logging.error("❌ Error fetching Genesys conversation details: %s", e)
        return None


logging.getLogger()
logging.basicConfig(
    format='%(levelname)-8s [%(filename)s:%(lineno)d in '
           'function %(funcName)s] %(message)s',
    datefmt='%Y-%m-%d:%H:%M:%S',
    level=config.log_level.upper()
)


@dataclass
class OpenConversationState:
    """ Memorize the state after open message that is not a connection prob
    """
    agent_thread: Thread
    user_thread: Thread
    conversation_name: str
    is_opened: bool
    participant_agent: object
    participant_user: object
    agent_audio_config: object
    customer_audio_config: object
    stop_summary: bool = field(default=False)
    summary_thread: Thread = field(default=None)
    websocket: object = field(default=None)


def periodic_conversation_summary(
        conversation_name: str,
        dialogflow_api: DialogflowAPI,
        open_conversation_state: OpenConversationState,
        interval_seconds: int = 60):
    """Generate conversation summary periodically and publish to Redis

    Args:
        conversation_name: Full Dialogflow conversation resource name
        dialogflow_api: DialogflowAPI instance
        open_conversation_state: State object to check stop_summary flag
        interval_seconds: How often to generate summary (default 60 seconds)
    """
    import time
    from datetime import datetime

    logging.info("📊 Starting periodic summarization thread for %s (interval: %d seconds)",
                conversation_name, interval_seconds)

    # Strip location from conversation name for Redis room matching
    conversation_name_without_location = conversation_name.replace('/locations/global', '')
    summary_count = 0

    while not open_conversation_state.stop_summary:
        time.sleep(interval_seconds)

        if open_conversation_state.stop_summary:
            break

        summary_count += 1
        logging.info("⏰ Triggering periodic conversation summary #%d for %s",
                    summary_count, conversation_name)

        summary_response = dialogflow_api.suggest_conversation_summary(conversation_name)

        if summary_response and summary_response.summary:
            summary_text = summary_response.summary.text
            logging.info("📝 Summary #%d generated (%d chars): %s",
                       summary_count, len(summary_text), summary_text[:200])

            # Extract Genesys conversation ID from conversation name
            # Format: projects/{project}/conversations/a{genesys_id}
            conversation_id = conversation_name_without_location.split('/')[-1]
            if conversation_id.startswith('a'):
                genesys_conversation_id = conversation_id[1:]  # Remove 'a' prefix
            else:
                genesys_conversation_id = conversation_id

            # Prepare summary event payload matching Dialogflow format
            summary_event = {
                'conversationName': conversation_name,
                'genesysConversationId': genesys_conversation_id,
                'summary': summary_text,
                'summaryCount': summary_count
            }

            # Get the SERVER_ID for this conversation from Redis
            server_id = redis_client.get(conversation_name_without_location)
            if server_id:
                # Publish to Redis for UI Connector
                server_id = str(server_id, encoding='utf-8')
                channel = f"{server_id}:{conversation_name_without_location}"

                message = {
                    'conversation_name': conversation_name_without_location,
                    'genesys_conversation_id': genesys_conversation_id,
                    'data': json.dumps({
                        'conversationName': conversation_name,
                        'genesysConversationId': genesys_conversation_id,
                        'payload': {
                            'summary': {
                                'text': summary_text,
                                'textSections': {}
                            }
                        }
                    }),
                    'data_type': 'conversation-summarization-received'
                }

                redis_client.publish(channel, json.dumps(message))
                logging.info("✅ Published summary #%d to Redis channel: %s", summary_count, channel)
            else:
                # No SERVER_ID - publish to Pub/Sub for test apps
                logging.info("📤 No SERVER_ID, publishing summary #%d to Pub/Sub for conversation: %s",
                           summary_count, conversation_name_without_location)
                try:
                    message_data = json.dumps(summary_event).encode('utf-8')
                    future = publisher.publish(PUBSUB_TOPIC, message_data)
                    future.result()  # Wait for publish to complete
                    logging.info("✅ Published summary #%d to Pub/Sub topic: %s",
                               summary_count, PUBSUB_TOPIC)
                except Exception as e:
                    logging.error("❌ Failed to publish summary to Pub/Sub: %s", e)
        else:
            logging.warning("⚠️ No summary generated for %s", conversation_name)

    logging.info("🛑 Stopped periodic summarization for %s", conversation_name)


def process_open_conversation_message(
        conversation_id: str,
        dialogflow_api: DialogflowAPI,
        agent_stream: Stream,
        customer_stream: Stream,
        ws: Server,
        audiohook: AudioHook
) -> OpenConversationState:
    """Process "open" message get from Audiohook Monitor, and establish a state
    object for conversation_name, agent_thread, user_thread, and is_opened bool
    """

    conversation_profile = dialogflow_api.get_conversation_profile(
        conversation_profile_name=config.conversation_profile_name)
    agent_audio_config = agent_stream.define_audio_config(conversation_profile)
    customer_audio_config = customer_stream.define_audio_config(
        conversation_profile)
    normalized_conversation_id = 'a' + conversation_id
    conversation_name = create_conversation_name(
        normalized_conversation_id, location_id, project)
    try:
        dialogflow_api.get_conversation(
            conversation_name)
    except NotFound as e:
        logging.info("Conversation not found, creating new conversation: %s", normalized_conversation_id)
        dialogflow_api.create_conversation(
            conversation_profile, normalized_conversation_id)

    try:
        participants_list = dialogflow_api.list_participant(
            conversation_name)
        participant_agent = find_participant_by_role(
            dialogflow.Participant.Role.HUMAN_AGENT, participants_list)
        participant_user = find_participant_by_role(
            dialogflow.Participant.Role.END_USER, participants_list)
        if not participant_agent:
            participant_agent = dialogflow_api.create_participant(
                conversation_name=conversation_name, role="HUMAN_AGENT")
        if not participant_user:
            participant_user = dialogflow_api.create_participant(
                conversation_name=conversation_name, role="END_USER")
    except NotFound as e:
        logging.error("Participants not found %s: ", e)

    agent_thread = Thread(
        target=dialogflow_api.maintained_streaming_analyze_content, args=(
            agent_stream, participant_agent, agent_audio_config))
    user_thread = Thread(
        target=dialogflow_api.maintained_streaming_analyze_content, args=(
            customer_stream, participant_user, customer_audio_config))

    logging.info("🚀 Starting speech-to-text threads for conversation: %s", conversation_name)
    logging.info("🤵 Agent participant: %s", participant_agent.name)
    logging.info("👤 User participant: %s", participant_user.name)

    # Start threads immediately since startPaused is False
    agent_thread.start()
    user_thread.start()
    logging.info("✅ Speech-to-text threads started successfully")

    # Create OpenConversationState first
    open_conversation_state = OpenConversationState(
        agent_thread,
        user_thread,
        conversation_name,
        True,
        participant_agent,
        participant_user,
        agent_audio_config,
        customer_audio_config)

    # Start periodic conversation summary thread (1 minute interval for testing)
    summary_thread = Thread(
        target=periodic_conversation_summary,
        args=(conversation_name, dialogflow_api, open_conversation_state, 60)
    )
    summary_thread.start()
    open_conversation_state.summary_thread = summary_thread
    logging.info("✅ Periodic summary thread started (60 second interval)")

    ws.send(json.dumps(audiohook.create_opened_message()))
    return open_conversation_state


def process_ongoing_conversation_messages(
        message: json,
        dialogflow_api: DialogflowAPI,
        audiohook: AudioHook,
        agent_stream: Stream,
        customer_stream: Stream,
        open_conversation_state: OpenConversationState,
        ws: Server) -> bool:
    """Process string messages that are not "open" and "ping" from Audiohook client through websocket.

    Note:
        Audiohook client passes Null UUIDs (00000000-0000-0000-0000-000000000000)
        as conversationId and participant.id parameters to identify connection probes.
        https://developer.genesys.cloud/devapps/audiohook/protocol-reference#openparameters

    Reference: https://developer.genesys.cloud/devapps/audiohook/protocol-reference

    Return:
        True, if there is a connection close message. So the outer loop for
        receiving audio can be completed
        False, for other messages to continue process messages from websocket
    """
    message_type = message.get("type")
    match message_type:
        case "resumed":
            # The first paused message after open message sets the
            # closed to True, now after resume, need to flip the bit
            customer_stream.closed = False
            agent_stream.closed = False

            # Only start threads if they're not already running
            if not open_conversation_state.agent_thread.is_alive():
                # Create new thread if the old one finished or was never started
                open_conversation_state.agent_thread = Thread(
                    target=dialogflow_api.maintained_streaming_analyze_content,
                    args=(agent_stream, open_conversation_state.participant_agent, open_conversation_state.agent_audio_config))
                open_conversation_state.agent_thread.start()

            if not open_conversation_state.user_thread.is_alive():
                # Create new thread if the old one finished or was never started
                open_conversation_state.user_thread = Thread(
                    target=dialogflow_api.maintained_streaming_analyze_content,
                    args=(customer_stream, open_conversation_state.participant_user, open_conversation_state.customer_audio_config))
                open_conversation_state.user_thread.start()
        case "paused":
            customer_stream.closed = True
            agent_stream.closed = True
            logging.debug("Audio stream is paused")
        case "close":
            # This "close" is for ending a real conversation
            agent_stream.closed = True
            customer_stream.closed = True
            agent_stream.terminate = True
            customer_stream.terminate = True

            # Stop periodic summary thread
            if open_conversation_state.summary_thread:
                open_conversation_state.stop_summary = True
                logging.info("🛑 Stopping periodic summary thread")

            ws.send(json.dumps(audiohook.create_close_message()))
            try:
                dialogflow_api.complete_conversation(
                    open_conversation_state.conversation_name)
            except Exception as e:
                logging.error("Error completing conversation %s", e)
            # wait for the two thread to finish then terminate
            logging.debug("Stop streaming threads for customers and agents")
            return True
        case "discarded":
            start_time = message.get("START_TIME")
            duration = message.get("DURATION")
            logging.info(
                "Currently the audio stream has been paused from %s for about %s second",
                start_time,
                duration)
    return False


def wait_for_redis_resume(open_conversation_state: OpenConversationState,
                          audiohook: AudioHook, ws: Server):
    await_redis(
            open_conversation_state.conversation_name)
    # Always send the resume after awaiting the redis, don't stop the audio streaming
    # event if redis client is not set
    try:
        ws.send(json.dumps(audiohook.create_resume_message()))
    except Exception as e:
        logging.warning("Could not send resume message, WebSocket may be closed: %s", e)


@sock.route('/connect')
def audiohook_connect(ws: Server):
    """Genesys Cloud Audiohook connector

    Args:
        ws (Server): Websocket server for exchange messages
    """
    agent_stream = Stream(
        config.rate, chunk_size=config.chunk_size)
    customer_stream = Stream(
        config.rate, chunk_size=config.chunk_size)

    dialogflow_api = DialogflowAPI()
    audiohook = AudioHook()
    logging.info(
        "Audiohook client connected with the interceptor server")
    open_conversation_state = None
    while True:
        data = ws.receive()
        data_type = type(data).__name__
        data_size = len(data) if hasattr(data, '__len__') else 'N/A'
        # logging.info("🔌 WebSocket received data - type: %s, size: %s bytes", data_type, data_size)
        if isinstance(data, str):
            logging.info("📝 Processing WebSocket string message")
            try:
                json_message = json.loads(data)
            except ValueError as e:
                logging.warning(
                    "Not a valid JSON message %s, error details %s ", data, e)
                continue
            message_type = json_message.get("type")
            logging.info(
                "📨 Handle %s message: %s", message_type, json_message)
            conversation_id = json_message.get("parameters", {}).get(
                "conversationId", DEFAULT_CONVERSATION_ID)

            # Log all Genesys conversation/participant data
            logging.info("=" * 80)
            logging.info("📞 GENESYS AUDIOHOOK DATA:")
            logging.info("  Message Type: %s", message_type)
            logging.info("  Conversation ID: %s", conversation_id)
            logging.info("  Session ID: %s", json_message.get("id", 0))
            logging.info("  Sequence: %s", json_message.get("seq"))
            logging.info("  Parameters: %s", json.dumps(json_message.get("parameters", {}), indent=2))
            logging.info("  Full Message: %s", json.dumps(json_message, indent=2))
            logging.info("=" * 80)

            audiohook.set_session_id(json_message.get("id", 0))
            audiohook.set_client_sequence(json_message.get("seq"))
            if message_type == "open":
                if conversation_id == DEFAULT_CONVERSATION_ID:
                    logging.debug(
                        "Connection Probe, not creating Dialogflow Conversation")
                    ws.send(json.dumps(audiohook.create_opened_message()))
                elif conversation_id != DEFAULT_CONVERSATION_ID and open_conversation_state is None:
                    # Fetch Genesys conversation details via SDK
                    # Commented out: we already have conversation ID, don't need additional details
                    # get_genesys_conversation_details(conversation_id)
                    # Get the first "open" message for real conversation
                    # open_state contains the agent and user thread for
                    # calling streaming_analyze_content
                    # a bool flag indicating if conversation, participants have been initialized
                    # and the conversation_name for the dialogflow.Conversation object
                    open_conversation_state = process_open_conversation_message(
                        conversation_id,
                        dialogflow_api,
                        agent_stream,
                        customer_stream,
                        ws,
                        audiohook,
                    )
                    logging.debug(
                        "open conversation message %s ", open_conversation_state)
                    # Check if the redis client has join_room called from the
                    # agent assist backend. Before setting conversation_name in the redis client,
                    # we should not publish any messages to the redis client
                    # otherwise e UI modules will not receive pub/subs until redis connects the conversation
                    await_redis_thread = Thread(target=wait_for_redis_resume, args=(
                        open_conversation_state, audiohook, ws))
                    await_redis_thread.start()
            elif message_type == "ping":
                ws.send(json.dumps(audiohook.create_pong_message()))
            elif message_type == "close" and open_conversation_state is None:
                # This "close" is for a connection prob, we don't need to call dialogflow
                # to complete conversation and terminate the stream in this case
                ws.send(json.dumps(audiohook.create_close_message()))
                break
            elif open_conversation_state is not None:
                # Close websocket connection when receive a "close message"
                if (process_ongoing_conversation_messages(json_message,
                                                          dialogflow_api,
                                                          audiohook,
                                                          agent_stream,
                                                          customer_stream,
                                                          open_conversation_state, ws,
                                                          )):
                    logging.info(
                        "Disconnecting Audiohook with the server")
                    break
        else:
            # logging.info("🎵 Processing WebSocket binary audio data - size: %s bytes", len(data))
            # audio is a 2-channel interleaved 8-bit PCMU audio stream
            # which is separated into single streams
            # using numpy
            # stream the audio to pub/sub
            if open_conversation_state is not None:
                array = np.frombuffer(data, dtype=np.int8)
                reshaped = array.reshape(
                    (int(len(array) / 2), 2))

                # Log audio data statistics
                audio_chunk_size = len(data)
                customer_audio_size = len(reshaped[:, 0].tobytes())
                agent_audio_size = len(reshaped[:, 1].tobytes())

                # logging.info("🎧 Audio chunk processed - total: %s bytes, customer: %s bytes, agent: %s bytes",
                #             audio_chunk_size, customer_audio_size, agent_audio_size)

                # append audio to customer audio buffer
                customer_stream.fill_buffer(reshaped[:, 0].tobytes())
                # append audio to agent audio buffer
                agent_stream.fill_buffer(reshaped[:, 1].tobytes())

                # Log buffer states every 100 chunks to avoid log spam
                if hasattr(customer_stream, '_chunk_count'):
                    customer_stream._chunk_count += 1
                    agent_stream._chunk_count += 1
                else:
                    customer_stream._chunk_count = 1
                    agent_stream._chunk_count = 1

                if customer_stream._chunk_count % 100 == 0:
                    logging.info("Audio streaming progress - processed %s chunks for conversation: %s",
                               customer_stream._chunk_count, open_conversation_state.conversation_name)
            else:
                logging.warning("❌ Received audio data but no open conversation state - ignoring %s bytes", len(data))
