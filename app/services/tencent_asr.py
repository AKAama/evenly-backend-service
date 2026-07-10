"""Tencent Cloud Real-Time ASR (实时语音识别) WebSocket client.

Protocol reference:
  https://cloud.tencent.com/document/product/1093/48982

Connection flow:
  1. Build signed URL (HMAC-SHA1 over sorted query params + host + path)
  2. Open WebSocket; server sends {"code":0,"message":"success",...} on handshake
  3. Client streams raw PCM16 mono @16kHz as binary frames
  4. Server sends JSON text frames with result.slice_type:
        0 = utterance start, 1 = interim (partial), 2 = final (stable)
  5. When audio is done, client sends one text frame {"type":"end"}
  6. Server flushes remaining results, then sends {"final":1} and closes

Yields dicts compatible with the FunASR client:
  {"type": "partial", "text": ..., "is_final": False}
  {"type": "final",   "text": ..., "is_final": True}
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

import websockets

from app.config import settings

logger = logging.getLogger(__name__)


class TencentASRError(RuntimeError):
    pass


def _sign_url(
    *,
    endpoint: str,
    appid: str,
    secret_id: str,
    secret_key: str,
    engine_model_type: str,
    voice_id: str,
    needvad: int,
    vad_silence_time: int,
    filter_modal: int,
    filter_punc: int,
    convert_num_mode: int,
    hotword_list: str | None,
) -> str:
    """Build a signed wss:// URL per Tencent ASR auth spec.

    Plaintext to sign:
        <host><path>?<sorted_query_string>

    Tencent ASR uses the WebSocket request URL without the ``wss://``
    protocol prefix as the signing plaintext.
    """
    parsed = urlparse(endpoint)
    host = parsed.netloc
    path = parsed.path.rstrip("/") + f"/{appid}"

    params: dict[str, Any] = {
        "secretid": secret_id,
        "timestamp": int(time.time()),
        "expired": int(time.time()) + 90,
        "nonce": uuid.uuid4().int % 900000 + 100000,
        "engine_model_type": engine_model_type,
        "voice_id": voice_id,
        "voice_format": 1,  # PCM
        "sample_rate": 16000,
        "needvad": needvad,
        "vad_silence_time": vad_silence_time,
        "filter_modal": filter_modal,
        "filter_punc": filter_punc,
        "convert_num_mode": convert_num_mode,
    }
    if hotword_list:
        params["hotword_list"] = hotword_list

    sorted_params = sorted(params.items())
    # Tencent verifies the signature against the original parameter values.
    # In particular, non-ASCII hotwords must remain unescaped in the signing
    # plaintext, while the actual WebSocket URL still needs percent-encoding.
    signing_query = "&".join(f"{key}={value}" for key, value in sorted_params)
    request_query = urllib.parse.urlencode(sorted_params)
    plaintext = f"{host}{path}?{signing_query}"
    sig = hmac.new(secret_key.encode("utf-8"), plaintext.encode("utf-8"), hashlib.sha1).digest()
    signature = urllib.parse.quote(base64.b64encode(sig).decode("utf-8"), safe="")

    return f"wss://{host}{path}?{request_query}&signature={signature}"


def _build_hotword_list(words: list[str], weight: int) -> str | None:
    """Serialize hotwords into "word1|weight,word2|weight" format.

    - weight=1..10  : 普通热词（权重越高越倾向）
    - weight=11     : 超级热词
    - weight=100    : 中文同音同调替换；非中文专名自动使用超级热词 11
    每个 hotword 不含空格或标点，<=10 字，最多 128 个。
    """
    if not words:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    w = max(1, min(int(weight), 100))
    for raw in words:
        word = str(raw or "").strip()
        if (
            not word
            or len(word) > 10
            or word.isdecimal()
            or not all(char.isalnum() for char in word)
            or word in seen
        ):
            continue
        seen.add(word)
        is_chinese_name = all("\u4e00" <= char <= "\u9fff" for char in word)
        effective_weight = 100 if w == 100 and is_chinese_name else (11 if w == 100 else w)
        cleaned.append(f"{word}|{effective_weight}")
        if len(cleaned) >= 128:
            break
    return ",".join(cleaned) if cleaned else None


async def stream_tencent_asr(
    audio_chunks: AsyncIterator[bytes],
    *,
    wav_name: str,
    session_id: str | None = None,
    hotwords: list[str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    sid = session_id or "-"
    if not settings.asr_appid or not settings.asr_secret_id or not settings.asr_secret_key:
        raise TencentASRError("腾讯语音识别未配置 ASR_APPID / ASR_SECRET_ID / ASR_SECRET_KEY")

    voice_id = uuid.uuid4().hex
    hotword_list = _build_hotword_list(hotwords or [], settings.asr_hotword_weight)
    url = _sign_url(
        endpoint=settings.asr_endpoint,
        appid=settings.asr_appid,
        secret_id=settings.asr_secret_id,
        secret_key=settings.asr_secret_key,
        engine_model_type=settings.asr_engine_model_type,
        voice_id=voice_id,
        needvad=settings.asr_needvad,
        vad_silence_time=settings.asr_vad_silence_time,
        filter_modal=settings.asr_filter_modal,
        filter_punc=settings.asr_filter_punc,
        convert_num_mode=settings.asr_convert_num_mode,
        hotword_list=hotword_list,
    )

    t0 = perf_counter()
    logger.info(
        "腾讯 ASR 开始连接 session_id=%s engine=%s needvad=%d vad_silence=%dms hotwords=%d",
        sid, settings.asr_engine_model_type, settings.asr_needvad,
        settings.asr_vad_silence_time, len(hotword_list.split(",")) if hotword_list else 0,
    )

    async with websockets.connect(
        url,
        ping_interval=20,
        ping_timeout=10,
        close_timeout=2,
        open_timeout=settings.asr_connect_timeout_seconds,
        max_size=2**20,
    ) as ws:
        connect_done_at = perf_counter()
        logger.info("腾讯 ASR 已连接 session_id=%s connect_ms=%.0f", sid, (connect_done_at - t0) * 1000)

        # 1) 等待握手成功消息 {"code":0,"message":"success","voice_id":...}
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=settings.asr_connect_timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise TencentASRError("腾讯 ASR 握手超时") from exc
        handshake = _parse_json(raw)
        code = handshake.get("code", -1) if isinstance(handshake, dict) else -1
        if code != 0:
            msg = handshake.get("message", "未知错误") if isinstance(handshake, dict) else "响应异常"
            raise TencentASRError(f"腾讯 ASR 握手失败：code={code} message={msg}")
        handshake_done_at = perf_counter()
        logger.info(
            "腾讯 ASR 握手成功 session_id=%s handshake_ms=%.0f voice_id=%s",
            sid, (handshake_done_at - connect_done_at) * 1000,
            handshake.get("voice_id", voice_id),
        )

        # 2) 启动音频发送任务
        sender = asyncio.create_task(_send_audio(ws, audio_chunks, session_id=sid))
        receiver = asyncio.create_task(ws.recv())
        sender_done = False
        stream_final_received = False
        last_final_at: float | None = None

        try:
            while True:
                wait_for = {receiver}
                timeout: float | None = None
                if not sender.done():
                    wait_for.add(sender)
                else:
                    if not sender_done:
                        await sender
                        sender_done = True
                    # 发完 {"type":"end"} 后，最多等 asr_final_timeout_seconds 强制收尾
                    timeout = settings.asr_final_timeout_seconds
                    if last_final_at is not None:
                        grace = 2.0
                        since = perf_counter() - last_final_at
                        timeout = min(timeout, max(0.0, grace - since))

                done, _ = await asyncio.wait(
                    wait_for, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    logger.warning(
                        "腾讯 ASR 等待最终结果超时 session_id=%s timeout_s=%.1f",
                        sid, settings.asr_final_timeout_seconds,
                    )
                    break

                if sender in done and not sender_done:
                    await sender
                    sender_done = True
                    if receiver not in done:
                        continue

                if receiver not in done:
                    continue

                raw_msg = receiver.result()
                data = _parse_json(raw_msg)
                if not isinstance(data, dict):
                    receiver = asyncio.create_task(ws.recv())
                    continue

                if data.get("code", 0) != 0 and not data.get("final"):
                    logger.warning(
                        "腾讯 ASR 错误 code=%s message=%s session_id=%s",
                        data.get("code"), data.get("message"), sid,
                    )
                    # 不可恢复错误（如 4008 音频发送超时、鉴权失败）
                    code_val = int(data.get("code") or 0)
                    if code_val in (4002, 4003, 4006, 4008):
                        raise TencentASRError(f"腾讯 ASR 错误：{data.get('message')}")
                    receiver = asyncio.create_task(ws.recv())
                    continue

                # final=1 表示整段音频处理完毕，正常结束
                if data.get("final") == 1:
                    logger.info(
                        "腾讯 ASR 收到 final=1 结束信号 session_id=%s total_ms=%.0f",
                        sid, (perf_counter() - t0) * 1000,
                    )
                    break

                result = data.get("result")
                if isinstance(result, dict):
                    slice_type = int(result.get("slice_type", -1))
                    text = str(result.get("voice_text_str") or "").strip()
                    if text:
                        is_final = slice_type == 2
                        if is_final:
                            last_final_at = perf_counter()
                            logger.info(
                                "腾讯 ASR 收到最终结果 session_id=%s slice_type=%d chars=%d",
                                sid, slice_type, len(text),
                            )
                        else:
                            logger.debug(
                                "腾讯 ASR 收到中间结果 session_id=%s slice_type=%d chars=%d",
                                sid, slice_type, len(text),
                            )
                        yield {
                            "type": "final" if is_final else "partial",
                            "text": text,
                            "is_final": is_final,
                        }
                receiver = asyncio.create_task(ws.recv())
        finally:
            receiver.cancel()
            sender.cancel()
            await asyncio.gather(receiver, sender, return_exceptions=True)


async def _send_audio(ws, audio_chunks: AsyncIterator[bytes], *, session_id: str) -> None:
    chunk_count = 0
    byte_count = 0
    async for chunk in audio_chunks:
        if not chunk:
            continue
        chunk_count += 1
        byte_count += len(chunk)
        await ws.send(chunk)
    logger.info(
        "腾讯 ASR 音频发送完成，发送 end 信号 session_id=%s chunks=%d bytes=%d",
        session_id, chunk_count, byte_count,
    )
    await ws.send(json.dumps({"type": "end"}, ensure_ascii=False))


def _parse_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    if not isinstance(raw, str):
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
