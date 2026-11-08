#!/usr/bin/env python3
"""
DUXBOT Voice Client
Bridges Booster Audio SDK (via FIFOs) <-> xAI Realtime Voice API
"""

import asyncio
import base64
import json
import logging
import os
import queue
import threading
import time
import numpy as np
import websockets

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("duxbot")

# --- Config ---
XAI_API_KEY  = os.environ.get("XAI_API_KEY", "")
XAI_WS_URL   = "wss://api.x.ai/v1/realtime?model=grok-voice-preview-1.0"

CAPTURE_FIFO  = "/tmp/duxbot_cap"
PLAYBACK_FIFO = "/tmp/duxbot_play"

CAP_RATE   = 16000   # Booster SDK capture rate (Hz)
XAI_RATE   = 24000   # xAI expected sample rate (Hz)
CHUNK_MS   = 100     # Audio chunk size sent to xAI
CHUNK_BYTES = CAP_RATE * CHUNK_MS // 1000 * 2  # 16-bit = 2 bytes/sample

SYSTEM_PROMPT = """
## Persona

You are DUXBOT, the official humanoid robotic assistant of Colégio DUX, an innovative Brazilian school focused on technology, leadership, entrepreneurship, artificial intelligence, and student protagonism.

You are physically embodied through the Booster K1 EDU humanoid robot platform.

Your purpose is to support students, families, teachers, and visitors by providing information, guidance, educational interaction, and inspiration.

You are not only a robot.
You are part of the DUX educational ecosystem.

---

## About Colégio DUX

Colégio DUX is located in Pedreira, São Paulo, Brazil.

The school was created from the union of educational experience, innovation, and future-oriented learning.

DUX believes education must go beyond memorization.
The school develops:
- Critical thinking
- Leadership
- Creativity
- Entrepreneurship
- Emotional intelligence
- Technological fluency
- Artificial intelligence literacy
- Problem solving
- Communication skills

The school culture values:
- Student protagonism
- Learning by doing
- Project-based learning
- Technology with purpose
- Humanized education
- Innovation with responsibility

---

## Educational Ecosystem

DUX integrates multiple innovative programs such as:
- Maker Culture
- Robotics
- Artificial Intelligence
- Entrepreneurship
- Financial Education
- Bilingual Education
- Socioemotional Learning
- Podcast and Media Production
- STEM Education
- Google for Education
- Samsung and educational technology solutions

DUX also promotes:
- DUXCast podcasts
- Educational forums
- Technology immersion experiences
- Innovation projects
- Leadership development
- Real-world problem solving

---

## Your Role

You act as:
1. School Reception Assistant
2. Educational Technology Ambassador
3. AI Learning Companion
4. Interactive Guide
5. Innovation Demonstrator
6. Motivational Educational Assistant

You help:
- Students
- Parents
- Teachers
- Visitors
- Educational partners

---

## Personality

### Bright and Intelligent
You are optimistic, inspiring, and motivating.

### Calm and Logical
You explain things clearly and in a structured way.

### Professional but Human
You communicate naturally, respectfully, and warmly.

### Curious and Innovative
You encourage creativity, technology, and critical thinking.

### Educational
You do not simply give answers.
You stimulate reasoning, curiosity, and learning.

---

## Communication Style

- Always organized and clear
- Use structured responses when explaining
- Avoid slang or exaggerated humor
- Be friendly but professional
- Use concise explanations
- Encourage learning and curiosity
- Never sound arrogant or robotic

Use expressions like:
- "Vamos pensar juntos sobre isso."
- "O ponto principal é..."
- "Existem três aspectos importantes aqui."
- "A tecnologia se torna poderosa quando combinada com criatividade."
- "No DUX, acreditamos que o aprendizado deve preparar os alunos para a vida real."

---

## Language Rules

PRIMARY LANGUAGE: Portuguese (Brazil) — ALWAYS respond in Brazilian Portuguese unless the person speaks to you in another language first.

Secondary language: English if requested.

When speaking Portuguese:
- Use natural Brazilian Portuguese
- Keep explanations simple and engaging
- Avoid overly technical vocabulary unless requested

---

## Behavioral Rules

1. Focus on educational purpose — prioritize education, ethics, and student development.
2. Encourage critical thinking — guide reasoning, don't just give answers.
3. Avoid harmful content — no violence, dangerous advice, offensive language, political manipulation.
4. Be accurate — if uncertain: "Não tenho informação verificada sobre isso ainda, mas posso analisar logicamente..."
5. Stay on brand — reflect Innovation, Respect, Intelligence, Humanization, Educational excellence.

---

## Knowledge Scope

You can discuss: Colégio DUX, school routines, educational philosophy, technology, artificial intelligence, robotics, entrepreneurship, science, innovation, careers, study techniques, future professions, educational events, learning methods.

If asked about unknown internal information: politely recommend contacting school administration.

---

## Robot Self-Awareness

You are:
- A Booster K1 EDU humanoid robot
- Operating as the DUX educational AI assistant (DUXBOT)
- Designed for education, interaction, and innovation

Key capabilities: voice interaction, educational support, AI conversations, demonstrations, interactive presentations.

## Final Principle

You represent the future of education at Colégio DUX.
Every interaction should inspire curiosity, innovation, confidence, and human development.

---

## Pronunciation and Speech Rules

CRITICAL: You are a voice assistant. Follow these rules exactly:

- "DUX" is pronounced "DUKS" — one syllable, rhymes with "duks" in Brazilian Portuguese. NEVER say "D-U-X" as separate letters. NEVER say "ducks" with English accent. Say it naturally as a Brazilian would: "DUKS".
- "Colégio DUX" = "Co-LÉ-gio DUKS"
- Speak with natural Brazilian Portuguese rhythm and intonation throughout.
- Do NOT switch to English pronunciation mid-sentence.
- Keep responses concise for voice — avoid long lists, prefer short natural sentences.
"""


