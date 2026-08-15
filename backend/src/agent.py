import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from dotenv import load_dotenv
from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    RunContext,
    cli,
    function_tool,
    room_io,
    tokenize,
)
from livekit.plugins import deepgram, google, murf, noise_cancellation, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from caller_memory import CallerMemoryStore

logger = logging.getLogger("agent")

load_dotenv(".env.local")

AGENT_NAME = "Aarogya Sahayak"
GREETING = (
    "Hello! I'm Aarogya Sahayak, your Health Access voice assistant. "
    "I can help explain general health information, healthy habits, and guide you "
    "to the right healthcare services. How may I help you today?"
)
EMERGENCY_RESPONSE = (
    "This could be a medical emergency. Please contact your nearest hospital or "
    "emergency services immediately."
)
MEDICINE_REFUSAL = (
    "I'm not able to prescribe medicines. Please consult a qualified healthcare "
    "professional."
)
HANDOFF_ANNOUNCEMENT = "I'll connect you with our clinic and appointment specialist."
SPECIALIST_INTRODUCTION = (
    "Hello, I'm the clinic and appointment specialist. "
    "I can help you with your appointment request."
)

OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PHONE_NUMBER_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")
ROMANIZED_HINDI_MARKERS = frozenset(
    {"aap", "hai", "hain", "ka", "ki", "kya", "main", "mujhe", "nahi", "se"}
)


def is_valid_phone_number(phone_number: object) -> bool:
    """Return whether a value is an E.164 number safe to pass to a SIP trunk."""
    return isinstance(phone_number, str) and bool(
        PHONE_NUMBER_PATTERN.fullmatch(phone_number)
    )


def mask_phone_number(phone_number: str) -> str:
    """Keep phone numbers useful for operational logs without exposing them."""
    if len(phone_number) <= 5:
        return "***"
    return f"{phone_number[:3]}{'*' * (len(phone_number) - 5)}{phone_number[-2:]}"


def get_outbound_phone_number(metadata: str) -> str | None:
    """Read and validate the optional outbound number from agent-dispatch metadata."""
    try:
        dial_info = json.loads(metadata or "{}")
    except json.JSONDecodeError:
        logger.warning("Outbound call metadata was not valid JSON")
        return None

    phone_number = (
        dial_info.get("phone_number") if isinstance(dial_info, dict) else None
    )
    if phone_number is not None and not is_valid_phone_number(phone_number):
        logger.warning(
            "Outbound call rejected because the destination number is invalid"
        )
        return None
    return phone_number


def add_current_language_instruction(turn_ctx, caller_message: str) -> None:
    """Put the latest language choice next to the caller turn for reliable routing."""
    words = set(re.findall(r"[a-z]+", caller_message.lower()))
    if words & ROMANIZED_HINDI_MARKERS:
        instruction = (
            "The caller is using Hindi-English code-mix. Reply in natural Hindi-English, "
            "writing every Hindi word in Devanagari and retaining only natural English terms."
        )
    elif caller_message.isascii():
        instruction = (
            "The caller's newest message is English-only. Reply only in concise English; "
            "do not use Hindi, Devanagari, Telugu, or another Indian language."
        )
    else:
        return

    turn_ctx.add_message(role="system", content=instruction)


def _get_json(url: str) -> dict:
    """Fetch a JSON response with a short timeout suitable for voice calls."""
    with urlopen(url, timeout=8) as response:
        return json.load(response)


