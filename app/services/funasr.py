import asyncio
import json
from collections.abc import AsyncIterator

from app.config import settings


class FunASRError(RuntimeError):
    pass


def _chunk_size() -> list[int]:
    try:
        values = [int(item.strip()) for item in settings.funasr_chunk_size.split(",")]
    except ValueError as exc:
        raise FunASRError("FUNASR_CHUNK_SIZE 配置无效") from exc
    if len(values) != 3 or any(item <= 0 for item in values):
        raise FunASRError("FUNASR_CHUNK_SIZE 需要形如 5,10,5")
    return values


def _headers() -> dict[str, str]:
    if settings.funasr_auth_header and settings.funasr_auth_token:
        return {settings.funasr_auth_header: settings.funasr_auth_token}
    return {}


async def stream_funasr(
    audio_chunks: AsyncIterator[bytes],
    *,
    wav_name: str,
) -> AsyncIterator[dict[str, str | bool]]:
    if not settings.funasr_websocket_url:
        raise FunASRError("语音流式识别尚未配置 FUNASR_WEBSOCKET_URL")

    try:
        import websockets
    except ImportError as exc:
        raise FunASRError("语音流式识别缺少 websockets 依赖，请先安装后端依赖") from exc

    connect_kwargs = {
        "ping_interval": 20,
        "ping_timeout": 20,
    }
    headers = _headers()
    if headers:
        connect_kwargs["additional_headers"] = headers
    try:
        funasr_context = websockets.connect(settings.funasr_websocket_url, **connect_kwargs)
    except TypeError:
        if headers:
            connect_kwargs.pop("additional_headers", None)
            connect_kwargs["extra_headers"] = headers
        funasr_context = websockets.connect(settings.funasr_websocket_url, **connect_kwargs)

    async with funasr_context as funasr_ws:
        await funasr_ws.send(json.dumps({
            "mode": settings.funasr_mode,
            "chunk_size": _chunk_size(),
            "chunk_interval": settings.funasr_chunk_interval,
            "wav_name": wav_name,
            "is_speaking": True,
            "itn": settings.funasr_itn,
        }, ensure_ascii=False))

        sender = asyncio.create_task(_send_audio(funasr_ws, audio_chunks))
        try:
            async for raw_message in funasr_ws:
                event = _normalize_message(raw_message)
                if event:
                    yield event
                if event.get("is_final"):
                    break
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)


async def _send_audio(funasr_ws, audio_chunks: AsyncIterator[bytes]) -> None:
    async for chunk in audio_chunks:
        if chunk:
            await funasr_ws.send(chunk)
    await funasr_ws.send(json.dumps({"is_speaking": False}, ensure_ascii=False))


def _normalize_message(raw_message) -> dict[str, str | bool]:
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8", errors="ignore")
    try:
        data = json.loads(raw_message)
    except (TypeError, json.JSONDecodeError):
        return {}

    text = str(data.get("text") or "").strip()
    if not text:
        return {}

    mode = str(data.get("mode") or "")
    is_final = bool(data.get("is_final")) or mode.endswith("offline")
    return {
        "type": "final" if is_final else "partial",
        "text": text,
        "is_final": is_final,
    }
