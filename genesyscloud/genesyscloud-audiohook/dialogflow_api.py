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

"""Module for interacting with Agent Assist Backend using
dialogflow_v2beta1 API version
Reference: https://cloud.google.com/python/docs/reference/dialogflow/latest/google.cloud.dialogflow_v2beta1
"""
import logging
import re
import time

import google.auth
import redis
from google.api_core.client_options import ClientOptions
from google.api_core.exceptions import FailedPrecondition, OutOfRange, ResourceExhausted
from google.cloud import dialogflow_v2beta1 as dialogflow

from audio_stream import Stream
from audiohook_config import config

# Wait for 2 units of 0.5 second for the redis client to set conversation name
AWAIT_REDIS_COUNTER = 2
AWAIT_REDIS_SECOND_PER_COUNTER = 0.5
LOCATION_ID_REGEX = r"^projects\/[^/]+\/locations\/([^/]+)"

credentials, project = google.auth.default()
redis_client = redis.StrictRedis(
    host=config.redis_host, port=config.redis_port)


try:
    location_id = re.match(
        LOCATION_ID_REGEX, config.conversation_profile_name)[1]
except Exception as e:
    raise ValueError(
        "Conversation profile name is not in correct format") from e


def determine_dialogflow_api_endpoint(location: str) -> str:
    """Get Dialogflow api endpoint
    Reference: https://cloud.google.com/dialogflow/es/docs/reference/rest/v2-overview#service-endpoint
    """
    dialogflow_endpoint = "dialogflow.googleapis.com"
    if location != 'global':
        dialogflow_endpoint = f"{location}-dialogflow.googleapis.com"
    logging.debug("Dialogflow api endpoint %s", dialogflow_endpoint)
    return dialogflow_endpoint


def create_conversation_name(conversation_id: str, location_id: str, project: str) -> str:
    """Set conversation name for the object
    """
    return f"projects/{project}/locations/{location_id}/conversations/{conversation_id}"


def find_participant_by_role(role: dialogflow.Participant.Role, participants_list: list[dialogflow.Participant]) -> dialogflow.Participant | None:

    for participant in participants_list:
        if participant.role == role:

            logging.debug("the active participant is %s:%s ",
                          participant.role, participant.name)
            return participant

    return None