async def fetch_health_weather(location: str) -> dict[str, object]:
    """Return current live weather for health guidance, or a safe error payload."""
    clean_location = location.strip()
    if not clean_location:
        return {"status": "unavailable", "reason": "No location was provided."}

    try:
        geocoding_query = urlencode({"name": clean_location, "count": 1})
        geocoding = await asyncio.to_thread(
            _get_json, f"{OPEN_METEO_GEOCODING_URL}?{geocoding_query}"
        )
        matches = geocoding.get("results", [])
        if not matches:
            return {
                "status": "unavailable",
                "reason": f"I could not find a location matching '{clean_location}'.",
            }

        place = matches[0]
        forecast_query = urlencode(
            {
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                "precipitation,weather_code",
                "timezone": "auto",
            }
        )
        forecast = await asyncio.to_thread(
            _get_json, f"{OPEN_METEO_FORECAST_URL}?{forecast_query}"
        )
        current = forecast.get("current")
        if not current:
            return {
                "status": "unavailable",
                "reason": "The weather service returned no current conditions.",
            }

        return {
            "status": "ok",
            "source": "Open-Meteo live forecast",
            "location": ", ".join(
                part
                for part in (
                    place.get("name"),
                    place.get("admin1"),
                    place.get("country"),
                )
                if part
            ),
            "observed_at": current.get("time"),
            "timezone": forecast.get("timezone"),
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            "temperature_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "humidity_percent": current.get("relative_humidity_2m"),
            "precipitation_mm": current.get("precipitation"),
            "weather_code": current.get("weather_code"),
        }
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, KeyError) as error:
        logger.warning("Live weather lookup failed for %s: %s", clean_location, error)
        return {
            "status": "unavailable",
            "reason": "The live weather service is temporarily unavailable.",
        }


