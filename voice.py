"""
Voice pipeline for claudebot — Discord voice channel integration.

Provides speech-to-text, turn detection, and text-to-speech for real-time
voice conversations with Claude Code via Discord voice channels.

Adapted from Claude Avatar by Olivia (olivia.9596 / @4confusedemoji / github.com/taygetea).
The STT, TTS, turn detection, and Discord audio bridge code is based on her work.
"""

import os
import asyncio
import threading
import json
import base64
import time
import signal
import logging
from pathlib import Path
from enum import Enum, auto
from collections import deque
from typing import Callable, Any

import aiohttp
import numpy as np
from scipy import signal as scipy_signal
import websockets
from websockets import State as WebSocketState
import discord
from discord.ext import voice_recv

log = logging.getLogger("claudebot")

# ── Constants ─────────────────────────────────────────────────────────────────

DISCORD_SAMPLE_RATE = 48000
STT_SAMPLE_RATE = 16000
TTS_SAMPLE_RATE = 24000

# VAD settings (Silero VAD)
VAD_THRESHOLD = 0.65  # Higher = less noise sensitivity (0.5 default too sensitive for road/keyboard)
VAD_MIN_SILENCE_MS = 600
VAD_MIN_SPEECH_MS = 100
VAD_MAX_SPEECH_S = 8  # Force speech_end if VAD stays active this long (noise floor)

# Smart Turn detection
SMART_TURN_THRESHOLD = 0.5
SMART_TURN_FALLBACK_SECS = 0.5

# TTS
TTS_MODEL_ID = "eleven_flash_v2_5"
DEFAULT_VOICE_ID = "Bj9UqZbhQsanLzgalpEG"

# Voice registry — name → ElevenLabs voice ID
VOICE_REGISTRY: dict[str, str] = {
    "cowboy": "Bj9UqZbhQsanLzgalpEG",
    "clown": "PtJWpiWfVrCfOmNdkFpr",
    "asmr": "du9lwz8ZPYY8gsZt7QO5",
    "badal": "nz09Q9CDAFErYIezui5v",
    "trickster": "N2lVS1w4EtoT3dr4eOWO",
    "chill": "SAz9YHcvj6GT2YYXdXww",
    "warrior": "SOYHLrjzK2X1ezoPC6cr",
    "grandpa": "pqHfZKP75CvOlQylNhV4",
    "watts": "WD53oEZtK9wG1cK0PGgB",
    "soft": "uuwMBG2Gr2J4az3TLKTo",
    "atc": "auts4d7td9XfAYMrUKra",
}
VOICE_DESCRIPTIONS: dict[str, str] = {
    "cowboy": "rugged cowboy drawl",
    "clown": "silly clown voice",
    "asmr": "soft whispery ASMR",
    "badal": "breaking news anchor",
    "trickster": "husky gravelly trickster with an edge",
    "chill": "relaxed neutral narrator",
    "warrior": "fierce animated warrior",
    "grandpa": "wise old storyteller",
    "watts": "alan watts philosopher",
    "soft": "gentle soft voice",
    "atc": "air traffic controller",
}

# STT
STT_HALLUCINATION_THRESHOLD_SECS = 30
STT_BILLING_ERROR_KEYWORDS = ["insufficient_funds", "quota_exceeded", "payment_required"]

# Model path
MODELS_DIR = Path(__file__).parent / "models"
SMART_TURN_MODEL_PATH = MODELS_DIR / "smart-turn-v3.2-cpu.onnx"

# Config from env
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
VOICE_CHANNEL_IDS = [
    int(x) for x in os.getenv("VOICE_CHANNEL_IDS", "").split(",") if x.strip()
]
VOICE_ALLOWED_USER_IDS = [
    int(x) for x in os.getenv("VOICE_ALLOWED_USER_IDS", "").split(",") if x.strip()
]
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID)
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
VOICE_LLM_MODEL = os.getenv("VOICE_MODEL", "anthropic/claude-haiku-4.5")
VOICE_MODE = os.getenv("VOICE_MODE", "claude")  # "openrouter" or "claude"
VOICE_CLAUDE_MODEL = os.getenv("VOICE_CLAUDE_MODEL", "claude-opus-4-6")

# ── Noise filter ──────────────────────────────────────────────────────────────
# Adapted from Claude Avatar by Olivia (github.com/taygetea)

NOISE_PATTERNS = [
    "(heavy breathing)", "(static)", "(silence)", "(background noise)",
    "(inaudible)", "(unintelligible)", "(music)", "(coughing)",
    "(laughing)", "(sighing)",
]


def is_noise_commit(text: str) -> bool:
    """Check if a transcript is noise/non-speech rather than actual speech."""
    text_lower = text.lower().strip()
    if text_lower.startswith("(") and text_lower.endswith(")"):
        return True
    if text_lower.startswith("*") and text_lower.endswith("*") and text_lower.count("*") == 2:
        return True
    for pattern in NOISE_PATTERNS:
        if pattern in text_lower:
            return True
    if len(text_lower) < 3 and not text_lower.isalpha():
        return True
    return False


# ── Audio resampling ──────────────────────────────────────────────────────────
# Adapted from Claude Avatar by Olivia (github.com/taygetea)

class AudioResampler:
    """Handles audio resampling between Discord and voice engine formats."""

    def discord_to_engine(self, pcm_bytes: bytes) -> bytes:
        """48kHz stereo int16 -> 16kHz mono int16"""
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        if len(samples) == 0:
            return b""
        if len(samples) % 2 != 0:
            samples = samples[:-1]
        stereo = samples.reshape(-1, 2)
        mono = stereo.mean(axis=1).astype(np.float64)
        if len(mono) == 0:
            return b""
        resampled = scipy_signal.resample_poly(mono, up=1, down=3)
        return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()

    def engine_to_discord(self, pcm_bytes: bytes) -> bytes:
        """24kHz mono int16 -> 48kHz stereo int16"""
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
        if len(samples) == 0:
            return b""
        resampled = scipy_signal.resample_poly(samples, up=2, down=1)
        stereo = np.column_stack([resampled, resampled])
        return np.clip(stereo, -32768, 32767).astype(np.int16).tobytes()


# ── STT session ───────────────────────────────────────────────────────────────
# Adapted from Claude Avatar by Olivia (github.com/taygetea)

