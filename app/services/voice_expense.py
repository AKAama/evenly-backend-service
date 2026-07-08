import json
from decimal import Decimal, InvalidOperation

import requests

from app.config import settings


class VoiceExpenseError(RuntimeError):
    pass


def _openai_headers() -> dict[str, str]:
    if not settings.openai_api_key:
        raise VoiceExpenseError("语音记账尚未配置 OPENAI_API_KEY")
    return {"Authorization": f"Bearer {settings.openai_api_key}"}


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


def parse_expense_draft(
    transcript: str,
    members: list[dict[str, str | bool | None]],
    current_user_id: str,
) -> dict:
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={**_openai_headers(), "Content-Type": "application/json"},
        json={
            "model": settings.openai_text_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你把中文语音记账内容转换为账单草稿。只能使用给定成员 ID。"
                        "‘我’表示 current_user_id。付款人必须是 registered=true 的成员。"
                        "没有明确参与人时只选择付款人。标题保持简短。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "transcript": transcript,
                            "current_user_id": current_user_id,
                            "members": members,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "expense_draft",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "amount": {"type": "number"},
                            "payer_user_id": {"type": "string"},
                            "participant_member_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": [
                            "title",
                            "amount",
                            "payer_user_id",
                            "participant_member_ids",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
        },
        timeout=45,
    )
    if not response.ok:
        raise VoiceExpenseError("账单内容解析失败，请稍后重试")
    try:
        content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise VoiceExpenseError("账单内容解析失败") from exc


def create_voice_expense_draft(
    audio: bytes,
    filename: str,
    content_type: str,
    members: list[dict[str, str | bool | None]],
    current_user_id: str,
) -> dict:
    transcript = transcribe_audio(audio, filename, content_type)
    parsed = parse_expense_draft(transcript, members, current_user_id)

    members_by_id = {str(member["member_id"]): member for member in members}
    registered_user_ids = {
        str(member["user_id"])
        for member in members
        if member["registered"] and member["user_id"]
    }
    payer_user_id = str(parsed.get("payer_user_id", ""))
    if payer_user_id not in registered_user_ids:
        payer_user_id = current_user_id
    if payer_user_id not in registered_user_ids:
        raise VoiceExpenseError("未能识别有效付款人")

    participant_ids = list(dict.fromkeys(
        member_id
        for member_id in parsed.get("participant_member_ids", [])
        if member_id in members_by_id
    ))
    payer_member = next(
        member for member in members if str(member.get("user_id")) == payer_user_id
    )
    payer_member_id = str(payer_member["member_id"])
    if payer_member_id not in participant_ids:
        participant_ids.append(payer_member_id)

    try:
        amount = Decimal(str(parsed["amount"])).quantize(Decimal("0.01"))
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise VoiceExpenseError("未能识别有效金额") from exc
    if amount <= 0:
        raise VoiceExpenseError("未能识别有效金额")

    title = str(parsed.get("title", "")).strip() or "语音账单"
    participant_names = [members_by_id[item]["name"] for item in participant_ids]
    payer_name = payer_member["name"]
    return {
        "transcript": transcript,
        "title": title,
        "amount": amount,
        "payer_user_id": payer_user_id,
        "participant_member_ids": participant_ids,
        "confirmation_text": (
            f"已生成{title}，金额{amount}元，{payer_name}付款，"
            f"参与人是{'、'.join(str(name) for name in participant_names)}。请确认。"
        ),
    }