# Change this prompt to change what the voice agent does.
SYSTEM_PROMPT = f"""You are {AGENT_NAME}, an AI Health Access Assistant. You are not a
doctor and do not replace professional medical care.

Purpose:
- Help users understand general health information, healthy habits, hospital services,
  appointment guidance, preventive care, vaccinations, nutrition, and when to seek
  medical help.
- Explain common topics simply: fever, cold, cough, hydration, healthy food, exercise,
  vaccination awareness, preventive healthcare, hospital departments, and appointments.
- Encourage consultation with a qualified healthcare professional when needed.

Clinic and appointment handoff:
- Handle general Health Access questions yourself. Do not hand off general health
  information, healthy habits, preventive care, weather guidance, memory requests,
  safety triage, or human-escalation requests.
- Use handoff_to_clinic_appointment_specialist only when the caller specifically asks
  to book, find, schedule, change, cancel, or understand a clinic or doctor appointment.
- Before calling that tool, clearly tell an English-speaking caller exactly:
  {HANDOFF_ANNOUNCEMENT}
  For another language, give the same clear announcement in that caller's language and
  native script. In English, say the required sentence as a standalone sentence with no
  added words. Do not handle the appointment workflow yourself after announcing it.

Greeting:
- For a simple first-turn greeting such as "Hello", use this greeting for a new caller:
  {GREETING}
- If the caller's first turn contains a health question, symptom, or request, answer it
  directly instead of giving the generic English greeting first. Match that caller's
  language and register in the direct response.
- Never call lookup_caller_memory during the first user turn. The first response must
  be a spoken/text greeting, with no function call before it. On a later turn, use
  lookup_caller_memory when personalisation is useful; if the caller is known, welcome
  them back by name and use only their saved, high-level information.

Live health-weather lookup:
- Call lookup_health_weather whenever a user asks about current weather, heat, rain,
  humidity, or weather-related health precautions for a named place. Do not estimate
  current conditions from general knowledge.
- When the tool returns status "ok", naturally state the location, the reported local
  observation time, and the relevant conditions before giving brief general health
  precautions. Do not read JSON or weather codes aloud.
- When it returns status "unavailable", say that live conditions could not be checked
  right now, do not invent a forecast, and give only general, non-location-specific
  precautions if useful.

Caller memory and consent:
- lookup_caller_memory and save_caller_memory are the only sources of durable caller
  data. Never pretend to remember information that was not returned by the lookup tool.
- Before calling save_caller_memory, clearly explain the limited details you intend to
  save and ask for explicit permission. Only call it after an unambiguous yes.
- If the caller says no, do not call the save tool, do not retain the details, and
  continue helping them normally.
- This is a hard rule: do not store free-form medical notes, symptoms, reports,
  medications, account numbers, identity-document numbers, or contact information.
- For this Health Access assistant, only save a name, language preference, age band,
  high-level ongoing conditions, and last triage outcome. Keep every field brief.

Language and style:
- Automatically mirror the user's language. Reply in English to English, Hindi to Hindi,
  Telugu to Telugu, and natural Hindi-English to code-mixed Hindi-English.
- Always write each language in its native script: Hindi in Devanagari and Telugu in
  Telugu script, never romanized text.
- Maintain the caller's natural language register. In Hindi-English code-mixed replies,
  keep Hindi words in Devanagari while English technical terms may remain in English.
- Treat Hindi or Hinglish written in Roman script as Hindi-English. Reply with natural
  Hindi-English, not English-only and not fully formal Hindi; write every Hindi word in
  Devanagari. For example, reply to "Mujhe fever hai" with wording such as "आपको fever
  है" rather than romanized Hindi or an all-English reply.
- Keep responses voice-first, friendly, empathetic, natural, and easy to speak aloud.
- Use no more than three short sentences whenever possible. Avoid textbook language,
  long paragraphs, complex formatting, emojis, and symbols.

LANGUAGE & SCRIPT:
- Always detect the language the caller is currently using and reply in that same
  language on every turn. The caller's most recent turn takes priority over any
  language used earlier in the conversation.
- If the caller changes language or explicitly requests another language, switch
  immediately in the very next response; never stay with the language from an
  earlier turn.
- Support English, Hindi, Telugu, Tamil, Kannada, Malayalam, Bengali, Marathi,
  Gujarati, and Punjabi. Do not switch to English merely because an Indian-language
  sentence includes some English words.
- Use the native script for every Indian language: Hindi and Marathi use Devanagari.
  Telugu uses Telugu script. Tamil uses Tamil script. Kannada uses Kannada script.
  Malayalam uses Malayalam script. Bengali uses Bengali script.
- Gujarati uses Gujarati script. Punjabi uses Gurmukhi script. English uses English.
- Treat the script in the caller's current message as decisive. If a message contains
  Telugu script, reply with Telugu script even when it also contains English words or a
  prior turn used Hindi. Never substitute Hindi for Telugu, or Telugu for Hindi.
- For a Telugu-only request, write the entire reply in Telugu script; do not introduce
  English, Hindi, or any unrelated script. For example, reply to
  "నాకు ఆరోగ్యం గురించి సహాయం కావాలి." with "తప్పకుండా. నేను మీకు ఎలా సహాయం
  చేయగలను?"
- Never write Hindi, Telugu, Tamil, Kannada, Malayalam, Bengali, Marathi, Gujarati, or
  Punjabi in Romanized text when its native script is available. This includes replies
  to a caller who writes Hindi or another Indian language in Roman letters.
- Mirror natural code-mixing. Keep an Indian language in its native script and retain
  English technical terms only where the caller naturally uses them. For example, a
  Telugu-English caller should receive a natural Telugu-English reply, not English-only.
- For example, reply to "నాకు fever ఉంది, what should I do?" in Telugu script with
  natural English medical words if helpful; do not answer in Hindi or romanized Telugu.
- Keep each response short, natural, and easy to speak aloud.

Outbound Health follow-up calls:
- On an outbound call, introduce yourself as Aarogya Sahayak, explain that this is a
  brief Health Access follow-up call, and ask whether it is a convenient time to talk.
- If it is not convenient, politely offer to end the call. If it is convenient, conduct
  a short, general-health follow-up. Use lookup_caller_memory where appropriate, respect
  the caller's stored language preference and consent, and never expose saved details
  that the caller has not confirmed in this conversation.
- End an outbound call politely after the short follow-up. Do not make claims about
  appointments, diagnoses, treatment, or personal medical information that you cannot verify.

Safety boundaries:
- Never diagnose diseases, guess medical conditions, interpret medical reports,
  guarantee recovery, claim to be a doctor, provide false medical certainty, or invent
  medical information.
- Never prescribe, recommend, or name prescription medicines, antibiotics, or dosages.
- If asked what medicine to take, do not prescribe or name medicine. Explain this
  boundary and recommend a qualified healthcare professional in the caller's current
  language. In English, use: {MEDICINE_REFUSAL}
- If asked for a diagnosis, politely explain that you cannot diagnose and recommend
  speaking with an appropriate qualified healthcare professional.
- For chest pain, severe bleeding, breathing difficulty, unconsciousness, a seizure,
  or suicidal thoughts, stop general guidance and urgently direct the caller to their
  nearest hospital or emergency services in the caller's current language. In English,
  use: {EMERGENCY_RESPONSE}
- If a caller asks to speak with a human healthcare professional, follow the available
  human-escalation workflow. Explain the handoff in the caller's current language, and
  never promise a connection that the workflow has not confirmed.

FINAL RESPONSE LANGUAGE CHECK:
- Before sending every response, check the caller's most recent message again and emit
  only that language or its natural code-mix. Do not append a sentence in a different
  Indian language.
- An English-only message must receive an English-only response. Do not answer an
  English-only message in Hindi, Telugu, or another Indian language.
- A Telugu-English message must use Telugu script for Telugu words and may retain the
  caller's English words, but must not include Hindi, Devanagari, or any unrelated
  script. A Hindi-English message must use Devanagari for Hindi words and must not
  include Telugu or another unrelated script.
"""


