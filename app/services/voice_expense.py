import json
from datetime import date
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
                            "currency": {"type": "string"},
                            "category": {"type": ["string", "null"]},
                            "note": {"type": ["string", "null"]},
                            "payer_user_id": {"type": "string"},
                            "participant_member_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "split_type": {
                                "type": "string",
                                "enum": ["equal", "exact", "unknown"],
                            },
                            "splits": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "member_id": {"type": "string"},
                                        "amount": {"type": "number"},
                                    },
                                    "required": ["member_id", "amount"],
                                    "additionalProperties": False,
                                },
                            },
                            "confidence": {"type": "number"},
                            "missing_fields": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": [
                            "title",
                            "amount",
                            "currency",
                            "category",
                            "note",
                            "payer_user_id",
                            "participant_member_ids",
                            "split_type",
                            "splits",
                            "confidence",
                            "missing_fields",
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


def _normalize_money(value) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (TypeError, InvalidOperation) as exc:
        raise VoiceExpenseError("未能识别有效金额") from exc
    if amount <= 0:
        raise VoiceExpenseError("未能识别有效金额")
    return amount


def _build_equal_splits(amount: Decimal, participant_ids: list[str]) -> dict[str, Decimal]:
    total_cents = int((amount * 100).to_integral_value())
    base_cents = total_cents // len(participant_ids)
    remainder = total_cents - base_cents * len(participant_ids)
    splits = {}
    for member_id in participant_ids:
        extra_cent = 1 if remainder > 0 else 0
        splits[member_id] = Decimal(base_cents + extra_cent) / Decimal(100)
        remainder -= extra_cent
    return splits


def _resolve_splits(
    parsed: dict,
    amount: Decimal,
    participant_ids: list[str],
) -> tuple[str, dict[str, Decimal]]:
    parsed_split_type = str(parsed.get("split_type") or "equal")
    parsed_splits = parsed.get("splits") or []
    exact_splits = {}

    for item in parsed_splits:
        if not isinstance(item, dict):
            continue
        member_id = str(item.get("member_id", ""))
        if member_id not in participant_ids:
            continue
        exact_splits[member_id] = _normalize_money(item.get("amount"))

    if len(exact_splits) == len(participant_ids):
        split_total = sum(exact_splits.values(), Decimal("0.00")).quantize(Decimal("0.01"))
        if split_total == amount:
            if parsed_split_type == "equal":
                return "equal", exact_splits
            return "exact", exact_splits

    return "equal", _build_equal_splits(amount, participant_ids)


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

    amount = _normalize_money(parsed.get("amount"))
    split_type, split_amounts = _resolve_splits(parsed, amount, participant_ids)

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

    participant_names = [members_by_id[item]["name"] for item in participant_ids]
    payer_name = payer_member["name"]
    splits = [
        {
            "member_id": member_id,
            "user_id": members_by_id[member_id]["user_id"],
            "amount": split_amounts[member_id].quantize(Decimal("0.01")),
        }
        for member_id in participant_ids
    ]
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
        "split_type": split_type,
        "splits": splits,
        "confidence": float(confidence) if confidence is not None else None,
        "missing_fields": missing_fields,
        "confirmation_text": (
            f"已生成{title}，金额{amount}元，{payer_name}付款，"
            f"参与人是{'、'.join(str(name) for name in participant_names)}。请确认。"
        ),
    }
