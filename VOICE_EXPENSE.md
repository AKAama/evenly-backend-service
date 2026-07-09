# Voice Expense Drafts

This document defines the target contract for voice-based expense creation.
The current backend still supports uploaded audio drafts at
`POST /expenses/ledgers/{ledger_id}/voice-draft`; the same draft shape should
be reused by the streaming FunASR implementation.

## Product Flow

1. iOS starts a voice expense session for a ledger.
2. iOS streams 16 kHz mono PCM chunks to the backend.
3. The backend forwards audio chunks to FunASR over WebSocket.
4. FunASR partial text is sent back to iOS for live captions.
5. Final text is accumulated by the backend.
6. When the user stops recording, the backend calls the LLM once with the final transcript.
7. The backend validates members, payer, amount, and split totals.
8. iOS shows an editable confirmation page.
9. iOS creates the expense with the existing `POST /expenses/ledgers/{ledger_id}/expenses` API.

Partial transcripts are UI-only. Do not call the LLM for every partial result.

## Audio Contract

- Format: 16-bit signed little-endian PCM
- Sample rate: 16 kHz
- Channels: mono
- Recommended chunk duration: 100-300 ms
- Maximum session duration: 60 seconds

If iOS captures another format, resample before streaming to the backend.

## Target WebSocket Endpoint

```text
GET /expenses/ledgers/{ledger_id}/voice-session
```

Authentication should use the same session cookie as the rest of the API. If a
native client cannot rely on cookies, pass a short-lived bearer token through
the `Authorization` header during the WebSocket handshake.

## Cloud FunASR Configuration

The backend connects to the cloud FunASR WebSocket endpoint. iOS should never
connect to FunASR directly.

Required:

```text
FUNASR_WEBSOCKET_URL=wss://your-funasr-provider.example/ws
```

Optional:

```text
FUNASR_AUTH_HEADER=Authorization
FUNASR_AUTH_TOKEN=Bearer your-token
FUNASR_MODE=2pass
FUNASR_CHUNK_SIZE=5,10,5
FUNASR_CHUNK_INTERVAL=10
FUNASR_ITN=true
```

If the provider uses a custom header such as `X-API-Key`, set
`FUNASR_AUTH_HEADER=X-API-Key` and put the secret in `FUNASR_AUTH_TOKEN`.

## Client Events

Start session metadata:

```json
{
  "type": "start",
  "audio": {
    "format": "pcm_s16le",
    "sample_rate": 16000,
    "channels": 1
  }
}
```

Audio chunks are sent as binary WebSocket messages.

Stop recording:

```json
{ "type": "stop" }
```

Cancel session:

```json
{ "type": "cancel" }
```

## Server Events

Session accepted:

```json
{ "type": "ready" }
```

Live caption:

```json
{ "type": "partial_transcript", "text": "中午和张三" }
```

Stable transcript segment:

```json
{ "type": "final_transcript", "text": "中午和张三吃饭花了 268，我付的，AA" }
```

Draft:

```json
{
  "type": "draft",
  "data": {
    "transcript": "中午和张三吃饭花了 268，我付的，AA",
    "title": "中午吃饭",
    "amount": "268.00",
    "total_amount": "268.00",
    "currency": "CNY",
    "category": "餐饮",
    "note": "中午吃饭",
    "expense_date": "2026-07-09",
    "payer_user_id": "user-uuid",
    "participant_member_ids": ["member-uuid-1", "member-uuid-2"],
    "split_type": "equal",
    "splits": [
      {
        "member_id": "member-uuid-1",
        "user_id": "user-uuid",
        "amount": "134.00"
      },
      {
        "member_id": "member-uuid-2",
        "user_id": null,
        "amount": "134.00"
      }
    ],
    "confidence": 0.86,
    "missing_fields": [],
    "confirmation_text": "已生成中午吃饭，金额268.00元，我付款，参与人是我、张三。请确认。"
  }
}
```

Error:

```json
{ "type": "error", "message": "未能识别有效金额" }
```

## Draft Rules

- The LLM may infer fields, but the backend is authoritative.
- `payer_user_id` must be a registered active member in the ledger.
- `participant_member_ids` must be active ledger members.
- The payer's ledger member must be included in participants.
- `splits` must sum to `total_amount`.
- If exact splits are absent or invalid, the backend falls back to equal split.
- The backend returns a draft only; it does not create the expense automatically.

## FunASR Adapter Boundary

The streaming implementation should keep FunASR-specific message formats inside
an adapter service. Router code should only deal with normalized events:

```python
{"type": "partial", "text": "..."}
{"type": "final", "text": "..."}
{"type": "error", "message": "..."}
```

This keeps the iOS protocol stable if the ASR provider changes later.