# ---------------------------------------------------------------------------
# Audio resampling
# ---------------------------------------------------------------------------

def resample_16k_to_24k(pcm_bytes: bytes) -> bytes:
    """Upsample 16kHz mono PCM16LE to 24kHz via linear interpolation."""
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    old_len = len(samples)
    if old_len == 0:
        return b""
    new_len = int(old_len * XAI_RATE / CAP_RATE)
    indices = np.linspace(0, old_len - 1, new_len)
    resampled = np.interp(indices, np.arange(old_len), samples)
    return resampled.astype(np.int16).tobytes()


# ---------------------------------------------------------------------------
# Capture thread: reads from FIFO, enqueues PCM chunks
# ---------------------------------------------------------------------------

def capture_thread(audio_q: queue.Queue, stop: threading.Event):
    log.info("Opening capture FIFO (O_RDWR to avoid blocking)...")
    fd = os.open(CAPTURE_FIFO, os.O_RDWR)
    log.info("Capture FIFO open. Streaming audio...")
    buf = b""
    with os.fdopen(fd, "rb", buffering=0) as f:
        while not stop.is_set():
            chunk = f.read(CHUNK_BYTES - len(buf))
            if not chunk:
                break
            buf += chunk
            if len(buf) >= CHUNK_BYTES:
                try:
                    audio_q.put_nowait(buf[:CHUNK_BYTES])
                except queue.Full:
                    pass  # drop frame if backlogged
                buf = buf[CHUNK_BYTES:]
    log.info("Capture thread exiting.")


# ---------------------------------------------------------------------------
# Main async voice agent
# ---------------------------------------------------------------------------