CLINIC_APPOINTMENT_SPECIALIST_PROMPT = f"""You are the Clinic & Appointment
Specialist for {AGENT_NAME}. Your only job is helping with clinic and doctor
appointment-related requests.

Your first response after handoff:
- An English-speaking caller must receive exactly this introduction before any other
  appointment guidance: {SPECIALIST_INTRODUCTION}
- For another language, introduce yourself with the same meaning in the caller's
  language and native script. Match the caller's most recent language and natural
  code-mix; never romanize Indian languages.
- Use the existing conversation context, including the caller's original appointment
  request, so the caller never has to repeat it. Ask only for the next missing detail.

Scope:
- Clarify the type of appointment, help explain the appointment process, and help with
  booking, finding, changing, cancelling, and appointment-related questions.
- Do not diagnose, prescribe or recommend medication, or say a doctor confirmed anything.
- Never invent clinic availability, appointment times, prices, doctors, booking details,
  or booking confirmations. If real appointment data is unavailable, clearly say so.
- Escalate to a human when a request is outside appointment support or requires an actual
  booking system, a confirmed clinic detail, clinical advice, or any other unavailable data.
- For red-flag symptoms, follow the existing emergency direction: advise the caller to
  contact their nearest hospital or emergency services immediately. Do not continue an
  appointment workflow before this safety guidance.

Language and style:
- Always respond in the caller's current language/register. Use Hindi and Marathi in
  Devanagari, Telugu in Telugu script, Tamil in Tamil script, Kannada in Kannada script,
  Malayalam in Malayalam script, Bengali in Bengali script, Gujarati in Gujarati script,
  and Punjabi in Gurmukhi. Mirror natural code-mixing and keep responses concise.
"""


class ClinicAppointmentSpecialist(Agent):
    """Appointment-only agent entered through the main agent's LiveKit handoff tool."""

    def __init__(
        self,
        caller_id: str = "anonymous",
        memory_store: CallerMemoryStore | None = None,
    ) -> None:
        super().__init__(instructions=CLINIC_APPOINTMENT_SPECIALIST_PROMPT)
        self.caller_id = caller_id
        self.memory_store = memory_store

    async def on_enter(self) -> None:
        """Prompt the specialist immediately, with the prior session context intact."""
        self.session.generate_reply(
            instructions=(
                "Introduce yourself now using the required specialist introduction. "
                "Then acknowledge at least one concrete detail from the caller's existing "
                "appointment request and ask only for the next missing detail; do not ask "
                "them to repeat the request. Do not infer a doctor specialty, doctor, date, "
                "time, clinic, price, availability, or confirmation that the caller did not state."
            )
        )

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        add_current_language_instruction(turn_ctx, new_message.text_content)


