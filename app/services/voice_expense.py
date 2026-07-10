import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from time import perf_counter

import requests

from app.config import settings

logger = logging.getLogger(__name__)


class VoiceExpenseError(RuntimeError):
    pass


def _openai_headers() -> dict[str, str]:
    if not settings.openai_api_key:
        raise VoiceExpenseError("语音记账尚未配置 OPENAI_API_KEY / DASHSCOPE_API_KEY")
    return {
        "Authorization": f"Bearer {settings.openai_api_key}",
    }


def transcribe_audio(audio: bytes, filename: str, content_type: str) -> str:
    response = requests.post(
        "https://api.openai.com/v1/audio/transcriptions",
        headers=_openai_headers(),
        data={"model": settings.openai_transcription_model, "language": "zh"},
        files={"file": (filename, audio, content_type)},
        timeout=60,
    )
    if not response.ok:
        raise VoiceExpenseError("语音转写失败，请稍后重试")
    transcript = str(response.json().get("text", "")).strip()
    if not transcript:
        raise VoiceExpenseError("没有识别到有效语音")
    return transcript


def _expense_draft_system_prompt() -> str:
    return (
        "你把中文语音记账内容转换为账单草稿。输出必须是合法 JSON 对象，不要输出 Markdown。"
        "账本成员列表 members 是唯一可信来源，只能返回给定的成员 ID 和用户 ID，禁止编造。"
        "优先把语音里的人名、昵称、同音称呼匹配到 members.name。"
        "‘我’表示 current_user_id 对应的成员。"
        "付款人字段 payer_user_id 必须是 members 中 registered=true 的 user_id；"
        "临时成员 registered=false（user_id 为 null）可以作为参与人，但不能作为付款人。"
        "参与人字段 participant_member_ids 是参与本次消费的 members 的 member_id 字符串数组；"
        "如果没说参与人，只填付款人自己；如果说了“我和某人”，包含我和该成员的 member_id。"
        "不需要计算分摊金额，客户端会自行平分。"
        "返回的 JSON 字段：title（账目名称字符串）、amount（金额浮点数，小数点后两位）、"
        "payer_user_id（字符串，付款人的 user_id）、"
        "participant_member_ids（字符串数组，参与人的 member_id）。"
        "可选字段：category（分类字符串）、note（备注字符串）、currency（货币代码，默认 CNY）、"
        "confidence（0-1 置信度）、missing_fields（未能识别的字段名数组）。"
    )


def _expense_draft_user_payload(
        transcript: str,
        members: list[dict[str, str | bool | None]],
        current_user_id: str,
) -> str:
    return json.dumps(
        {
            "transcript": transcript,
            "current_user_id": current_user_id,
            "members": members,
        },
        ensure_ascii=False,
    )


def _model_supports_thinking_disable(model: str) -> bool:
    """DashScope qwen3 / deepseek-r1 / qwq 等 reasoning 模型需要在 body 里显式关闭思考。

    注意：关闭后才能走 JSON mode，否则 thinking 块会污染输出导致 json.loads 失败。
    """
    name = model.lower()
    return "qwen3" in name or "deepseek-r1" in name or "qwq" in name or "-thinking" in name


def _parse_request_body(
        transcript: str,
        members: list[dict[str, str | bool | None]],
        current_user_id: str,
) -> dict:
    system_prompt = _expense_draft_system_prompt()
    user_payload = _expense_draft_user_payload(transcript, members, current_user_id)
    model = settings.openai_text_model

    # 统一使用 Chat Completions 格式：
    #   - 兼容 OpenAI 官方 / DashScope / 大部分 OpenAI-compatible 网关
    #   - 原生支持 response_format: json_object，输出稳定是纯 JSON
    #   - enable_thinking 作为顶层 extra body 字段被 DashScope 识别
    body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    if _model_supports_thinking_disable(model):
        # DashScope 兼容模式：顶层传 enable_thinking=false 关闭推理。
        body["enable_thinking"] = False
    return body


def _extract_model_text(data: dict) -> str:
    """Chat Completions 响应格式：choices[0].message.content"""
    try:
        return str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        pass
    # 兼容 Responses 风格返回（万一误配 /responses URL 也能解析）
    output_text = data.get("output_text")
    if output_text:
        return str(output_text)
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for content_item in content:
                if isinstance(content_item, dict) and content_item.get("type") == "output_text":
                    text = str(content_item.get("text") or "").strip()
                    if text:
                        return text
    raise VoiceExpenseError("账单内容解析失败")


