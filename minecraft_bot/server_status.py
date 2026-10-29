import asyncio
import json
import struct
import time
from dataclasses import dataclass

from .config import MINECRAFT_STATUS_HOST, MINECRAFT_STATUS_PORT


@dataclass(frozen=True)
class ServerStatus:
    online_players: int
    max_players: int
    version: str
    latency_ms: int


def _encode_varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            result.append(byte | 0x80)
        else:
            result.append(byte)
            return bytes(result)


def _encode_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _encode_varint(len(encoded)) + encoded


async def _read_varint(reader: asyncio.StreamReader) -> int:
    value = 0
    for index in range(5):
        byte = (await reader.readexactly(1))[0]
        value |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            if value & 0x80000000:
                value -= 0x100000000
            return value
    raise ValueError("Некорректный VarInt")


async def _read_packet(reader: asyncio.StreamReader) -> bytes:
    packet_length = await _read_varint(reader)
    if packet_length <= 0 or packet_length > 1_048_576:
        raise ValueError("Некорректный размер ответа сервера")
    return await reader.readexactly(packet_length)


async def query_server_status(timeout_seconds: float = 4.0) -> ServerStatus:
    started_at = time.monotonic()
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(MINECRAFT_STATUS_HOST, MINECRAFT_STATUS_PORT),
        timeout=timeout_seconds,
    )
    try:
        handshake = (
            b"\x00"
            + _encode_varint(-1)
            + _encode_string(MINECRAFT_STATUS_HOST)
            + struct.pack(">H", MINECRAFT_STATUS_PORT)
            + b"\x01"
        )
        writer.write(_encode_varint(len(handshake)) + handshake)
        writer.write(b"\x01\x00")
        ping_payload = struct.pack(">q", time.monotonic_ns())
        writer.write(b"\x09\x01" + ping_payload)
        await asyncio.wait_for(writer.drain(), timeout=timeout_seconds)

        response = await asyncio.wait_for(
            _read_packet(reader), timeout=timeout_seconds
        )
        packet_id, offset = _decode_varint(response)
        if packet_id != 0:
            raise ValueError("Сервер вернул неожиданный пакет")

        json_length, length_size = _decode_varint(response, offset)
        json_start = offset + length_size
        if json_length < 0 or json_start + json_length > len(response):
            raise ValueError("Некорректный ответ сервера")
        status_json = json.loads(response[json_start : json_start + json_length])

        players = status_json.get("players") or {}
        version = status_json.get("version") or {}
        return ServerStatus(
            online_players=int(players.get("online", 0)),
            max_players=int(players.get("max", 0)),
            version=str(version.get("name", "неизвестна")),
            latency_ms=round((time.monotonic() - started_at) * 1000),
        )
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def _decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    value = 0
    for index in range(5):
        if offset + index >= len(data):
            raise ValueError("Неполный VarInt")
        byte = data[offset + index]
        value |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            if value & 0x80000000:
                value -= 0x100000000
            return value, index + 1
    raise ValueError("Некорректный VarInt")