class DialogflowAPI:
    """Class for interacting with the Dialogflow API
    """

    def __init__(self) -> None:

        self.api_endpoint = determine_dialogflow_api_endpoint(
            location_id)
        self.participants_client = dialogflow.ParticipantsClient(
            credentials=credentials,
            client_options=ClientOptions(
                api_endpoint=self.api_endpoint)
        )
        self.conversations_client = dialogflow.ConversationsClient(
            credentials=credentials,
            client_options=ClientOptions(
                api_endpoint=self.api_endpoint
            )
        )
        self.conversation_profiles_client = dialogflow.ConversationProfilesClient(
            credentials=credentials, client_options=ClientOptions(
                api_endpoint=self.api_endpoint))

    def get_conversation_profile(
            self,
            conversation_profile_name: str) -> dialogflow.ConversationProfile:
        """Load conversation profile
        """
        logging.debug("Getting conversation profile for %s ",
                      conversation_profile_name)
        return self.conversation_profiles_client.get_conversation_profile(
            request=dialogflow.GetConversationProfileRequest(
                name=conversation_profile_name
            )
        )

    def create_conversation(
            self,
            conversation_profile: dialogflow.ConversationProfile,
            conversation_id: str,
    ) -> dialogflow.Conversation:
        """Create conversation using conversation_id
        """
        logging.info("Creating new Dialogflow conversation with ID: %s", conversation_id)
        logging.info("Using conversation profile: %s", conversation_profile.name)

        conversation = dialogflow.Conversation(
            conversation_profile=conversation_profile.name
        )
        project_path = self.conversations_client.common_location_path(
            project, location_id)

        logging.info("Project path for conversation: %s", project_path)
        logging.info("Location ID: %s, Project: %s", location_id, project)

        conversation_request = dialogflow.CreateConversationRequest(
            parent=project_path,
            conversation=conversation,
            conversation_id=conversation_id,
        )

        logging.info("Making CreateConversation API call...")
        conversation = self.conversations_client.create_conversation(
            request=conversation_request)

        logging.info(
            "✅ Successfully created conversation: %s", conversation.name)
        logging.info("Conversation state: %s", conversation.lifecycle_state)
        logging.info("Conversation start time: %s", conversation.start_time)

        return conversation

    def get_conversation(
            self, conversation_name: str) -> dialogflow.Conversation:
        """Get conversation using the conversation_name from dialogflow
        """

        get_conversation_request = dialogflow.GetConversationRequest(
            name=conversation_name,
        )

        conversation = self.conversations_client.get_conversation(
            request=get_conversation_request)

        return conversation

    def list_participant(self,
                         conversation_name: str) -> list[dialogflow.Participant]:
        """List existing participant for Human agent and End user

        Args:
            conversation_name (str): conversation name

        Returns:
            _type_: _description_
        """
        participants_pagers = self.participants_client.list_participants(
            dialogflow.ListParticipantsRequest(parent=conversation_name))
        participant_list = list(participants_pagers.__iter__())
        logging.debug("participant list %s, type ", participant_list)
        return participant_list

    def create_participant(
            self, conversation_name: str,
            role: str):
        """Create both the agent and customer participant for the conversation

        Args:
            conversation_name (str): full conversation name path
            role (str): participant role (HUMAN_AGENT or END_USER)
        """
        logging.info("Creating participant with role: %s for conversation: %s", role, conversation_name)

        participant = dialogflow.Participant()
        participant.role = role

        logging.info("Making CreateParticipant API call for role: %s", role)
        participant = self.participants_client.create_participant(
            parent=conversation_name, participant=participant)

        logging.info("✅ Successfully created participant: %s (role: %s)",
                      participant.name, participant.role.name)

        return participant

    def maintained_streaming_analyze_content(
            self,
            audio_stream: Stream,
            participant: dialogflow.Participant,
            audio_config: dialogflow.InputAudioConfig):
        """While the stream is not closed or terminated, maintain a steady call to streaming
        analyze content API endpoint
        """
        logging.info("Starting maintained streaming analyze content for participant: %s", participant.name)
        logging.info("Audio config - sample rate: %s, encoding: %s",
                    audio_config.sample_rate_hertz, audio_config.audio_encoding)

        stream_session_count = 0
        while not audio_stream.terminate:
            stream_session_count += 1
            # logging.debug("Starting streaming session #%s for participant: %s",
            #             stream_session_count, participant.name)

            while not audio_stream.closed:
                logging.debug("Calling streaming_analyze_content for %s (session #%s)",
                            participant.name, stream_session_count)
                self.streaming_analyze_content(
                    audio_stream,
                    participant,
                    audio_config)

            # logging.debug("Audio stream closed for participant: %s (session #%s)",
            #             participant.name, stream_session_count)

        logging.info("Terminating streaming analyze content for participant: %s", participant.name)

    def streaming_analyze_content(
            self,
            audio_stream: Stream,
            participant: dialogflow.Participant,
            audio_config: dialogflow.InputAudioConfig):
        """Call dialogflow backend StreamingAnalyzeContent endpoint,
        and send the audio binary stream from Audiohook.
        """
        # logging.info("Initiating streaming analyze content for participant: %s", participant.name)
        logging.debug("Participant role: %s", participant.role.name)

        try:
            # logging.info("Making streaming_analyze_content API call for %s", participant.name)
            responses = self.participants_client.streaming_analyze_content(
                requests=self.generator_streaming_analyze_content_request(
                    audio_config, participant, audio_stream))
            # logging.info("Successfully established streaming connection for %s", participant.name)
        except OutOfRange as e:
            audio_stream.closed = True
            logging.error(
                "OutOfRange error - audio stream exceeded 120 seconds for %s: %s",
                participant.name, e)
            return
        except FailedPrecondition as e:
            audio_stream.closed = True
            logging.error(
                "FailedPrecondition error for StreamingAnalyzeContent %s: %s",
                participant.name, e)
            return
        except ResourceExhausted as e:
            audio_stream.closed = True
            logging.error(
                "ResourceExhausted error - quota exceeded for %s: %s",
                participant.name, e)
            return
        except Exception as e:
            audio_stream.closed = True
            logging.error(
                "Unexpected error in streaming_analyze_content for %s: %s",
                participant.name, e)
            return

        response_count = 0
        for response in responses:
            response_count += 1
            logging.debug("Processing response #%s for %s", response_count, participant.name)

            # Skip processing if the response contains empty recognition results
            if hasattr(response, 'recognition_result') and response.recognition_result:
                transcript = response.recognition_result.transcript.strip()
                if not transcript or len(transcript) < 2:
                    logging.debug("Skipping empty/minimal recognition result for %s: '%s'", participant.name, transcript)
                    continue

            # Log response structure
            if hasattr(response, 'recognition_result') and response.recognition_result:
                recognition_result = response.recognition_result
                audio_stream.speech_end_offset = recognition_result.speech_end_offset.seconds * 1000

                # Use the already validated transcript
                transcript = recognition_result.transcript.strip()

                if recognition_result.is_final:
                    audio_stream.is_final = True
                    offset = recognition_result.speech_end_offset
                    audio_stream.is_final_offset = int(
                        offset.seconds * 1000 + offset.microseconds / 1000
                    )
                    logging.info(
                        "FINAL TRANSCRIPT - %s (%s): '%s' [confidence: %s, end_offset: %sms]",
                        participant.role.name,
                        participant.name,
                        transcript,
                        getattr(recognition_result, 'confidence', 'N/A'),
                        audio_stream.is_final_offset
                    )
                else:
                    logging.info(
                        "INTERIM TRANSCRIPT - %s (%s): '%s' [end_offset: %sms]",
                        participant.role.name,
                        participant.name,
                        transcript,
                        recognition_result.speech_end_offset.seconds * 1000
                    )

            # Log other response types
            if hasattr(response, 'automated_agent_reply') and response.automated_agent_reply:
                logging.info("Automated agent reply received for %s", participant.name)

            if hasattr(response, 'message') and response.message:
                logging.info("Message received for %s", participant.name)

            logging.debug("Full response for %s: %s", participant.name, response)

    def complete_conversation(self, conversation_name: str):
        """Send complete conversation request to Dialogflow
        """
        self.conversations_client.complete_conversation(
            name=conversation_name
        )
        logging.debug("Call complete conversation for %s", conversation_name)

    def generator_streaming_analyze_content_request(
            self,
            audio_config: dialogflow.InputAudioConfig,
            participant: dialogflow.Participant,
            audio_stream: Stream):
        """Generates and return an iterator for StreamingAnalyzeContentRequest,
        The first request should only include the input_audio_config
        And the following request contains the audio chunks as input_audio.
        The last request does not have any input_audio or input_audio_config and indicates that
        the server side is half-closing the streaming

        Args:
            audio_config (dialogflow.InputAudioConfig): Input for Speech Recognizer
            https://cloud.google.com/dialogflow/es/docs/reference/rest/v2beta1/InputAudioConfig
            participant (dialogflow.Participant): Participant for the Dialogflow API call
            audio_queue (asyncio.Queue): Queue to store the audio binary stream

        Yields:
            _type_: first filed the audio config, and then yield the binary data.
        """
        # Sending audio_config for participant
        enable_debugging_info = config.log_level.upper() == "DEBUG"
        generator = audio_stream.generator()
        yield dialogflow.StreamingAnalyzeContentRequest(
            participant=participant.name,
            audio_config=audio_config,
            enable_debugging_info=enable_debugging_info,
        )

        for content in generator:
            # next audio chunks to streaming_analyze_content
            yield dialogflow.StreamingAnalyzeContentRequest(
                input_audio=content,
                enable_debugging_info=enable_debugging_info,
            )

        logging.info(
            "Participant: %s, streaming analyze content request, end streaming yield an empty request ",
            participant.name)
        yield dialogflow.StreamingAnalyzeContentRequest(
            enable_debugging_info=enable_debugging_info,
        )
        logging.debug(
            "Ending the current audio stream session, start new session")


def await_redis(conversation_name: str) -> bool:
    """Check if the redis memory store connection has been established or not
    """
    conversation_name = determine_conversation_name_without_location(
        conversation_name)
    # give the user bit time to accept the call and wait for the Agent Assist Backend
    # to create the redis memory store
    counter = AWAIT_REDIS_COUNTER

    redis_exists = redis_client.exists(conversation_name) != 0
    while not redis_exists and counter > 0:
        time.sleep(AWAIT_REDIS_SECOND_PER_COUNTER)
        redis_exists = redis_client.exists(conversation_name) != 0
        counter = counter - 1
    logging.debug("return to send resume message redis client exist %s and final counter %s ",
                  redis_exists, counter)
    return redis_exists


def determine_conversation_name_without_location(conversation_name: str):
    """Returns a conversation name without its location id."""
    conversation_name_without_location = conversation_name
    if '/locations/' in conversation_name:
        name_array = conversation_name.split('/')
        conversation_name_without_location = '/'.join(
            name_array[i] for i in [0, 1, -2, -1])
    return conversation_name_without_location