class UserSTTSession:
    """Manages a single user's ElevenLabs STT WebSocket connection."""

    def __init__(self, user_id: int, display_name: str,
                 on_transcript: Callable[[int, str], None] = None):
        self.user_id = user_id
        self.display_name = display_name
        self.on_transcript = on_transcript

        self.ws = None
        self.connected = asyncio.Event()
        self.should_stop = False

        self.audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
        self.transcripts: list[tuple[float, str]] = []
        self.partial_transcript = ""

        self._send_task = None
        self._receive_task = None

        self.last_audio_time: float = time.time()
        self.last_transcript_time: float = 0.0
        self.last_commit_time: float = 0.0
        self.last_close_reason: str | None = None

    def is_billing_error(self) -> bool:
        if not self.last_close_reason:
            return False
        reason_lower = self.last_close_reason.lower()
        return any(kw in reason_lower for kw in STT_BILLING_ERROR_KEYWORDS)

    def _get_stt_uri(self) -> str:
        return (
            f"wss://api.elevenlabs.io/v1/speech-to-text/realtime"
            f"?model_id=scribe_v2_realtime"
            f"&language_code=en"
            f"&commit_strategy=vad"
            f"&vad_silence_threshold_secs=0.5"
            f"&audio_format=pcm_16000"
        )

    async def start(self):
        try:
            headers = {"xi-api-key": ELEVENLABS_API_KEY}
            self.ws = await websockets.connect(self._get_stt_uri(), additional_headers=headers)
            self.connected.set()

            silent_chunk = b"\x00" * 3200
            init_msg = {
                "message_type": "input_audio_chunk",
                "audio_base_64": base64.b64encode(silent_chunk).decode("utf-8"),
                "commit": False,
                "sample_rate": STT_SAMPLE_RATE,
            }
            await self.ws.send(json.dumps(init_msg))

            self._send_task = asyncio.create_task(self._send_audio())
            self._receive_task = asyncio.create_task(self._receive_transcripts())
            log.info(f"[STT] {self.display_name}: connected")

        except Exception as e:
            log.warning(f"[STT] {self.display_name}: connection failed: {e}")
            self.connected.clear()

    async def stop(self, timeout: float = 2.0):
        self.should_stop = True
        self.connected.clear()

        tasks = [t for t in (self._send_task, self._receive_task) if t]
        for t in tasks:
            t.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
                )
            except asyncio.TimeoutError:
                pass

        if self.ws:
            try:
                await asyncio.wait_for(self.ws.close(), timeout=1.0)
            except Exception:
                pass
            self.ws = None

    async def send_audio(self, pcm_bytes: bytes):
        self.last_audio_time = time.time()
        try:
            self.audio_queue.put_nowait(pcm_bytes)
        except asyncio.QueueFull:
            pass  # drop

    async def force_commit(self, debounce_secs: float = 2.0):
        now = time.time()
        if now - self.last_commit_time < debounce_secs:
            return
        ws = self.ws
        if ws and self.connected.is_set():
            try:
                self.last_commit_time = now
                msg = {
                    "message_type": "input_audio_chunk",
                    "audio_base_64": base64.b64encode(b"\x00" * 1600).decode("utf-8"),
                    "commit": True,
                    "sample_rate": STT_SAMPLE_RATE,
                }
                await ws.send(json.dumps(msg))
            except Exception:
                pass

    async def _send_audio(self):
        silence_chunk = b"\x00" * 3200

        while not self.should_stop and self.connected.is_set():
            try:
                try:
                    pcm_bytes = await asyncio.wait_for(self.audio_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    pcm_bytes = silence_chunk

                audio_b64 = base64.b64encode(pcm_bytes).decode("utf-8")
                msg = {
                    "message_type": "input_audio_chunk",
                    "audio_base_64": audio_b64,
                    "commit": False,
                    "sample_rate": STT_SAMPLE_RATE,
                }
                ws = self.ws
                if ws:
                    await ws.send(json.dumps(msg))

            except websockets.exceptions.ConnectionClosed as e:
                self.last_close_reason = e.reason or ""
                log.info(f"[STT] {self.display_name}: send closed (code={e.code})")
                self.connected.clear()
                break
            except Exception as e:
                if not self.should_stop:
                    log.warning(f"[STT] {self.display_name}: send error: {e}")
                self.connected.clear()
                break

    async def _receive_transcripts(self):
        try:
            async for message in self.ws:
                if self.should_stop:
                    break
                data = json.loads(message)
                msg_type = data.get("message_type", "")

                if msg_type == "partial_transcript":
                    self.partial_transcript = data.get("text", "")

                elif msg_type in ("committed_transcript", "committed_transcript_with_timestamps"):
                    text = data.get("text", "")
                    if text and not is_noise_commit(text):
                        audio_age = time.time() - self.last_audio_time
                        if audio_age > STT_HALLUCINATION_THRESHOLD_SECS:
                            log.debug(f"[STT] {self.display_name}: REJECTED hallucination: {text[:50]}")
                            continue
                        timestamp = time.time()
                        self.last_transcript_time = timestamp
                        self.transcripts.append((timestamp, text))
                        log.info(f"[STT] {self.display_name}: {text}")
                        if self.on_transcript:
                            self.on_transcript(self.user_id, text)

                elif "error" in msg_type:
                    log.warning(f"[STT] {self.display_name} error: {data.get('error', 'unknown')}")

        except websockets.exceptions.ConnectionClosed as e:
            self.last_close_reason = e.reason or ""
            log.info(f"[STT] {self.display_name}: receive closed (code={e.code})")
        except Exception as e:
            if not self.should_stop:
                log.warning(f"[STT] {self.display_name}: receive error: {e}")
        finally:
            self.connected.clear()

    def get_transcripts(self) -> list[tuple[float, str]]:
        result = self.transcripts.copy()
        self.transcripts.clear()
        self.partial_transcript = ""
        return result

    def has_transcripts(self) -> bool:
        return len(self.transcripts) > 0


class WhisperSTTSession:
    """Local Whisper-based STT fallback — same interface as UserSTTSession.

    Buffers 16kHz mono PCM audio and transcribes with faster-whisper when
    force_commit() is called (triggered by Silero VAD speech_end).
    """

    _model = None  # class-level singleton

    def __init__(self, user_id: int, display_name: str,
                 on_transcript: Callable[[int, str], None] = None):
        self.user_id = user_id
        self.display_name = display_name
        self.on_transcript = on_transcript

        self.connected = asyncio.Event()
        self.should_stop = False
        self.audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
        self.transcripts: list[tuple[float, str]] = []
        self.partial_transcript = ""
        self.last_audio_time: float = time.time()
        self.last_transcript_time: float = 0.0
        self.last_commit_time: float = 0.0
        self.last_close_reason: str | None = None

        self._audio_buffer: list[bytes] = []
        self._buffer_lock = threading.Lock()

    @classmethod
    def _load_model(cls):
        if cls._model is None:
            from faster_whisper import WhisperModel
            cls._model = WhisperModel("base.en", device="cuda", compute_type="float16")
            log.info("[WhisperSTT] Model loaded (base.en, CUDA)")

    def is_billing_error(self) -> bool:
        return False  # local model never has billing errors

    async def start(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._load_model)
        self.connected.set()
        log.info(f"[WhisperSTT] {self.display_name}: ready (local fallback)")

    async def stop(self, timeout: float = 2.0):
        self.should_stop = True
        self.connected.clear()

    async def send_audio(self, pcm_bytes: bytes):
        self.last_audio_time = time.time()
        with self._buffer_lock:
            self._audio_buffer.append(pcm_bytes)

    async def force_commit(self, debounce_secs: float = 0.5):
        now = time.time()
        if now - self.last_commit_time < debounce_secs:
            return
        self.last_commit_time = now
        with self._buffer_lock:
            if not self._audio_buffer:
                return
            all_audio = b"".join(self._audio_buffer)
            self._audio_buffer.clear()
        # Need at least 0.3s of audio (4800 samples at 16kHz)
        if len(all_audio) < 9600:
            return
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, self._transcribe, all_audio)
        if text and not is_noise_commit(text):
            timestamp = time.time()
            self.last_transcript_time = timestamp
            self.transcripts.append((timestamp, text))
            log.info(f"[WhisperSTT] {self.display_name}: {text}")
            if self.on_transcript:
                self.on_transcript(self.user_id, text)

    def _transcribe(self, pcm_bytes: bytes) -> str:
        """Run Whisper transcription on PCM audio (blocking)."""
        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = self._model.transcribe(
            audio, language="en", beam_size=1,
            vad_filter=True, vad_parameters={"min_silence_duration_ms": 300},
        )
        texts = [seg.text.strip() for seg in segments if seg.text.strip()]
        return " ".join(texts)

    def get_transcripts(self) -> list[tuple[float, str]]:
        result = self.transcripts.copy()
        self.transcripts.clear()
        self.partial_transcript = ""
        return result

    def has_transcripts(self) -> bool:
        return len(self.transcripts) > 0


class MultiUserSTTManager:
    """Manages STT sessions for multiple users with speaker attribution."""
    # Adapted from Claude Avatar by Olivia (github.com/taygetea)

    def __init__(self, on_transcript: Callable[[int, str], None] = None):
        self._sessions: dict[int, UserSTTSession | WhisperSTTSession] = {}
        self._session_lock = asyncio.Lock()
        self.user_names: dict[int, str] = {}
        self.on_transcript = on_transcript
        self._use_local_stt: bool = False  # fallback to Whisper

    async def ensure_session(self, user_id: int, display_name: str):
        async with self._session_lock:
            if user_id not in self._sessions:
                self.user_names[user_id] = display_name
                if self._use_local_stt:
                    session = WhisperSTTSession(user_id, display_name, on_transcript=self.on_transcript)
                else:
                    session = UserSTTSession(user_id, display_name, on_transcript=self.on_transcript)
                self._sessions[user_id] = session
                await session.start()
            return self._sessions[user_id]

    async def _switch_to_whisper(self, user_id: int, display_name: str):
        """Replace an ElevenLabs session with a local Whisper session."""
        self._use_local_stt = True
        old = self._sessions.get(user_id)
        if old:
            await old.stop()
        session = WhisperSTTSession(user_id, display_name, on_transcript=self.on_transcript)
        self._sessions[user_id] = session
        await session.start()
        log.info(f"[STT] {display_name}: switched to local Whisper STT")

    async def send_audio(self, user_id: int, display_name: str, pcm_bytes: bytes):
        session = await self.ensure_session(user_id, display_name)
        if session.connected.is_set():
            await session.send_audio(pcm_bytes)
        else:
            if session.is_billing_error():
                if not getattr(session, "_billing_logged", False):
                    log.warning(f"[STT] {display_name}: billing error — {session.last_close_reason}, switching to Whisper")
                    session._billing_logged = True
                    await self._switch_to_whisper(user_id, display_name)
                return
            # Avoid reconnect storm — only reconnect if not already reconnecting
            if not getattr(session, "_reconnecting", False):
                session._reconnecting = True
                log.info(f"[STT] {display_name}: reconnecting...")
                await self.reconnect_session(user_id)

    def get_aggregated_transcript(self) -> str:
        all_transcripts: list[tuple[float, int, str]] = []
        for user_id, session in self._sessions.items():
            for timestamp, text in session.get_transcripts():
                all_transcripts.append((timestamp, user_id, text))
        if not all_transcripts:
            return ""
        all_transcripts.sort(key=lambda x: x[0])
        lines = []
        for _, user_id, text in all_transcripts:
            name = self.user_names.get(user_id, f"User {user_id}")
            lines.append(f"{name}: {text}")
        return "\n".join(lines)

    def has_transcripts(self) -> bool:
        return any(s.has_transcripts() for s in self._sessions.values())

    async def stop_all(self, timeout: float = 5.0):
        if not self._sessions:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*[s.stop(timeout=2.0) for s in self._sessions.values()],
                               return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            pass
        self._sessions.clear()

    async def force_commit(self, user_id: int):
        if user_id in self._sessions:
            await self._sessions[user_id].force_commit()

    async def reconnect_session(self, user_id: int):
        if user_id in self._sessions:
            session = self._sessions[user_id]
            if not session.connected.is_set() and not session.is_billing_error():
                await session.stop()
                display_name = self.user_names.get(user_id, f"User {user_id}")
                new_session = UserSTTSession(user_id, display_name, on_transcript=self.on_transcript)
                self._sessions[user_id] = new_session
                await new_session.start()


