# -*- coding: utf-8 -*-
"""
Monkey-patch for discord-ext-voice-recv to improve packet loss handling.

The library's default behavior flushes the entire jitter buffer when a
sequential packet isn't ready, discarding buffered audio. This patch
uses Opus PLC (packet loss concealment) instead, generating interpolated
audio for missing packets.

Additionally patches _decode_packet to handle corrupted packets gracefully
instead of crashing with OpusError.

Apply early in startup before any voice_recv imports:
    from core.voice_recv_patch import apply_patch
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
    Apply the jitter buffer patch. Safe to call multiple times.
    Must be called before any VoiceRecvClient connections are made.
    """
    global _PATCHED
    if _PATCHED:
        return

    from discord.ext.voice_recv import opus
    from discord.opus import OpusError

    original_get_next_packet = opus.PacketDecoder._get_next_packet
    original_decode_packet = opus.PacketDecoder._decode_packet

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