def parse_expense_draft(
        transcript: str,
        members: list[dict[str, str | bool | None]],
        current_user_id: str,
) -> dict:
    body = _parse_request_body(transcript, members, current_user_id)
    t0 = perf_counter()
    response = requests.post(
        settings.openai_url,
        headers={**_openai_headers(), "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    elapsed_ms = (perf_counter() - t0) * 1000
    logger.info(
        "LLM 调用完成 model=%s url=%s status=%d elapsed_ms=%.0f bytes=%d",
        settings.openai_text_model,
        settings.openai_url,
        response.status_code,
        elapsed_ms,
        len(response.content),
    )
    if not response.ok:
        logger.warning(
            "LLM 调用失败 status=%d body=%s",
            response.status_code,
            response.text[:1000],
        )
        raise VoiceExpenseError("账单内容解析失败，请稍后重试")
    try:
        content = _extract_model_text(response.json())
        logger.info("LLM 返回内容：%s", content[:500])
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise VoiceExpenseError("账单内容解析失败")
        return parsed
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("LLM 响应解析失败 body=%s error=%s", response.text[:1000], exc)
        raise VoiceExpenseError("账单内容解析失败") from exc


def _normalize_money(value) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (TypeError, InvalidOperation) as exc:
        raise VoiceExpenseError("未能识别有效金额") from exc
    if amount <= 0:
        raise VoiceExpenseError("未能识别有效金额")
    return amount


def create_voice_expense_draft(
        audio: bytes,
        filename: str,
        content_type: str,
        members: list[dict[str, str | bool | None]],
        current_user_id: str,
) -> dict:
    transcript = transcribe_audio(audio, filename, content_type)
    return create_voice_expense_draft_from_transcript(
        transcript=transcript,
        members=members,
        current_user_id=current_user_id,
    )


def create_voice_expense_draft_from_transcript(
        transcript: str,
        members: list[dict[str, str | bool | None]],
        current_user_id: str,
) -> dict:
    transcript = transcript.strip()
    if not transcript:
        raise VoiceExpenseError("没有识别到有效语音")
    parsed = parse_expense_draft(transcript, members, current_user_id)
    members_by_id = {str(m["member_id"]): m for m in members}
    registered_user_ids = {
        str(m["user_id"]) for m in members if m.get("registered") and m.get("user_id")
    }

    # LLM 按提示词返回 payer_user_id；没返回或返回了未注册用户时，回退到 current_user。
    payer_user_id = str(parsed.get("payer_user_id", "")).strip()
    if payer_user_id not in registered_user_ids:
        payer_user_id = current_user_id
    if payer_user_id not in registered_user_ids:
        raise VoiceExpenseError("未能识别有效付款人")
    payer_member = next(
        m for m in members if str(m.get("user_id")) == payer_user_id
    )
    payer_member_id = str(payer_member["member_id"])

    participant_ids = list(dict.fromkeys(
        member_id
        for member_id in parsed.get("participant_member_ids", [])
        if member_id in members_by_id
    ))
    if payer_member_id not in participant_ids:
        participant_ids.append(payer_member_id)

    amount = _normalize_money(parsed.get("amount"))

    title = str(parsed.get("title", "")).strip() or "语音账单"
    category = parsed.get("category")
    note = str(parsed.get("note") or "").strip() or None
    currency = str(parsed.get("currency") or "CNY").strip().upper() or "CNY"
    missing_fields = [
        str(item) for item in parsed.get("missing_fields", []) if str(item).strip()
    ]
    confidence = parsed.get("confidence")
    if not isinstance(confidence, (int, float)):
        confidence = None

    participant_names = [str(members_by_id[mid]["name"]) for mid in participant_ids]
    payer_name = str(payer_member["name"])
    return {
        "transcript": transcript,
        "title": title,
        "amount": amount,
        "total_amount": amount,
        "currency": currency,
        "category": str(category).strip() if category else None,
        "note": note,
        "expense_date": date.today(),
        "payer_user_id": payer_user_id,
        "participant_member_ids": participant_ids,
        "splits": [],
        "confidence": float(confidence) if confidence is not None else None,
        "missing_fields": missing_fields,
        "confirmation_text": (
            f"已生成{title}，金额{amount}元，{payer_name}付款，"
            f"参与人是{'、'.join(participant_names)}。请确认。"
        ),
    }