# ── Smart Turn detection ──────────────────────────────────────────────────────
# Adapted from Claude Avatar by Olivia (github.com/taygetea)

def _truncate_audio(audio: np.ndarray, n_seconds: int = 8, sr: int = 16000) -> np.ndarray:
    max_samples = n_seconds * sr
    if len(audio) > max_samples:
        return audio[-max_samples:]
    elif len(audio) < max_samples:
        return np.pad(audio, (max_samples - len(audio), 0), mode="constant")
    return audio


class SmartTurnDetector:
    """ONNX-based turn completion detection."""

    def __init__(self):
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor

        log.info("Loading Smart Turn model...")
        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(str(SMART_TURN_MODEL_PATH), sess_options=so)
        self.feature_extractor = WhisperFeatureExtractor(chunk_length=8)
        log.info("Smart Turn model loaded")

    def predict(self, audio_array: np.ndarray) -> dict:
        audio_array = _truncate_audio(audio_array, n_seconds=8)
        inputs = self.feature_extractor(
            audio_array, sampling_rate=STT_SAMPLE_RATE,
            return_tensors="np", padding="max_length",
            max_length=8 * STT_SAMPLE_RATE, truncation=True, do_normalize=True,
        )
        input_features = inputs.input_features.squeeze(0).astype(np.float32)
        input_features = np.expand_dims(input_features, axis=0)
        outputs = self.session.run(None, {"input_features": input_features})
        probability = outputs[0][0].item()
        return {"prediction": 1 if probability > 0.5 else 0, "probability": probability}


class SmartTurnManager:
    """VAD + Smart Turn turn detection."""

    def __init__(self, threshold: float = SMART_TURN_THRESHOLD):
        import torch
        from silero_vad import load_silero_vad

        self.vad_model = load_silero_vad()
        self.smart_turn = SmartTurnDetector()
        self.threshold = threshold
        self.audio_buffer = []
        self.is_speaking = False
        self.speech_start_time = None
        self.silence_start_time = None
        self.last_smart_turn_result = None
        self.waiting_for_more_speech = False
        self.wait_start_time = None
        self.pending_evaluation = False
        self.transcript_buffer = []
        log.info(f"SmartTurnManager initialized (threshold={self.threshold})")

    def reset(self):
        self.audio_buffer = []
        self.is_speaking = False
        self.speech_start_time = None
        self.silence_start_time = None
        self.last_smart_turn_result = None
        self.waiting_for_more_speech = False
        self.wait_start_time = None
        self.pending_evaluation = False
        self.transcript_buffer = []

    def on_speech_started(self):
        if self.waiting_for_more_speech:
            self.waiting_for_more_speech = False
            self.wait_start_time = None
            self.pending_evaluation = False
        if not self.is_speaking:
            self.is_speaking = True
            self.speech_start_time = time.time()

    def on_speech_ended(self) -> dict:
        self.is_speaking = False
        if not self.audio_buffer:
            return {"turn_complete": False, "probability": 0.0, "needs_wait": False}

        if self.transcript_buffer:
            # Have transcript — run Smart Turn model on audio
            audio = np.array(self.audio_buffer, dtype=np.float32)
            result = self.smart_turn.predict(audio)
            prob = result["probability"]
            log.info(f"[SmartTurn] Speech ended with transcript, prob={prob:.3f} (threshold={self.threshold})")
            if prob >= self.threshold:
                return {"turn_complete": True, "probability": prob, "needs_wait": False}
            else:
                # Not confident — short fallback wait
                self.waiting_for_more_speech = True
                self.wait_start_time = time.time()
                return {"turn_complete": False, "probability": prob, "needs_wait": True}
        else:
            # No transcript yet — wait for STT to commit, then evaluate
            self.waiting_for_more_speech = True
            self.wait_start_time = time.time()
            self.pending_evaluation = True
            return {"turn_complete": False, "probability": 0.0, "needs_wait": True, "pending_transcript": True}

    def check_pending_evaluation(self) -> dict | None:
        if not self.pending_evaluation or not self.transcript_buffer:
            return None
        self.pending_evaluation = False

        # Transcript arrived — run Smart Turn on buffered audio
        audio = np.array(self.audio_buffer, dtype=np.float32)
        result = self.smart_turn.predict(audio)
        prob = result["probability"]
        log.info(f"[SmartTurn] Pending transcript arrived, prob={prob:.3f} (threshold={self.threshold})")
        if prob >= self.threshold:
            self.waiting_for_more_speech = False
            self.wait_start_time = None
            return {"turn_complete": True, "probability": prob}
        else:
            # Not confident — let the fallback timer handle it
            return None

    def add_transcript(self, text: str):
        text = text.strip()
        if text:
            self.transcript_buffer.append(text)

    def get_transcript(self) -> str:
        return " ".join(self.transcript_buffer)

    def buffer_audio(self, pcm_bytes: bytes):
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        self.audio_buffer.extend(samples.astype(np.float32) / 32768.0)

    def check_timeout(self) -> bool:
        if not self.waiting_for_more_speech or not self.wait_start_time:
            return False
        if not self.transcript_buffer:
            return False
        if (time.time() - self.wait_start_time) >= SMART_TURN_FALLBACK_SECS:
            log.info(f"[SmartTurn] Timeout fallback after {SMART_TURN_FALLBACK_SECS}s")
            self.last_smart_turn_result = {"turn_complete": True, "probability": 0.0, "timeout": True}
            self.is_speaking = False
            self.waiting_for_more_speech = False
            return True
        return False

    def run_smart_turn(self) -> dict:
        if not self.audio_buffer:
            return {"prediction": 0, "probability": 0.0, "turn_complete": False}
        audio_array = np.array(self.audio_buffer, dtype=np.float32)
        result = self.smart_turn.predict(audio_array)
        turn_complete = result["probability"] >= self.threshold
        result["turn_complete"] = turn_complete
        self.last_smart_turn_result = result
        log.info(f"[SmartTurn] prob={result['probability']:.3f} threshold={self.threshold} complete={turn_complete}")
        if turn_complete:
            self.is_speaking = False
            self.waiting_for_more_speech = False
        else:
            self.waiting_for_more_speech = True
            self.wait_start_time = time.time()
        return result


class TurnState(Enum):
    IDLE = auto()
    LISTENING = auto()
    EVALUATING = auto()
    TURN_READY = auto()
    RESPONDING = auto()