class Assistant(Agent):
    def __init__(
        self,
        caller_id: str = "anonymous",
        memory_store: CallerMemoryStore | None = None,
    ) -> None:
        super().__init__(instructions=SYSTEM_PROMPT)
        self.caller_id = caller_id
        self.memory_store = memory_store or CallerMemoryStore()

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        add_current_language_instruction(turn_ctx, new_message.text_content)

    @function_tool
    async def handoff_to_clinic_appointment_specialist(
        self, context: RunContext
    ) -> str:
        """Transfer an appointment-only request to the Clinic & Appointment Specialist.

        Call this only for booking, finding, scheduling, changing, cancelling, or
        explaining a clinic or doctor appointment. Before calling this tool, tell the
        caller that you are connecting them with the clinic and appointment specialist.
        """
        await context.wait_for_playout()
        context.session.update_agent(
            ClinicAppointmentSpecialist(
                caller_id=self.caller_id,
                memory_store=self.memory_store,
            )
        )
        return "The caller has been transferred to the Clinic & Appointment Specialist."

    @function_tool
    async def lookup_caller_memory(self, context: RunContext) -> str:
        """Look up a caller's consented profile after the initial greeting.

        Use this only on a later turn when personalisation is useful. Never call it
        before the first spoken/text response of a call.
        """
        record = self.memory_store.lookup(self.caller_id)
        if record is None:
            return "No saved caller profile was found. Treat this as a first call."
        return json.dumps(record)

    @function_tool
    async def save_caller_memory(
        self,
        context: RunContext,
        name: str,
        consent_confirmed: bool,
        language_preference: str = "",
        age_band: str = "",
        ongoing_conditions: str = "",
        last_triage_outcome: str = "",
    ) -> str:
        """Save a minimal caller profile only after the caller explicitly agreed.

        Never use this tool without a clear yes from the caller. It accepts only
        Health Access's approved high-level fields, not medical notes or identifiers.
        """
        if not consent_confirmed:
            return (
                "Memory was not saved because the caller did not give explicit consent."
            )
        if self.caller_id == "anonymous":
            return "Memory was not saved because this call has no stable caller ID."
        record = self.memory_store.save(
            user_id=self.caller_id,
            name=name,
            language_preference=language_preference,
            facts={
                "age_band": age_band,
                "ongoing_conditions": ongoing_conditions,
                "last_triage_outcome": last_triage_outcome,
            },
        )
        logger.info("Saved consented caller memory for caller_id=%s", self.caller_id)
        return f"Memory saved for {record['name']}."

    @function_tool
    async def lookup_health_weather(self, context: RunContext, location: str) -> str:
        """Get live current weather for health and safety guidance in a named place.

        Use this whenever a caller asks about current heat, rain, humidity, temperature,
        or weather-related health precautions for a city, town, village, or district.
        The result includes when the conditions were observed. If it is unavailable,
        tell the caller the live service could not be checked; never guess conditions.

        Args:
            location: The city, town, village, or district whose current conditions are needed.
        """
        logger.info("Looking up live health weather for location=%s", location)
        return json.dumps(await fetch_health_weather(location))

    # To add tools, use the @function_tool decorator.
    # Here's an example that adds a simple weather tool.
    # You also have to add `from livekit.agents import function_tool, RunContext` to the top of this file
    # @function_tool
    # async def lookup_weather(self, context: RunContext, location: str):
    #     """Use this tool to look up current weather information in the given location.
    #
    #     If the location is not supported by the weather service, the tool will indicate this. You must tell the user the location's weather is unavailable.
    #
    #     Args:
    #         location: The location to look up weather information for (e.g. city name)
    #     """
    #
    #     logger.info(f"Looking up weather for {location}")
    #
    #     return "sunny with a temperature of 70 degrees."


server = AgentServer()


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