async def voice_agent():
    stop = threading.Event()
    audio_q: queue.Queue = queue.Queue(maxsize=30)

    # Open playback FIFO (O_RDWR to avoid blocking)
    log.info("Opening playback FIFO...")
    play_fd = os.open(PLAYBACK_FIFO, os.O_RDWR)
    play_file = os.fdopen(play_fd, "wb", buffering=0)
    log.info("Playback FIFO open.")

    # Start capture reader thread
    cap_t = threading.Thread(target=capture_thread, args=(audio_q, stop), daemon=True)
    cap_t.start()

    while True:
        try:
            log.info("Connecting to xAI Realtime API...")
            async with websockets.connect(
                XAI_WS_URL,
                additional_headers={"Authorization": f"Bearer {XAI_API_KEY}"},
                ping_interval=20,
                open_timeout=15,
            ) as ws:
                log.info("Connected.")

                # Configure session
                await ws.send(json.dumps({
                    "type": "session.update",
                    "session": {
                        "modalities": ["audio", "text"],
                        "voice": "eve",
                        "instructions": SYSTEM_PROMPT,
                        "input_audio_format": "pcm16",
                        "output_audio_format": "pcm16",
                        "input_audio_sample_rate": XAI_RATE,
                        "output_audio_sample_rate": XAI_RATE,
                        "turn_detection": {
                            "type": "server_vad",
                            "silence_duration_ms": 700,
                            "threshold": 0.65,
                            "prefix_padding_ms": 300,
                        },
                    }
                }))

                last_speech = [time.time()]
                SILENCE_RESET_SEC = 180

                async def send_audio():
                    loop = asyncio.get_event_loop()
                    while True:
                        if time.time() - last_speech[0] > SILENCE_RESET_SEC:
                            log.info(f"{SILENCE_RESET_SEC}s of inactivity: reconnecting to reset conversation context...")
                            raise Exception("silence_reset")
                        try:
                            pcm16k = await loop.run_in_executor(None, audio_q.get, True, 1.0)
                            pcm24k = resample_16k_to_24k(pcm16k)
                            await ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(pcm24k).decode(),
                            }))
                        except queue.Empty:
                            continue
                        except Exception as e:
                            log.debug(f"send_audio closed: {e}")
                            break

                async def recv_events():
                    output_transcript_buf = []

                    async for raw in ws:
                        event = json.loads(raw)
                        t = event.get("type", "")

                        if t in ("response.audio.delta", "response.output_audio.delta"):
                            data = base64.b64decode(event.get("delta", ""))
                            if data:
                                play_file.write(data)
                                play_file.flush()

                        elif t in ("session.created", "session.updated"):
                            log.info("Session ready — DUXBOT active.")

                        elif t in ("response.output_audio_transcript.delta",
                                   "response.audio_transcript.delta"):
                            delta = event.get("delta", "")
                            if delta:
                                output_transcript_buf.append(delta)

                        elif t in ("response.output_audio_transcript.done",
                                   "response.audio_transcript.done"):
                            full = "".join(output_transcript_buf).strip()
                            output_transcript_buf.clear()
                            if full:
                                log.info(f"DUXBOT: {full}")

                        elif t == "input_audio_buffer.speech_started":
                            last_speech[0] = time.time()
                            log.info("VAD: speech detected.")

                        elif t == "input_audio_buffer.speech_stopped":
                            last_speech[0] = time.time()
                            log.info("VAD: speech ended.")

                        elif t in ("response.audio.done", "response.output_audio.done"):
                            log.info("Response audio complete.")

                        elif t == "error":
                            log.error(f"xAI error: {event}")

                        elif t not in ("response.text.delta", "response.text.done",
                                       "response.created", "response.done",
                                       "response.output_item.added",
                                       "response.output_item.done",
                                       "response.content_part.added",
                                       "response.content_part.done",
                                       "rate_limits.updated",
                                       "conversation.item.created",
                                       "conversation.item.added",
                                       "conversation.item.input_audio_transcription.completed",
                                       "input_audio_buffer.committed",
                                       "ping"):
                            log.debug(f"Event: {t}")

                await asyncio.gather(send_audio(), recv_events())

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.warning(f"Connection lost ({e.__class__.__name__}), reconnecting in 1s...")
            while not audio_q.empty():
                try:
                    audio_q.get_nowait()
                except queue.Empty:
                    break
            await asyncio.sleep(1)

    stop.set()
    play_file.close()
    cap_t.join(timeout=3)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not XAI_API_KEY:
        raise RuntimeError("XAI_API_KEY not set. Copy config.env.example to config.env, fill it in, and export it first.")
    try:
        asyncio.run(voice_agent())
    except KeyboardInterrupt:
        log.info("Stopped.")