class TurnCoordinator:
    """Thread-safe turn detection state machine."""
    # Adapted from Claude Avatar by Olivia (github.com/taygetea)

    def __init__(self, turn_manager: SmartTurnManager):
        self._lock = asyncio.Lock()
        self._state = TurnState.IDLE
        self._tm = turn_manager
        self._turn_ready = asyncio.Event()
        self._pending_transcript: str | None = None

    @property
    def state(self) -> TurnState:
        return self._state

    async def on_speech_start(self):
        async with self._lock:
            if self._state == TurnState.RESPONDING:
                return
            if self._state == TurnState.EVALUATING:
                # If we have a transcript waiting, complete the turn now —
                # new speech means the previous utterance was done.
                if self._tm.transcript_buffer:
                    log.info("[TurnCoord] Speech during EVALUATING with transcript — completing turn")
                    await self._try_complete_locked()
                    return
                self._tm.reset()
            self._state = TurnState.LISTENING
            self._tm.on_speech_started()

    async def on_speech_end(self) -> bool:
        async with self._lock:
            if self._state != TurnState.LISTENING:
                return False
            self._state = TurnState.EVALUATING
            result = self._tm.on_speech_ended()
            if result.get("turn_complete"):
                return await self._try_complete_locked()
            return False

    async def on_transcript_received(self):
        async with self._lock:
            if self._state == TurnState.LISTENING:
                # Transcript arrived while VAD still active (noise keeping it open).
                # Trust STT — transition to EVALUATING and run Smart Turn.
                log.info("[TurnCoord] Transcript received in LISTENING — forcing evaluation")
                self._state = TurnState.EVALUATING
                result = self._tm.on_speech_ended()
                if result.get("turn_complete"):
                    await self._try_complete_locked()
                return
            if self._state != TurnState.EVALUATING:
                return
            pending = self._tm.check_pending_evaluation()
            if pending and pending.get("turn_complete"):
                await self._try_complete_locked()

    async def check_timeout(self) -> bool:
        async with self._lock:
            if self._state == TurnState.LISTENING:
                # If we have a transcript and Smart Turn's fallback timer expired, complete
                if self._tm.transcript_buffer and self._tm.check_timeout():
                    log.info("[TurnCoord] Timeout in LISTENING with transcript — completing")
                    await self._try_complete_locked()
                    return True
                return False
            if self._state != TurnState.EVALUATING:
                return False
            if self._tm.check_timeout():
                await self._try_complete_locked()
                return True
            return False

    async def _try_complete_locked(self) -> bool:
        transcript = self._tm.get_transcript()
        if not transcript:
            return False
        self._pending_transcript = transcript
        self._state = TurnState.TURN_READY
        self._turn_ready.set()
        return True

    async def consume_turn(self) -> str | None:
        async with self._lock:
            if self._state != TurnState.TURN_READY:
                return None
            transcript = self._pending_transcript
            self._pending_transcript = None
            self._state = TurnState.RESPONDING
            self._turn_ready.clear()
            self._tm.reset()
            return transcript

    async def on_response_complete(self):
        async with self._lock:
            if self._state == TurnState.RESPONDING:
                self._state = TurnState.IDLE

    async def reset(self):
        async with self._lock:
            self._state = TurnState.IDLE
            self._pending_transcript = None
            self._turn_ready.clear()
            self._tm.reset()

    async def wait_for_turn(self, timeout: float = 0.2) -> bool:
        try:
            await asyncio.wait_for(self._turn_ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False


# ── OpenRouter streaming LLM ──────────────────────────────────────────────────

VOICE_SYSTEM_PROMPT = (
    "You are in a voice chat. Keep responses SHORT — 1-2 sentences max. "
    "Be conversational, not monologue-y. Let the user talk. "
    "No markdown, no code blocks, no bullet points, no lists. "
    "If the user interrupted you, acknowledge it naturally."
)

class OpenRouterVoiceLLM:
    """Streams chat completions from OpenRouter for voice use."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._history: list[dict] = [{"role": "system", "content": VOICE_SYSTEM_PROMPT}]

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(keepalive_timeout=300)
            self._session = aiohttp.ClientSession(connector=connector)

    async def warm_up(self):
        """Pre-warm TCP+TLS connection to OpenRouter."""
        await self._ensure_session()
        try:
            async with self._session.head("https://openrouter.ai/api/v1/models", timeout=aiohttp.ClientTimeout(total=5)):
                pass
            log.info("[OR] Connection pre-warmed")
        except Exception as e:
            log.warning(f"[OR] Warm-up failed (non-fatal): {e}")

    async def stream(self, user_text: str, on_token: Callable[[str], Any] = None,
                     should_stop: Callable[[], bool] = None) -> str:
        """Stream a completion, calling on_token for each token. Returns full text."""
        await self._ensure_session()
        self._history.append({"role": "user", "content": user_text})

        t0 = time.time()
        payload = {
            "model": VOICE_LLM_MODEL,
            "messages": self._history,
            "stream": True,
            "max_tokens": 200,
            "temperature": 0.6,
            "reasoning": {"effort": "none"},
            "provider": {
                "order": ["anthropic"],
                "allow_fallbacks": True,
                "sort": "latency",
            },
        }
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        }

        full_text = ""
        first_token_logged = False
        interrupted = False

        log.info(f"[OR] Sending request to {VOICE_LLM_MODEL} t+{(time.time()-t0)*1000:.0f}ms")

        async with self._session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload, headers=headers,
        ) as resp:
            log.info(f"[OR] HTTP response received t+{(time.time()-t0)*1000:.0f}ms status={resp.status}")
            if resp.status != 200:
                body = await resp.text()
                log.error(f"[OR] Error {resp.status}: {body[:200]}")
                return ""

            async for raw_line in resp.content:
                if should_stop and should_stop():
                    log.info(f"[OR] Interrupted at {len(full_text)} chars")
                    interrupted = True
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    token = delta.get("content", "")
                    if token:
                        if not first_token_logged:
                            log.info(f"[OR] First token t+{(time.time()-t0)*1000:.0f}ms: {token!r}")
                            first_token_logged = True
                        full_text += token
                        if on_token:
                            await on_token(token)
                except json.JSONDecodeError:
                    pass

        if interrupted:
            log.info(f"[OR] Stream interrupted t+{(time.time()-t0)*1000:.0f}ms, {len(full_text)} chars sent")
            # Still record partial response in history
            if full_text:
                self._history.append({"role": "assistant", "content": full_text + "..."})
        else:
            log.info(f"[OR] Stream complete t+{(time.time()-t0)*1000:.0f}ms, {len(full_text)} chars")
            self._history.append({"role": "assistant", "content": full_text})
        # Keep history manageable
        if len(self._history) > 20:
            self._history = self._history[:1] + self._history[-18:]
        return full_text

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def reset(self):
        self._history = [{"role": "system", "content": VOICE_SYSTEM_PROMPT}]


# ── Streaming TTS ─────────────────────────────────────────────────────────────
# Adapted from Claude Avatar by Olivia (github.com/taygetea)

class StreamingTTS:
    """Persistent ElevenLabs TTS WebSocket with token-level streaming."""

    def __init__(self, bridge_queue: deque, voice_id: str = None):
        self.bridge_queue = bridge_queue  # outgoing audio to Discord
        self.voice_id = voice_id or VOICE_ID
        self.ws = None
        self.receive_task = None
        self.tts_finished = asyncio.Event()
        self._response_active = False
        self._response_generation_id = 0
        self.bridge_audio_bytes = 0
        self.audio_start_time: float | None = None
        self._quota_error = False  # set when ElevenLabs returns a quota/credit error

    async def connect(self):
        uri = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream-input"
            f"?model_id={TTS_MODEL_ID}&output_format=pcm_24000&inactivity_timeout=180"
        )
        self.ws = await websockets.connect(uri)
        init_msg = {
            "text": " ",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            "xi_api_key": ELEVENLABS_API_KEY,
        }
        await self.ws.send(json.dumps(init_msg))
        self.receive_task = asyncio.create_task(self._receive_audio())
        log.info("[TTS] Connected to ElevenLabs")

    async def _receive_audio(self):
        try:
            async for message in self.ws:
                if not self._response_active:
                    continue
                if isinstance(message, bytes):
                    if self.audio_start_time is None:
                        self.audio_start_time = time.time()
                        log.info(f"[TIMING] TTS first audio bytes received ({len(message)} bytes)")
                    self.bridge_audio_bytes += len(message)
                    self.bridge_queue.append(message)
                else:
                    data = json.loads(message)
                    if data.get("audio"):
                        audio_data = base64.b64decode(data["audio"])
                        if self.audio_start_time is None:
                            self.audio_start_time = time.time()
                            log.info(f"[TIMING] TTS first audio bytes received ({len(audio_data)} bytes)")
                        self.bridge_audio_bytes += len(audio_data)
                        self.bridge_queue.append(audio_data)
                    if data.get("isFinal"):
                        log.info(f"[TIMING] TTS isFinal, total={self.bridge_audio_bytes} bytes")
                        self.tts_finished.set()
                        self._response_active = False
        except websockets.exceptions.ConnectionClosed as e:
            log.warning(f"[TTS] WebSocket closed: code={e.code} reason={e.reason!r}")
            if _is_elevenlabs_quota_error(e):
                self._quota_error = True
        except Exception as e:
            log.warning(f"[TTS] Error: {e}")
            if _is_elevenlabs_quota_error(e):
                self._quota_error = True
        finally:
            self.tts_finished.set()
            self._response_active = False

    async def prepare_response(self):
        self.tts_finished.clear()
        self.bridge_audio_bytes = 0
        self.audio_start_time = None
        self._response_generation_id += 1
        self._response_active = True

    async def send_token(self, text: str):
        if self.ws and self._response_active:
            await self.ws.send(json.dumps({"text": text, "try_trigger_generation": True}))

    async def finish_response(self):
        if self.ws and self._response_active:
            await self.ws.send(json.dumps({"text": ""}))
        await self.tts_finished.wait()
        self._response_active = False
        # ElevenLabs closes the WS after each generation (isFinal).
        # Pre-reconnect so the next turn doesn't pay the connection cost.
        if not self.is_connected():
            await self.connect()

    async def cancel_response(self):
        if not self._response_active:
            return
        self._response_active = False
        self._response_generation_id += 1
        self.tts_finished.set()
        # Close and reconnect — ElevenLabs WS is in a bad state after
        # a partial generation (no isFinal sent).
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        await self.connect()

    async def close(self):
        if self._response_active:
            await self.cancel_response()
        if self.ws:
            await self.ws.close()
            self.ws = None
        if self.receive_task:
            self.receive_task.cancel()
            try:
                await self.receive_task
            except asyncio.CancelledError:
                pass
            self.receive_task = None

    def is_connected(self) -> bool:
        return self.ws is not None and self.ws.state == WebSocketState.OPEN


class KokoroTTS:
    """Local Kokoro TTS fallback — same interface as StreamingTTS.

    Tokens accumulate in a buffer. On finish_response(), the full text is
    synthesized sentence-by-sentence via Kokoro's pipeline and pushed as
    24kHz 16-bit PCM chunks to the bridge queue.  Because Kokoro generates
    an entire sentence in ~50ms on GPU, latency is still very low.
    """

    _pipeline = None  # class-level singleton — loaded once

    def __init__(self, bridge_queue: deque, voice: str = "af_heart"):
        self.bridge_queue = bridge_queue
        self.voice = voice
        self._buffer: list[str] = []
        self._response_active = False
        self.tts_finished = asyncio.Event()
        self.bridge_audio_bytes = 0
        self.audio_start_time: float | None = None
        self._synth_task: asyncio.Task | None = None

    @classmethod
    def _load_pipeline(cls):
        if cls._pipeline is None:
            import warnings
            warnings.filterwarnings("ignore")
            from kokoro import KPipeline
            cls._pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
            log.info("[KokoroTTS] Pipeline loaded")

    async def connect(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._load_pipeline)
        log.info("[KokoroTTS] Ready (local fallback)")

    def is_connected(self) -> bool:
        return self._pipeline is not None

    async def prepare_response(self):
        self.tts_finished.clear()
        self._buffer.clear()
        self.bridge_audio_bytes = 0
        self.audio_start_time = None
        self._response_active = True

    async def send_token(self, text: str):
        if self._response_active:
            self._buffer.append(text)

    async def finish_response(self):
        if not self._response_active:
            self.tts_finished.set()
            return
        full_text = "".join(self._buffer).strip()
        if not full_text:
            self._response_active = False
            self.tts_finished.set()
            return
        # Synthesize in a thread to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._synthesize, full_text)
        except Exception as e:
            log.warning(f"[KokoroTTS] Synthesis error: {e}")
        self._response_active = False
        self.tts_finished.set()

    def _synthesize(self, text: str):
        """Run Kokoro synthesis (blocking, called in executor thread)."""
        import numpy as np
        for result in self._pipeline(text, voice=self.voice):
            if not self._response_active:
                break
            audio = result.audio.numpy() if hasattr(result.audio, "numpy") else np.array(result.audio)
            pcm = (audio * 32767).astype(np.int16).tobytes()
            if self.audio_start_time is None:
                self.audio_start_time = time.time()
                log.info(f"[KokoroTTS] First audio chunk ({len(pcm)} bytes)")
            self.bridge_audio_bytes += len(pcm)
            self.bridge_queue.append(pcm)

    async def cancel_response(self):
        self._response_active = False
        self._buffer.clear()
        self.tts_finished.set()

    async def close(self):
        await self.cancel_response()


# ElevenLabs quota/credit error detection
_ELEVENLABS_QUOTA_CODES = {1008, 4003, 4008}
_ELEVENLABS_QUOTA_KEYWORDS = {"quota", "limit", "credit", "insufficient", "exceeded", "payment"}


def _is_elevenlabs_quota_error(exc: Exception) -> bool:
    """Check if an exception indicates ElevenLabs is out of credits/quota."""
    if isinstance(exc, websockets.exceptions.ConnectionClosed):
        if exc.code in _ELEVENLABS_QUOTA_CODES:
            return True
        reason = (exc.reason or "").lower()
        if any(kw in reason for kw in _ELEVENLABS_QUOTA_KEYWORDS):
            return True
    msg = str(exc).lower()
    return any(kw in msg for kw in _ELEVENLABS_QUOTA_KEYWORDS)


# ── Discord audio I/O ─────────────────────────────────────────────────────────
# Adapted from Claude Avatar by Olivia (github.com/taygetea)

class VoiceEngineSink(voice_recv.AudioSink):
    """Receives per-packet Discord audio and forwards to the voice manager."""

    SPEECH_END_DEBOUNCE_MS = 500

    def __init__(self, manager: "VoiceManager"):
        super().__init__()
        self.manager = manager
        self.resampler = AudioResampler()
        self.allowed_user_ids = set(VOICE_ALLOWED_USER_IDS)
        self.user_names: dict[int, str] = {}
        self._speech_end_timers: dict[int, threading.Timer] = {}

    def wants_opus(self) -> bool:
        return False

    def write(self, user: discord.User | discord.Member | None, data: voice_recv.VoiceData):
        if user is None or user.bot:
            return
        if self.allowed_user_ids and user.id not in self.allowed_user_ids:
            return
        if user.id not in self.user_names:
            self.user_names[user.id] = getattr(user, "display_name", None) or user.name
            log.info(f"[VoiceSink] First audio from {self.user_names[user.id]} (id={user.id})")

        pcm_data = data.pcm
        if not pcm_data:
            return

        resampled = self.resampler.discord_to_engine(pcm_data)
        if resampled:
            display_name = self.user_names.get(user.id, f"User {user.id}")
            self.manager._on_audio(user.id, display_name, resampled)

    def cleanup(self):
        for timer in self._speech_end_timers.values():
            timer.cancel()
        self._speech_end_timers.clear()

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_start(self, member: discord.Member):
        # Disabled — using Silero VAD in _on_audio instead of Discord's voice activity
        pass

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_stop(self, member: discord.Member):
        # Disabled — using Silero VAD in _on_audio instead of Discord's voice activity
        pass


class TTSAudioSource(discord.AudioSource):
    """Feeds TTS audio from engine to Discord voice."""

    FRAME_SIZE = 960 * 2 * 2  # 3840 bytes per 20ms frame at 48kHz stereo

    DUCK_VOLUME = 0.15  # ~-16dB when user is speaking

    def __init__(self, outgoing_queue: deque):
        self.queue = outgoing_queue
        self.buffer = b""
        self.resampler = AudioResampler()
        self.playback_start_time: float | None = None  # when first real audio sent to Discord
        self._has_played_audio = False
        self.ducking = False  # set True when user is speaking

    def reset_playback_tracking(self):
        self.playback_start_time = None
        self._has_played_audio = False

    def read(self) -> bytes:
        had_data = False
        while len(self.buffer) < self.FRAME_SIZE:
            try:
                chunk = self.queue.popleft()
                had_data = True
            except IndexError:
                break
            self.buffer += self.resampler.engine_to_discord(chunk)

        if len(self.buffer) >= self.FRAME_SIZE:
            frame = self.buffer[: self.FRAME_SIZE]
            self.buffer = self.buffer[self.FRAME_SIZE :]
            if not self._has_played_audio and had_data:
                self.playback_start_time = time.time()
                self._has_played_audio = True
            return frame
        else:
            silence_needed = self.FRAME_SIZE - len(self.buffer)
            frame = self.buffer + (b"\x00" * silence_needed)
            self.buffer = b""
            return frame

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        pass


# ── Voice Manager ─────────────────────────────────────────────────────────────

class VoiceManager:
    """
    Coordinates the voice pipeline: Discord audio <-> STT <-> Claude Code <-> TTS.

    Uses the existing _PersistentProcess from ClaudeBridge for Claude Code interaction,
    so voice shares the same context as text messages.
    """

    def __init__(self, client: discord.Client, bridge: Any):
        self.client = client
        self.bridge = bridge  # ClaudeBridge instance
        self.voice_client: voice_recv.VoiceRecvClient | None = None

        self._outgoing_audio: deque[bytes] = deque(maxlen=500)
        self._stt_manager: MultiUserSTTManager | None = None
        self._turn_coordinator: TurnCoordinator | None = None
        self._tts: StreamingTTS | None = None
        self._or_llm: OpenRouterVoiceLLM | None = None
        self._audio_source: TTSAudioSource | None = None

        self._running = False
        self._main_task: asyncio.Task | None = None
        self._overlapping_speech = False
        self._claude_responding = False
        self._last_tts_len = 0
        self._current_voice_channel_id: int = 0
        self._current_voice_id: str = VOICE_ID  # persists across TTS reconnects
        self._use_local_tts: bool = False  # fallback to Kokoro when ElevenLabs quota runs out
        self._last_response_text: str = ""  # for Kokoro re-synthesis on ElevenLabs failure
        self._caller_ctx_key: str | None = None  # reuse caller's Claude Code process
        self._interrupt_transcript: str | None = None  # transcript that interrupted us
        self._playback_tasks: list[asyncio.Task] = []  # active playback tasks
        self._playback_stop = False  # signal to stop all playback

        # Per-user Silero VAD state (replaces Discord's voice activity)
        self._user_vad_speaking: dict[int, bool] = {}
        self._user_vad_silence_chunks: dict[int, int] = {}
        self._user_vad_speech_chunks: dict[int, int] = {}
        self._user_vad_buffer: dict[int, list] = {}  # accumulate samples until 512
        self._user_vad_speech_start: dict[int, float] = {}  # when VAD speech started
        self._silero_vad = None  # loaded lazily
        self._vad_chunk_size = 512  # Silero requires exactly 512 samples at 16kHz

        # Engine loop ref for thread-safe callbacks
        self._loop: asyncio.AbstractEventLoop | None = None

        log.info(f"[Voice] Mode: {VOICE_MODE}")

    async def start(self):
        """Initialize voice pipeline components."""
        if not ELEVENLABS_API_KEY:
            log.warning("[Voice] ELEVENLABS_API_KEY not set, voice disabled")
            return
        if not SMART_TURN_MODEL_PATH.exists():
            log.warning(f"[Voice] Smart Turn model not found at {SMART_TURN_MODEL_PATH}")
            log.warning("[Voice] Download with: curl -L -o models/smart-turn-v3.2-cpu.onnx https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx")
            return

        self._loop = asyncio.get_event_loop()

        # Init Silero VAD for speech detection (replaces Discord's voice activity)
        import torch
        from silero_vad import load_silero_vad
        self._silero_vad = load_silero_vad()
        self._silero_vad_lock = threading.Lock()
        log.info("[Voice] Silero VAD loaded for speech gating")

        # Init STT
        self._stt_manager = MultiUserSTTManager(on_transcript=self._on_transcript)

        # Init turn detection
        turn_manager = SmartTurnManager(threshold=SMART_TURN_THRESHOLD)
        self._turn_coordinator = TurnCoordinator(turn_manager)

        # Init TTS
        self._tts = StreamingTTS(self._outgoing_audio, voice_id=self._current_voice_id)
        await self._tts.connect()

        # Init OpenRouter LLM if needed
        if VOICE_MODE == "openrouter":
            self._or_llm = OpenRouterVoiceLLM()
            await self._or_llm.warm_up()
            log.info(f"[Voice] OpenRouter LLM ready, model={VOICE_LLM_MODEL}")

        self._running = True
        self._main_task = asyncio.create_task(self._main_loop())
        log.info("[Voice] Pipeline started")
        # Write diagnostic to file for debugging
        Path(__file__).parent.joinpath("voice_debug.txt").write_text(
            f"Pipeline started at {time.strftime('%H:%M:%S')}\n"
            f"_running={self._running}\n"
            f"STT manager: {self._stt_manager is not None}\n"
            f"Turn coordinator: {self._turn_coordinator is not None}\n"
            f"TTS: {self._tts is not None}, connected={self._tts.is_connected() if self._tts else '?'}\n"
        )

    async def stop(self):
        self._running = False
        if self._main_task:
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass
        if self._tts:
            await self._tts.close()
        if self._or_llm:
            await self._or_llm.close()
        if self._stt_manager:
            await self._stt_manager.stop_all()
        await self._leave_channel()
        log.info("[Voice] Pipeline stopped")

    # ── Discord voice connection ──────────────────────────────────────────

    async def join_channel(self, channel: discord.VoiceChannel, caller_ctx_key: str = None):
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()

        # Also clean up any stale guild-level voice client (e.g. after bot restart)
        existing = channel.guild.voice_client
        if existing:
            try:
                await existing.disconnect(force=True)
            except Exception:
                pass

        # Reset STT sessions on rejoin to prevent stale/reconnect-storming sessions
        if self._stt_manager:
            await self._stt_manager.stop_all()

        self.voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
        self._current_voice_channel_id = channel.id
        self._caller_ctx_key = caller_ctx_key  # reuse caller's Claude Code process
        log.info(f"[Voice] Joined {channel.name} (caller_ctx={caller_ctx_key})")

        sink = VoiceEngineSink(self)
        self.voice_client.listen(sink)

        self._audio_source = TTSAudioSource(self._outgoing_audio)
        self.voice_client.play(self._audio_source)

    async def _leave_channel(self):
        if self.voice_client:
            if self.voice_client.is_listening():
                self.voice_client.stop_listening()
            if self.voice_client.is_playing():
                self.voice_client.stop()
            await self.voice_client.disconnect()
            self.voice_client = None
            self._current_voice_channel_id = 0
            log.info("[Voice] Left channel")

    async def on_voice_state_update(self, member: discord.Member,
                                     before: discord.VoiceState,
                                     after: discord.VoiceState):
        """Handle voice state changes for auto-join/leave (only if configured)."""
        if not self._running or not VOICE_CHANNEL_IDS or not VOICE_ALLOWED_USER_IDS:
            return

        if member.id == self.client.user.id or member.id not in VOICE_ALLOWED_USER_IDS:
            return

        before_ch = before.channel.id if before.channel else None
        after_ch = after.channel.id if after.channel else None

        if after_ch in VOICE_CHANNEL_IDS and before_ch not in VOICE_CHANNEL_IDS:
            if not self.voice_client or not self.voice_client.is_connected():
                log.info(f"[Voice] {member.display_name} joined — connecting")
                await self.join_channel(after.channel)

        elif before_ch in VOICE_CHANNEL_IDS and after_ch not in VOICE_CHANNEL_IDS:
            if self.voice_client and self.voice_client.is_connected():
                channel = self.voice_client.channel
                if channel:
                    remaining = [m for m in channel.members if m.id in VOICE_ALLOWED_USER_IDS]
                    if not remaining:
                        log.info("[Voice] No allowed users remain — leaving")
                        await self._leave_channel()

    async def check_user_presence(self):
        """Join if an allowed user is already in a voice channel."""
        for guild in self.client.guilds:
            for ch_id in VOICE_CHANNEL_IDS:
                channel = guild.get_channel(ch_id)
                if channel and isinstance(channel, discord.VoiceChannel):
                    for member in channel.members:
                        if member.id in VOICE_ALLOWED_USER_IDS:
                            log.info(f"[Voice] {member.display_name} already in channel — joining")
                            await self.join_channel(channel)
                            return

    # ── Audio callbacks (called from Discord voice thread) ────────────────

    def _on_audio(self, user_id: int, display_name: str, pcm_bytes: bytes):
        """Called from VoiceEngineSink.write() — runs in Discord voice thread."""
        import torch

        # Run Silero VAD to detect actual speech vs noise
        # Buffer samples until we have 512 (Silero's required chunk size at 16kHz)
        if self._silero_vad is not None:
            samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            buf = self._user_vad_buffer.get(user_id, [])
            buf.extend(samples)
            self._user_vad_buffer[user_id] = buf

            while len(buf) >= self._vad_chunk_size:
                chunk = np.array(buf[:self._vad_chunk_size], dtype=np.float32)
                del buf[:self._vad_chunk_size]
                tensor = torch.from_numpy(chunk)
                with self._silero_vad_lock:
                    prob = self._silero_vad(tensor, STT_SAMPLE_RATE).item()

                was_speaking = self._user_vad_speaking.get(user_id, False)
                # Each VAD chunk is 512/16000 = 32ms
                chunk_ms = 32
                if prob >= VAD_THRESHOLD:
                    self._user_vad_silence_chunks[user_id] = 0
                    speech_chunks = self._user_vad_speech_chunks.get(user_id, 0) + 1
                    self._user_vad_speech_chunks[user_id] = speech_chunks
                    if not was_speaking and speech_chunks >= max(1, VAD_MIN_SPEECH_MS // chunk_ms):
                        self._user_vad_speaking[user_id] = True
                        self._user_vad_speech_start[user_id] = time.time()
                        log.info(f"[SileroVAD] Speech start: {display_name} (prob={prob:.2f})")
                        self._on_speech_start(user_id, display_name)
                    elif was_speaking:
                        # Check timeout — force speech_end if VAD has been active too long (noise floor)
                        started = self._user_vad_speech_start.get(user_id, 0)
                        if started and (time.time() - started) > VAD_MAX_SPEECH_S:
                            self._user_vad_speaking[user_id] = False
                            self._user_vad_speech_chunks[user_id] = 0
                            log.info(f"[SileroVAD] Speech timeout ({VAD_MAX_SPEECH_S}s): {display_name} — forcing end")
                            self._on_speech_end(user_id)
                else:
                    self._user_vad_speech_chunks[user_id] = 0
                    silence_chunks = self._user_vad_silence_chunks.get(user_id, 0) + 1
                    self._user_vad_silence_chunks[user_id] = silence_chunks
                    if was_speaking and silence_chunks >= max(1, VAD_MIN_SILENCE_MS // chunk_ms):
                        self._user_vad_speaking[user_id] = False
                        log.info(f"[SileroVAD] Speech end: {display_name} (silence={silence_chunks * chunk_ms}ms)")
                        self._on_speech_end(user_id)

        # Forward audio to STT (always, even during noise — STT has its own VAD)
        if self._loop and self._stt_manager:
            asyncio.run_coroutine_threadsafe(
                self._stt_manager.send_audio(user_id, display_name, pcm_bytes),
                self._loop,
            )
        # Buffer audio for Smart Turn
        if self._turn_coordinator:
            self._turn_coordinator._tm.buffer_audio(pcm_bytes)

    def _on_speech_start(self, user_id: int, display_name: str):
        """Called when Silero VAD confirms speech."""
        if self._loop and self._turn_coordinator:
            asyncio.run_coroutine_threadsafe(
                self._turn_coordinator.on_speech_start(), self._loop
            )
            if self._claude_responding:
                self._overlapping_speech = True

    def _on_speech_end(self, user_id: int):
        """Called when Silero VAD confirms speech ended."""
        if self._loop and self._turn_coordinator:
            asyncio.run_coroutine_threadsafe(
                self._handle_speech_end(user_id), self._loop
            )
        if self._loop and self._stt_manager:
            asyncio.run_coroutine_threadsafe(
                self._stt_manager.force_commit(user_id), self._loop
            )

    def _on_transcript(self, user_id: int, text: str):
        """Called from STT session when transcript arrives."""
        if self._claude_responding:
            # User spoke while we're responding — interrupt and pivot
            log.info(f"[Voice] Interrupt transcript while responding: {text[:80]!r}")
            self._interrupt_transcript = text
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._handle_interrupt(), self._loop
                )
            return
        if self._turn_coordinator:
            self._turn_coordinator._tm.add_transcript(text)
            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._turn_coordinator.on_transcript_received(), self._loop
                )

    async def _handle_interrupt(self):
        """Interrupt Claude response and stop TTS when user starts speaking."""
        log.info("[Voice] Interrupting — user spoke during response")
        if self._tts:
            await self._tts.cancel_response()
            self._outgoing_audio.clear()
        # Also clear the already-resampled buffer in the audio source
        if self._audio_source:
            self._audio_source.buffer = b""
        # Interrupt the right process — caller's or voice's own
        pp = None
        if self._caller_ctx_key:
            pp = self.bridge.get_process(self._caller_ctx_key)
        if not pp:
            ctx_key = f"voice:{self._current_voice_channel_id}" if self._current_voice_channel_id else "voice:0"
            pp = self.bridge._procs.get(ctx_key)
        if pp and pp.is_busy:
            await pp.interrupt()

    async def _handle_speech_end(self, user_id: int):
        turn_ready = await self._turn_coordinator.on_speech_end()
        if turn_ready:
            log.debug("[Voice] Turn ready after speech end")

    # ── Audio file / URL playback ─────────────────────────────────────────

    async def play_file(self, path: str, volume: float = 1.0) -> str:
        """Play an audio file (any format ffmpeg supports) into the voice channel.
        Layers on top of any existing playback. Returns status message."""
        if not self.voice_client or not self.voice_client.is_connected():
            return "Not connected to a voice channel"
        if not Path(path).exists():
            return f"File not found: {path}"
        self._cleanup_done_tasks()
        task = asyncio.create_task(self._stream_file_audio(path, volume))
        self._playback_tasks.append(task)
        return f"Playing: {Path(path).name}"

    async def play_url(self, url: str, volume: float = 0.5) -> str:
        """Stream audio from URL (YouTube etc.) via yt-dlp.
        Layers on top of any existing playback. Returns status message."""
        if not self.voice_client or not self.voice_client.is_connected():
            return "Not connected to a voice channel"
        self._cleanup_done_tasks()
        task = asyncio.create_task(self._stream_url_audio(url, volume))
        self._playback_tasks.append(task)
        return f"Streaming from URL..."

    def _cleanup_done_tasks(self):
        self._playback_tasks = [t for t in self._playback_tasks if not t.done()]

    async def stop_playback(self):
        """Stop all current playback."""
        self._playback_stop = True
        for task in self._playback_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._playback_tasks.clear()
        self._playback_stop = False

    async def switch_voice(self, voice_name: str) -> str:
        """Switch TTS voice by name. Reconnects the TTS WebSocket."""
        name = voice_name.lower().strip()
        if name not in VOICE_REGISTRY:
            available = ", ".join(f"{k} ({v})" for k, v in VOICE_DESCRIPTIONS.items())
            return f"Unknown voice '{voice_name}'. Available: {available}"
        voice_id = VOICE_REGISTRY[name]
        desc = VOICE_DESCRIPTIONS.get(name, name)
        self._current_voice_id = voice_id  # persist across reconnects
        if self._tts and isinstance(self._tts, StreamingTTS):
            self._tts.voice_id = voice_id
            # Reconnect with new voice
            if self._tts.ws:
                try:
                    await self._tts.ws.close()
                except Exception:
                    pass
                self._tts.ws = None
            await self._tts.connect()
        # If using KokoroTTS, voice switching doesn't apply (local voices are different)
        log.info(f"[Voice] Switched to voice: {name} ({desc})")
        return f"Switched to {name} voice ({desc})"

    async def _stream_file_audio(self, path: str, volume: float = 1.0):
        """Decode an audio file with ffmpeg and push 24kHz mono PCM into the queue."""
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", path,
            "-f", "s16le", "-ar", "24000", "-ac", "1",
            "-loglevel", "error", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            chunk_size = 2400  # 50ms at 24kHz mono (2 bytes/sample)
            log.info(f"[Playback] Started: {path}")
            while not self._playback_stop:
                data = await proc.stdout.read(chunk_size)
                if not data:
                    break
                if volume != 1.0:
                    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                    samples *= volume
                    data = np.clip(samples, -32768, 32767).astype(np.int16).tobytes()
                self._outgoing_audio.append(data)
                # Pace to ~realtime so we don't flood the buffer
                await asyncio.sleep(0.04)
            log.info(f"[Playback] Finished: {path}")
        except asyncio.CancelledError:
            log.info(f"[Playback] Cancelled: {path}")
        finally:
            proc.kill()
            await proc.wait()

    async def _stream_url_audio(self, url: str, volume: float = 0.5):
        """Stream audio from a URL via yt-dlp piped to ffmpeg."""
        # yt-dlp outputs to stdout, piped into ffmpeg for PCM conversion
        proc = await asyncio.create_subprocess_shell(
            f'yt-dlp -x -o - "{url}" 2>nul | ffmpeg -i pipe:0'
            f' -f s16le -ar 24000 -ac 1 -loglevel error -',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            chunk_size = 2400
            log.info(f"[Playback] Streaming URL: {url}")
            while not self._playback_stop:
                data = await proc.stdout.read(chunk_size)
                if not data:
                    break
                if volume != 1.0:
                    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                    samples *= volume
                    data = np.clip(samples, -32768, 32767).astype(np.int16).tobytes()
                self._outgoing_audio.append(data)
                await asyncio.sleep(0.04)
            log.info(f"[Playback] URL finished: {url}")
        except asyncio.CancelledError:
            log.info(f"[Playback] URL cancelled: {url}")
        finally:
            proc.kill()
            await proc.wait()

    # ── Main loop ─────────────────────────────────────────────────────────

    async def _process_turn(self, transcript: str):
        """Process a single turn: send to LLM, stream TTS response."""
        t_turn = time.time()
        log.info(f"[TIMING] Turn consumed: {transcript[:100]!r}")

        # Prepare TTS — use local Kokoro fallback if ElevenLabs quota exhausted
        if self._use_local_tts:
            if not self._tts or not isinstance(self._tts, KokoroTTS):
                self._tts = KokoroTTS(self._outgoing_audio)
                await self._tts.connect()
            await self._tts.prepare_response()
        elif self._tts and self._tts.is_connected():
            await self._tts.prepare_response()
        else:
            try:
                self._tts = StreamingTTS(self._outgoing_audio, voice_id=self._current_voice_id)
                await self._tts.connect()
                await self._tts.prepare_response()
            except Exception as e:
                if _is_elevenlabs_quota_error(e):
                    log.warning(f"[TTS] ElevenLabs quota/credit error: {e} — switching to local Kokoro TTS")
                    self._use_local_tts = True
                    self._tts = KokoroTTS(self._outgoing_audio)
                    await self._tts.connect()
                    await self._tts.prepare_response()
                else:
                    raise
        log.info(f"[TIMING] TTS ready t+{(time.time()-t_turn)*1000:.0f}ms (local={self._use_local_tts})")

        self._claude_responding = True
        self._interrupt_transcript = None
        self._reset_tts_tracking()
        self._turn_t0 = t_turn

        try:
            if VOICE_MODE == "openrouter":
                await self._respond_openrouter(transcript, t_turn)
            else:
                await self._respond_claude_code(transcript, t_turn)

            if self._tts and self._tts._response_active:
                t_finish_start = time.time()
                await self._tts.finish_response()
                log.info(f"[TIMING] TTS finish_response took {(time.time()-t_finish_start)*1000:.0f}ms")

            # Check if ElevenLabs hit quota mid-response → immediately re-synth with Kokoro
            if isinstance(self._tts, StreamingTTS) and self._tts._quota_error:
                log.warning("[TTS] ElevenLabs quota error — immediately re-synthesizing with Kokoro")
                self._use_local_tts = True
                # Get the full response text that was streamed but never spoken
                response_text = getattr(self, '_last_response_text', '')
                if response_text:
                    self._tts = KokoroTTS(self._outgoing_audio)
                    await self._tts.connect()
                    await self._tts.prepare_response()
                    await self._tts.send_token(response_text)
                    await self._tts.finish_response()
                    log.info(f"[TTS] Kokoro re-synthesized: {response_text[:80]!r}")

        except Exception as e:
            log.exception(f"[Voice] Error during response: {e}")
            if self._tts:
                await self._tts.cancel_response()

        finally:
            self._claude_responding = False
            await self._turn_coordinator.on_response_complete()
            log.info(f"[TIMING] Full turn took {(time.time()-t_turn)*1000:.0f}ms")

    async def _main_loop(self):
        """Main voice pipeline loop: wait for turns, send to LLM, TTS response."""
        log.info(f"[Voice] Main loop started (mode={VOICE_MODE})")
        try:
            while self._running:
                # Check if there's an interrupt transcript to process immediately
                if self._interrupt_transcript:
                    transcript = f"[interrupting]\n{self._interrupt_transcript}"
                    self._interrupt_transcript = None
                    self._overlapping_speech = False
                    # Reset turn coordinator so it's not stuck in RESPONDING
                    await self._turn_coordinator.reset()
                    await self._process_turn(transcript)
                    continue

                ready = await self._turn_coordinator.wait_for_turn(timeout=0.3)
                if not ready:
                    await self._turn_coordinator.check_timeout()
                    continue

                transcript = await self._turn_coordinator.consume_turn()
                if not transcript:
                    continue

                if self._overlapping_speech:
                    transcript = f"[said while you were speaking]\n{transcript}"
                    self._overlapping_speech = False

                await self._process_turn(transcript)

        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("[Voice] Main loop error")
        log.info("[Voice] Main loop ended")

    async def _respond_openrouter(self, transcript: str, t_turn: float):
        """Stream response from OpenRouter to TTS with timing."""
        self._first_or_token_sent = False

        async def on_token(token: str):
            if not self._tts or not self._tts._response_active:
                return
            if not self._first_or_token_sent:
                self._first_or_token_sent = True
                log.info(f"[TIMING] First token → TTS t+{(time.time()-t_turn)*1000:.0f}ms: {token!r}")
            await self._tts.send_token(token)
            # Log when TTS first produces audio
            if self._tts.audio_start_time is not None and not getattr(self, '_first_audio_logged', False):
                log.info(f"[TIMING] First TTS audio t+{(time.time()-t_turn)*1000:.0f}ms")
                self._first_audio_logged = True

        self._first_audio_logged = False
        result = await self._or_llm.stream(
            transcript, on_token=on_token,
            should_stop=lambda: self._interrupt_transcript is not None,
        )
        self._last_response_text = result
        log.info(f"[TIMING] OR stream done t+{(time.time()-t_turn)*1000:.0f}ms, {len(result)} chars")

    async def _respond_claude_code(self, transcript: str, t_turn: float):
        """Stream response from Claude Code to TTS with timing."""
        # Reuse the caller's persistent process if available
        pp = None
        using_caller = False
        if self._caller_ctx_key:
            pp = self.bridge.get_process(self._caller_ctx_key)
            if pp:
                using_caller = True
                log.info(f"[TIMING] Reusing caller process {self._caller_ctx_key} t+{(time.time()-t_turn)*1000:.0f}ms")

        if not pp:
            # Fallback: create a dedicated voice process
            ctx_key = f"voice:{self._current_voice_channel_id}" if self._current_voice_channel_id else "voice:0"
            cwd = str(Path.home() / "Documents")
            pp = await self.bridge.get_or_create(
                ctx_key, cwd, system_prompt=VOICE_SYSTEM_PROMPT,
                model=VOICE_CLAUDE_MODEL,
                extra_env={"MAX_THINKING_TOKENS": "0"},
            )
            log.info(f"[TIMING] Using voice process {ctx_key} t+{(time.time()-t_turn)*1000:.0f}ms")

        if pp.is_busy:
            log.info("[Voice] Interrupting current response")
            await pp.interrupt()
            await asyncio.sleep(0.1)

        # When using caller's process, prepend voice context so Claude responds for TTS
        voice_msg = transcript
        if using_caller:
            voice_msg = f"[voice message — respond in 1-2 short sentences, no markdown. bot_action blocks still work for play_audio/play_url/stop_audio/join_voice etc.]\n{transcript}"

        result = await pp.send(voice_msg, on_text=self._stream_to_tts)
        log.info(f"[TIMING] Claude Code done t+{(time.time()-t_turn)*1000:.0f}ms")

        # Reset streaming state
        self._in_bot_action = False
        self._bot_action_buffer = ""
        self._pre_action_buffer = ""
        self._cc_first_token_logged = False

        if result.get("error"):
            log.warning(f"[Voice] Claude error: {result.get('error_message', '')[:100]}")

        # Save speakable text (strip bot_action blocks) for potential Kokoro re-synthesis
        full_text = result.get("text", "")
        import re
        speakable = re.sub(r"```bot_action\s*\n.*?\n```", "", full_text, flags=re.DOTALL).strip()
        self._last_response_text = speakable

        if "bot_action" in full_text:
            await self._execute_voice_actions(full_text)

    async def _execute_voice_actions(self, text: str):
        """Extract and execute bot_actions from a voice response."""
        import re
        action_re = re.compile(
            r"(?:```bot_action\s*\n(.*?)\n```|<bot_action>\s*(.*?)\s*</bot_action>)",
            re.DOTALL,
        )
        for m in action_re.finditer(text):
            raw = m.group(1) or m.group(2)
            try:
                act = json.loads(raw)
            except json.JSONDecodeError:
                log.warning(f"[Voice] Bad bot_action JSON: {raw[:100]}")
                continue

            action = act.get("action")
            log.info(f"[Voice] Executing bot_action: {action}")

            if action == "play_audio":
                path = act.get("path", "")
                volume = float(act.get("volume", 1.0))
                result = await self.play_file(path, volume=volume)
                log.info(f"[Voice] play_audio result: {result}")
            elif action == "play_url":
                url = act.get("url", "")
                volume = float(act.get("volume", 0.5))
                result = await self.play_url(url, volume=volume)
                log.info(f"[Voice] play_url result: {result}")
            elif action == "stop_audio":
                await self.stop_playback()
                log.info("[Voice] Playback stopped via voice command")
            elif action == "switch_voice":
                voice_name = act.get("voice", "")
                result = await self.switch_voice(voice_name)
                log.info(f"[Voice] switch_voice result: {result}")
            else:
                log.info(f"[Voice] Ignoring non-voice bot_action: {action}")

    async def _stream_to_tts(self, full_text: str):
        """Called with accumulating text from Claude Code. Streams delta to TTS.
        Suppresses bot_action blocks from being spoken."""
        if not self._tts or not self._tts._response_active:
            return
        delta = full_text[self._last_tts_len:]
        self._last_tts_len = len(full_text)
        if not delta:
            return

        # Detect and suppress bot_action blocks from TTS
        if not hasattr(self, '_in_bot_action'):
            self._in_bot_action = False
        if not hasattr(self, '_bot_action_buffer'):
            self._bot_action_buffer = ""

        if self._in_bot_action:
            self._bot_action_buffer += delta
            # Check if block ended
            if "```" in self._bot_action_buffer.split("```bot_action", 1)[-1]:
                # Block complete — don't send to TTS
                log.info(f"[Voice] Suppressed bot_action block from TTS")
                self._in_bot_action = False
                self._bot_action_buffer = ""
            return

        # Check if we're entering a bot_action block
        combined = (getattr(self, '_pre_action_buffer', '') + delta)
        self._pre_action_buffer = ""
        if "```bot_action" in combined:
            # Split: text before block goes to TTS, block gets suppressed
            before = combined.split("```bot_action")[0]
            if before.strip():
                await self._tts.send_token(before)
            self._in_bot_action = True
            self._bot_action_buffer = "```bot_action" + combined.split("```bot_action", 1)[1]
            return
        # Buffer a bit in case ``` is split across deltas
        if combined.endswith("`") or combined.endswith("``"):
            self._pre_action_buffer = combined
            return

        t_turn = getattr(self, '_turn_t0', None)
        elapsed = f" t+{(time.time()-t_turn)*1000:.0f}ms" if t_turn else ""
        if self._last_tts_len == len(full_text[:self._last_tts_len]):
            if not getattr(self, '_cc_first_token_logged', False):
                log.info(f"[TIMING] CC first token → TTS{elapsed}: {combined!r}")
                self._cc_first_token_logged = True
        await self._tts.send_token(combined)
        if self._tts.audio_start_time is not None and not getattr(self, '_cc_first_audio_logged', False):
            log.info(f"[TIMING] First TTS audio{elapsed}")
            self._cc_first_audio_logged = True

    def _reset_tts_tracking(self):
        self._last_tts_len = 0
        self._cc_first_audio_logged = False
        self._cc_first_token_logged = False
        self._first_audio_logged = False
        self._in_bot_action = False
        self._bot_action_buffer = ""
        self._pre_action_buffer = ""
        if self._audio_source:
            self._audio_source.reset_playback_tracking()
