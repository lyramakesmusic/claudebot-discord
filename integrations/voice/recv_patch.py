# -*- coding: utf-8 -*-
"""
Monkey-patch for discord-ext-voice-recv.

1. Jitter buffer: uses Opus PLC instead of flushing on packet loss.
2. Decode errors: falls back to PLC instead of crashing on OpusError.
3. DAVE E2EE: decrypts audio with davey after transport decryption.

Apply early in startup before any voice_recv imports:
    from integrations.voice.recv_patch import apply_patch
    apply_patch()

See: https://github.com/imayhaveborkedit/discord-ext-voice-recv/issues/35
"""

import logging
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from discord.ext.voice_recv.rtp import AudioPacket

log = logging.getLogger(__name__)

_PATCHED = False
_MAX_CONSECUTIVE_PLC = 5  # Max fake packets before we give up and flush (~100ms)


def apply_patch() -> None:
    """
    Apply the jitter buffer patch + DAVE E2EE decryption. Safe to call multiple times.
    Must be called before any VoiceRecvClient connections are made.
    """
    global _PATCHED
    if _PATCHED:
        return

    from discord.ext.voice_recv import opus, reader as vr_reader
    from discord.opus import OpusError
    from discord.utils import MISSING

    original_get_next_packet = opus.PacketDecoder._get_next_packet
    original_decode_packet = opus.PacketDecoder._decode_packet

    # ── DAVE E2EE decryption patch ────────────────────────────────────────
    # Instead of replacing the entire callback (which loses error handling),
    # we wrap AudioReader.start() to patch the decryptor's decrypt_rtp method
    # so DAVE decryption happens transparently inside the existing callback.

    try:
        import davey
        _has_davey = True
    except ImportError:
        _has_davey = False

    original_reader_start = vr_reader.AudioReader.start

    def _patched_reader_start(self) -> None:
        """Wrap reader start to install DAVE-aware decrypt_rtp on the decryptor."""
        original_reader_start(self)

        if not _has_davey:
            return

        reader_self = self
        decryptor = self.decryptor
        # Get the unbound method from the class so we always use the current box
        _decrypt_method_name = '_decrypt_rtp_' + reader_self.voice_client._connection.mode

        # One-shot logging flags to avoid per-packet spam
        _log_state = {"dave_not_ready": False, "dave_none": False, "dave_error": False,
                       "transport_errors": 0, "dave_ok_count": 0, "no_dave_count": 0}

        def _decrypt_rtp_with_dave(packet):
            """Transport decrypt (with live key sync), then DAVE decrypt."""

            # Sync transport key if discord.py rotated it
            try:
                conn = reader_self.voice_client._connection
                conn_key = conn.secret_key
                if conn_key is not MISSING:
                    current = bytes(conn_key)
                    if current != getattr(decryptor, '_synced_key', b''):
                        decryptor._synced_key = current
                        decryptor.update_secret_key(current)
                        log.info("[DAVE] Transport secret key synced")
            except Exception as e:
                log.debug("[DAVE] Key sync error: %s", e)

            # Transport decrypt — wrapped in try/except for robustness
            # On failure, re-raise so voice_recv's callback can handle it
            # (it catches CryptoError and drops the packet gracefully)
            try:
                transport_decrypted = getattr(decryptor, _decrypt_method_name)(packet)
            except Exception as e:
                _log_state["transport_errors"] += 1
                if _log_state["transport_errors"] <= 3:
                    log.warning("[DAVE] Transport decrypt failed (%d): %s",
                                _log_state["transport_errors"], e)
                raise

            # DAVE decrypt
            try:
                conn = reader_self.voice_client._connection
                dave_session = getattr(conn, 'dave_session', None)

                if dave_session is None:
                    _log_state["no_dave_count"] += 1
                    if _log_state["no_dave_count"] == 1:
                        log.info("[DAVE] No dave_session, passing transport-decrypted audio")
                    return transport_decrypted

                if not getattr(dave_session, 'ready', False):
                    if not _log_state["dave_not_ready"]:
                        _log_state["dave_not_ready"] = True
                        log.warning("[DAVE] Session exists but not ready "
                                    "(status=%s epoch=%s), skipping E2EE decrypt",
                                    getattr(dave_session, 'status', '?'),
                                    getattr(dave_session, 'epoch', '?'))
                    return transport_decrypted
                else:
                    # Reset flag so we re-log if it goes unready again
                    _log_state["dave_not_ready"] = False

                ssrc = packet.ssrc
                user_id = reader_self.voice_client._ssrc_to_id.get(ssrc)
                if user_id is None:
                    return transport_decrypted

                result = dave_session.decrypt(user_id, davey.MediaType.audio, transport_decrypted)
                if result is not None:
                    _log_state["dave_ok_count"] += 1
                    if _log_state["dave_ok_count"] == 1:
                        log.info("[DAVE] First successful E2EE decrypt for ssrc=%s user=%s",
                                 ssrc, user_id)
                    return result

                # decrypt returned None — unexpected
                if not _log_state["dave_none"]:
                    _log_state["dave_none"] = True
                    log.warning("[DAVE] decrypt() returned None for user=%s ssrc=%s "
                                "(epoch=%s)", user_id, ssrc,
                                getattr(dave_session, 'epoch', '?'))
                return transport_decrypted

            except Exception as e:
                if not _log_state["dave_error"]:
                    _log_state["dave_error"] = True
                    log.warning("[DAVE] E2EE decrypt error: %s: %s", type(e).__name__, e)
                return transport_decrypted

        self.decryptor.decrypt_rtp = _decrypt_rtp_with_dave
        log.info("[DAVE] Installed DAVE-aware decrypt_rtp on AudioReader")

    vr_reader.AudioReader.start = _patched_reader_start
    log.info("Applied DAVE E2EE decryption patch to voice_recv")

    # ── Jitter buffer PLC patch ───────────────────────────────────────────

    def _get_next_packet_patched(self, timeout: float) -> Optional["AudioPacket"]:
        """
        Patched version that uses PLC instead of flushing on packet loss.

        Original behavior: When pop() returns None but buffer has items,
        flush entire buffer and return only the first packet (discarding rest).

        Patched behavior: Generate a FakePacket for PLC. The decoder's
        _decode_packet already handles FakePackets with FEC/interpolation.
        Only flush after MAX_CONSECUTIVE_PLC losses to resync.
        """
        # Track consecutive PLC packets (stored on instance)
        if not hasattr(self, "_consecutive_plc"):
            self._consecutive_plc = 0

        packet = self._buffer.pop(timeout=timeout)

        if packet is not None:
            # Got a real packet - reset PLC counter
            self._consecutive_plc = 0
            if not packet:
                # Empty packet marker, make fake for PLC
                packet = self._make_fakepacket()
            return packet

        # No sequential packet ready
        if not self._buffer:
            # Buffer truly empty, nothing to do
            return None

        # Buffer has items but they're not sequential (missing packet)
        # Instead of flushing, generate a PLC packet

        if self._consecutive_plc >= _MAX_CONSECUTIVE_PLC:
            # Too many consecutive losses - network is probably very bad
            # Fall back to original flush behavior to resync
            log.debug(
                "Max PLC reached (%d), flushing buffer to resync",
                self._consecutive_plc,
            )
            self._consecutive_plc = 0
            packets = self._buffer.flush()
            if packets:
                return packets[0]
            return None

        # Generate a fake packet for PLC
        # The decoder will use FEC if next packet available, else interpolate
        self._consecutive_plc += 1

        if self._consecutive_plc == 1:
            # Only log on first loss in a sequence to reduce spam
            gap = self._buffer.gap()
            log.debug("Packet loss detected (gap=%d), using PLC", gap)

        return self._make_fakepacket()

    def _decode_packet_patched(self, packet: "AudioPacket") -> Tuple["AudioPacket", bytes]:
        """
        Patched version that handles corrupted packets gracefully.

        If Opus decoding fails with OpusError (corrupted stream, invalid data),
        fall back to PLC (decode with None) instead of crashing.
        """
        try:
            return original_decode_packet(self, packet)
        except OpusError as e:
            # Corrupted packet - use PLC instead of crashing
            log.debug("OpusError decoding packet (seq=%s): %s, using PLC",
                     getattr(packet, 'sequence', '?'), e)
            # Generate silence/interpolation via PLC
            pcm = self._decoder.decode(None, fec=False)
            return packet, pcm

    # Apply the patches
    opus.PacketDecoder._get_next_packet = _get_next_packet_patched
    opus.PacketDecoder._decode_packet = _decode_packet_patched
    _PATCHED = True
    log.info("Applied voice_recv jitter buffer patch (PLC instead of flush)")


def is_patched() -> bool:
    """Check if the patch has been applied."""
    return _PATCHED