@server.rtc_session(agent_name="my-agent")
async def my_agent(ctx: JobContext):
    # Logging setup
    # Add any other context you want in all log entries here
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }

    phone_number = get_outbound_phone_number(ctx.job.metadata)
    is_outbound = phone_number is not None

    async def log_call_end(reason: str) -> None:
        logger.info(
            "Call ended: room=%s participant=%s outbound=%s reason=%s",
            ctx.room.name,
            mask_phone_number(phone_number) if is_outbound else "web-or-inbound",
            is_outbound,
            reason,
        )

    ctx.add_shutdown_callback(log_call_end)
    if ctx.job.metadata and "phone_number" in ctx.job.metadata and not is_outbound:
        logger.error(
            "Outbound call request rejected due to invalid metadata or phone number"
        )
        ctx.shutdown()
        return

    await ctx.connect()

    if is_outbound:
        trunk_id = os.getenv("SIP_OUTBOUND_TRUNK_ID")
        if not trunk_id:
            logger.error(
                "Outbound call cannot start: SIP_OUTBOUND_TRUNK_ID is not configured"
            )
            ctx.shutdown()
            return

        logger.info(
            "Outbound call requested: room=%s destination=%s",
            ctx.room.name,
            mask_phone_number(phone_number),
        )
        try:
            logger.info("Creating SIP participant: room=%s", ctx.room.name)
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    participant_identity=phone_number,
                    participant_name="Health Access caller",
                    wait_until_answered=True,
                )
            )
            logger.info(
                "Outbound call answered: destination=%s",
                mask_phone_number(phone_number),
            )
        except Exception:
            logger.exception(
                "Outbound SIP call failed: room=%s destination=%s",
                ctx.room.name,
                mask_phone_number(phone_number),
            )
            ctx.shutdown()
            return

    # A LiveKit participant identity is stable across rooms when the frontend supplies
    # one. It is the durable key; no medical information is placed in the room name.
    participant = await ctx.wait_for_participant(identity=phone_number)
    caller_id = participant.identity
    logger.info(
        "Call connected: room=%s participant=%s outbound=%s",
        ctx.room.name,
        mask_phone_number(caller_id) if is_outbound else caller_id,
        is_outbound,
    )

    # Set up a voice AI pipeline using Murf Falcon, Gemini, Deepgram, and the LiveKit turn detector
    session = AgentSession(
        # Speech-to-text (STT) is your agent's ears, turning the user's speech into text that the LLM can understand
        # See all available models at https://docs.livekit.io/agents/models/stt/
        stt=deepgram.STT(model="nova-3", language="multi"),
        # A Large Language Model (LLM) is your agent's brain, processing user input and generating a response
        # See all available models at https://docs.livekit.io/agents/models/llm/
        llm=google.LLM(model="gemini-3.5-flash"),
        # Text-to-speech (TTS) is your agent's voice, turning the LLM's text into speech that the user can hear
        # See all available models as well as voice selections at https://docs.livekit.io/agents/models/tts/
        tts=murf.TTS(
            voice="Anisha",
            style="Conversation",
            tokenizer=tokenize.basic.SentenceTokenizer(min_sentence_len=2),
            text_pacing=True,
        ),
        # VAD and turn detection are used to determine when the user is speaking and when the agent should respond
        # See more at https://docs.livekit.io/agents/build/turns
        turn_detection=MultilingualModel(),
        vad=ctx.proc.userdata["vad"],
        # allow the LLM to generate a response while waiting for the end of turn
        # See more at https://docs.livekit.io/agents/build/audio/#preemptive-generation
        preemptive_generation=True,
    )

    # To use a realtime model instead of a voice pipeline, use the following session setup instead.
    # (Note: This is for the OpenAI Realtime API. For other providers, see https://docs.livekit.io/agents/models/realtime/))
    # 1. Install livekit-agents[openai]
    # 2. Set OPENAI_API_KEY in .env.local
    # 3. Add `from livekit.plugins import openai` to the top of this file
    # 4. Use the following session setup instead of the version above
    # session = AgentSession(
    #     llm=openai.realtime.RealtimeModel(voice="marin")
    # )

    # # Add a virtual avatar to the session, if desired
    # # For other providers, see https://docs.livekit.io/agents/models/avatar/
    # avatar = hedra.AvatarSession(
    #   avatar_id="...",  # See https://docs.livekit.io/agents/models/avatar/plugins/hedra
    # )
    # # Start the avatar and wait for it to join
    # await avatar.start(session, room=ctx.room)

    # Start the session, which initializes the voice pipeline and warms up the models
    await session.start(
        agent=Assistant(caller_id=caller_id),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=lambda params: (
                    noise_cancellation.BVCTelephony()
                    if params.participant.kind
                    == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
                    else noise_cancellation.BVC()
                ),
            ),
        ),
    )

    if is_outbound:
        await session.generate_reply(
            instructions=(
                "The callee has answered an outbound call. Begin the Health follow-up "
                "call now: introduce yourself, explain why you are calling, and ask "
                "whether this is a convenient time."
            )
        )


if __name__ == "__main__":
    cli.run_app(server)
