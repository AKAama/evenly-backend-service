import asyncio
import json
import logging
from time import perf_counter
from uuid import UUID, uuid4
from decimal import Decimal
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import joinedload, selectinload, Session
from typing import List

from app.config import settings
from app.database import SessionLocal, get_db
from app.models import User, Ledger, LedgerMember, Expense, ExpenseSplit, ExpenseConfirmation, ExpenseStatus
from app.schemas.expense import (
    ExpenseCreate,
    ExpenseUpdate,
    ExpenseResponse,
    ExpenseWithDetails,
    ExpenseSplitCreate,
    CompoundExpenseCreate,
    CompoundExpenseResponse,
    ConfirmExpenseRequest,
    VoiceExpenseDraft,
    expense_to_with_details,
)
from app.services.auth import decode_token, get_user_by_id
from app.services.tencent_asr import TencentASRError, stream_tencent_asr
from app.services.voice_expense import (
    VoiceExpenseError,
    create_voice_expense_draft,
    create_voice_expense_draft_from_transcript,
)
from app.services.push import PushEvent, build_payload, send_push_safely
from app.services.rate_limit import allow_request, enforce_rate_limit
from app.utils.deps import get_current_user, get_ledger_or_404, require_ledger_member

router = APIRouter(prefix="/expenses", tags=["expenses"])
logger = logging.getLogger(__name__)

CENT = Decimal("0.01")


def normalize_money(value: Decimal) -> Decimal:
    return value.quantize(CENT)


def confirmation_exempt_user_ids(*, created_by, payer_id) -> set:
    """Creator and payer never need to confirm an expense."""
    exempt = set()
    if created_by is not None:
        exempt.add(created_by)
    if payer_id is not None:
        exempt.add(payer_id)
    return exempt


def required_confirmation_user_ids(split_user_ids: set, *, created_by, payer_id) -> set:
    return set(split_user_ids) - confirmation_exempt_user_ids(
        created_by=created_by,
        payer_id=payer_id,
    )


def get_active_voice_members(db: Session, ledger_id: UUID) -> list[dict[str, str | bool | None]]:
    rows = (
        db.query(LedgerMember)
        .options(joinedload(LedgerMember.user))
        .filter(
            LedgerMember.ledger_id == ledger_id,
            LedgerMember.status == "active",
        )
        .all()
    )
    return [
        {
            "member_id": str(member.id),
            "user_id": str(member.user_id) if member.user_id else None,
            "name": member.display_name,
            "registered": member.user_id is not None,
        }
        for member in rows
    ]


def _resolve_expense_splits(
    *,
    ledger_id: UUID,
    payload: ExpenseCreate | ExpenseUpdate,
    db: Session,
):
    """Validate payer/splits for create and update. Returns (resolved_splits, payer_member)."""
    if payload.total_amount <= 0:
        raise HTTPException(status_code=400, detail="Expense amount must be greater than zero")

    if not payload.splits:
        raise HTTPException(status_code=400, detail="At least one split is required")

    members = db.query(LedgerMember).filter(
        LedgerMember.ledger_id == ledger_id,
        LedgerMember.status == "active",
    ).all()
    members_by_id = {member.id: member for member in members}
    members_by_user_id = {
        member.user_id: member for member in members if member.user_id is not None
    }

    resolved_splits = []
    for split in payload.splits:
        member = members_by_id.get(split.member_id) if split.member_id else None
        if member is None and split.user_id:
            member = members_by_user_id.get(split.user_id)
        if member is None:
            raise HTTPException(status_code=400, detail="All split members must belong to the ledger")
        if split.user_id and member.user_id != split.user_id:
            raise HTTPException(status_code=400, detail="Split user and member do not match")
        resolved_splits.append((split, member))

    split_member_ids = [member.id for _, member in resolved_splits]
    if len(split_member_ids) != len(set(split_member_ids)):
        raise HTTPException(status_code=400, detail="Duplicate members in splits are not allowed")

    if any(split.amount <= 0 for split in payload.splits):
        raise HTTPException(status_code=400, detail="Split amounts must be greater than zero")

    payer_member = members_by_user_id.get(payload.payer_id)
    if payer_member is None or payer_member.is_temporary:
        raise HTTPException(status_code=400, detail="Payer must be a registered ledger member")

    split_total = normalize_money(sum(s.amount for s in payload.splits))
    expense_total = normalize_money(payload.total_amount)
    if split_total != expense_total:
        raise HTTPException(
            status_code=400,
            detail=f"Split total ({split_total}) must equal expense amount ({expense_total})",
        )

    payer_in_splits = any(member.id == payer_member.id for _, member in resolved_splits)
    if not payer_in_splits:
        raise HTTPException(status_code=400, detail="Payer must be included in splits")

    return resolved_splits, payer_member


def get_websocket_user(websocket: WebSocket, db: Session) -> User | None:
    authorization = websocket.headers.get("authorization", "")
    token = None
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    token = token or websocket.cookies.get(settings.auth_cookie_name)
    if not token:
        return None

    token_data = decode_token(token)
    if token_data is None or token_data.user_id is None:
        return None
    return get_user_by_id(db, token_data.user_id)


@router.websocket("/ledgers/{ledger_id}/voice-session")
async def create_voice_session(websocket: WebSocket, ledger_id: UUID):
    session_id = uuid4().hex[:8]
    started_at = perf_counter()
    audio_stats = {"chunks": 0, "bytes": 0, "first_chunk_at": None, "stop_at": None}
    client = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "unknown"

    def _elapsed_ms() -> float:
        return (perf_counter() - started_at) * 1000

    logger.info(
        "语音会话已连接 session_id=%s ledger_id=%s client=%s",
        session_id,
        ledger_id,
        client,
    )
    await websocket.accept()
    db = SessionLocal()
    receiver = None
    try:
        current_user = get_websocket_user(websocket, db)
        if current_user is None:
            logger.warning(
                "语音会话鉴权失败 session_id=%s ledger_id=%s reason=未登录或 token 无效",
                session_id,
                ledger_id,
            )
            await websocket.send_json({"type": "error", "message": "未登录或登录已过期"})
            await websocket.close(code=1008)
            return

        if not allow_request(f"voice:user:{current_user.id}", limit=20, window_seconds=60):
            await websocket.send_json({"type": "error", "message": "语音记账请求过于频繁，请稍后重试"})
            await websocket.close(code=1008)
            return

        get_ledger_or_404(db, ledger_id)
        require_ledger_member(db, ledger_id, current_user)
        members = get_active_voice_members(db, ledger_id)

        audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=32)
        receiver = asyncio.create_task(_receive_voice_audio(
            websocket,
            audio_queue,
            session_id=session_id,
            audio_stats=audio_stats,
        ))
        await websocket.send_json({"type": "ready"})
        logger.info(
            "语音会话已就绪 session_id=%s ledger_id=%s user_id=%s member_count=%d",
            session_id,
            ledger_id,
            current_user.id,
            len(members),
        )

        asr_started_at = perf_counter()
        final_segments: list[str] = []
        latest_partial = ""
        # 把当前账本成员名字作为热词传给腾讯 ASR（同音替换权重 100）
        voice_hotwords = [str(m.get("name") or "").strip() for m in members]
        voice_hotwords = [w for w in voice_hotwords if w]
        logger.info("语音会话热词 session_id=%s hotwords=%s", session_id, voice_hotwords)
        async for event in stream_tencent_asr(
            _audio_chunks(audio_queue, audio_stats=audio_stats),
            wav_name=str(ledger_id),
            session_id=session_id,
            hotwords=voice_hotwords,
        ):
            text = str(event.get("text") or "").strip()
            if not text:
                continue
            if event.get("type") == "final":
                final_segments.append(text)
                logger.info(
                    "语音会话收到最终转写 session_id=%s segment_chars=%d final_segment_count=%d elapsed_ms=%.0f",
                    session_id,
                    len(text),
                    len(final_segments),
                    _elapsed_ms(),
                )
                await websocket.send_json({"type": "final_transcript", "text": text})
            else:
                latest_partial = text
                logger.debug(
                    "语音会话收到部分转写 session_id=%s text_chars=%d elapsed_ms=%.0f",
                    session_id,
                    len(text),
                    _elapsed_ms(),
                )
                await websocket.send_json({"type": "partial_transcript", "text": text})

        asr_done_at = perf_counter()
        transcript = " ".join(final_segments).strip() or latest_partial
        first_chunk_at = audio_stats.get("first_chunk_at")
        stop_at = audio_stats.get("stop_at")
        logger.info(
            "语音会话识别完成 session_id=%s transcript_chars=%d final_segment_count=%d "
            "audio_chunks=%d audio_bytes=%d "
            "phase_ms={accept=%.0f, first_audio=%.0f, stop=%.0f, asr=%.0f, tail_after_stop=%.0f} total_ms=%.0f",
            session_id,
            len(transcript),
            len(final_segments),
            audio_stats["chunks"],
            audio_stats["bytes"],
            (first_chunk_at - started_at) * 1000 if first_chunk_at else -1,
            (first_chunk_at - started_at) * 1000 if first_chunk_at else -1,
            (stop_at - started_at) * 1000 if stop_at else -1,
            (asr_done_at - asr_started_at) * 1000,
            (asr_done_at - stop_at) * 1000 if stop_at else -1,
            _elapsed_ms(),
        )
        if not transcript:
            raise VoiceExpenseError("没有识别到语音内容，请再试一次")

        llm_started_at = perf_counter()
        draft = create_voice_expense_draft_from_transcript(
            transcript=transcript,
            members=members,
            current_user_id=str(current_user.id),
        )
        llm_done_at = perf_counter()
        participant_count = len(draft.get("participant_member_ids", [])) if isinstance(draft, dict) else 0
        logger.info(
            "语音会话生成草稿成功 session_id=%s amount=%s participant_count=%d llm_ms=%.0f total_ms=%.0f",
            session_id,
            draft.get("amount") if isinstance(draft, dict) else None,
            participant_count,
            (llm_done_at - llm_started_at) * 1000,
            _elapsed_ms(),
        )
        await websocket.send_json({"type": "draft", "data": jsonable_encoder(draft)})
        await websocket.close()
    except WebSocketDisconnect:
        logger.info(
            "语音会话客户端断开 session_id=%s ledger_id=%s audio_chunks=%d audio_bytes=%d",
            session_id,
            ledger_id,
            audio_stats["chunks"],
            audio_stats["bytes"],
        )
        return
    except (TencentASRError, VoiceExpenseError) as exc:
        logger.warning(
            "语音会话失败 session_id=%s ledger_id=%s error=%s",
            session_id,
            ledger_id,
            str(exc),
        )
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close(code=1011)
    except HTTPException as exc:
        logger.warning(
            "语音会话请求被拒绝 session_id=%s ledger_id=%s status_code=%s detail=%s",
            session_id,
            ledger_id,
            exc.status_code,
            exc.detail,
        )
        await websocket.send_json({"type": "error", "message": str(exc.detail)})
        await websocket.close(code=1008)
    except Exception as exc:
        logger.exception(
            "语音会话未知异常 session_id=%s ledger_id=%s error=%s",
            session_id,
            ledger_id,
            str(exc),
        )
        await websocket.send_json({"type": "error", "message": "语音记账服务异常，请稍后重试"})
        await websocket.close(code=1011)
    finally:
        if receiver:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        db.close()
        logger.info(
            "语音会话结束 session_id=%s ledger_id=%s audio_chunks=%d audio_bytes=%d duration_ms=%.2f",
            session_id,
            ledger_id,
            audio_stats["chunks"],
            audio_stats["bytes"],
            (perf_counter() - started_at) * 1000,
        )


async def _receive_voice_audio(
    websocket: WebSocket,
    audio_queue: asyncio.Queue[bytes | None],
    *,
    session_id: str,
    audio_stats: dict[str, int],
) -> None:
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                logger.info(
                    "语音会话客户端断开音频输入 session_id=%s audio_chunks=%d audio_bytes=%d",
                    session_id,
                    audio_stats["chunks"],
                    audio_stats["bytes"],
                )
                await audio_queue.put(None)
                return
            if "bytes" in message and message["bytes"]:
                audio_stats["chunks"] += 1
                audio_stats["bytes"] += len(message["bytes"])
                if audio_stats["chunks"] == 1:
                    audio_stats["first_chunk_at"] = perf_counter()
                if audio_stats["chunks"] == 1 or audio_stats["chunks"] % 50 == 0:
                    logger.info(
                        "语音会话收到音频数据 session_id=%s audio_chunks=%d audio_bytes=%d",
                        session_id,
                        audio_stats["chunks"],
                        audio_stats["bytes"],
                    )
                await audio_queue.put(message["bytes"])
                continue

            if "text" in message and message["text"]:
                try:
                    event = json.loads(message["text"])
                except json.JSONDecodeError:
                    logger.warning(
                        "语音会话收到无效控制消息 session_id=%s raw_chars=%d",
                        session_id,
                        len(message["text"]),
                    )
                    continue
                event_type = event.get("type")
                if event_type == "start":
                    audio = event.get("audio") if isinstance(event.get("audio"), dict) else {}
                    logger.info(
                        "语音会话收到开始元信息 session_id=%s format=%s sample_rate=%s channels=%s",
                        session_id,
                        audio.get("format"),
                        audio.get("sample_rate"),
                        audio.get("channels"),
                    )
                    continue
                if event_type in {"stop", "cancel"}:
                    log_message = "语音会话收到停止指令" if event_type == "stop" else "语音会话收到取消指令"
                    if event_type == "stop":
                        audio_stats["stop_at"] = perf_counter()
                    logger.info(
                        "%s session_id=%s audio_chunks=%d audio_bytes=%d",
                        log_message,
                        session_id,
                        audio_stats["chunks"],
                        audio_stats["bytes"],
                    )
                    await audio_queue.put(None)
                    return
                logger.warning(
                    "语音会话收到未知控制消息 session_id=%s event_type=%s",
                    session_id,
                    event_type,
                )
    except WebSocketDisconnect:
        logger.info(
            "语音会话音频接收断开 session_id=%s audio_chunks=%d audio_bytes=%d",
            session_id,
            audio_stats["chunks"],
            audio_stats["bytes"],
        )
        await audio_queue.put(None)
        raise


async def _audio_chunks(audio_queue: asyncio.Queue[bytes | None], *, audio_stats=None):
    while True:
        chunk = await audio_queue.get()
        if chunk is None:
            return
        yield chunk


@router.post("/ledgers/{ledger_id}/voice-draft", response_model=VoiceExpenseDraft)
def create_voice_draft(
    ledger_id: UUID,
    audio: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Transcribe audio and return a validated expense draft without saving it."""
    enforce_rate_limit(
        f"voice:user:{current_user.id}",
        limit=20,
        window_seconds=60,
        detail="语音记账请求过于频繁，请稍后重试",
    )
    get_ledger_or_404(db, ledger_id)
    require_ledger_member(db, ledger_id, current_user)
    content_type = audio.content_type or "application/octet-stream"
    if not content_type.startswith("audio/"):
        raise HTTPException(status_code=400, detail="请上传音频文件")

    audio_bytes = audio.file.read(10 * 1024 * 1024 + 1)
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="音频文件为空")
    if len(audio_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="音频文件不能超过 10 MB")

    members = get_active_voice_members(db, ledger_id)
    try:
        return create_voice_expense_draft(
            audio=audio_bytes,
            filename=audio.filename or "voice.m4a",
            content_type=content_type,
            members=members,
            current_user_id=str(current_user.id),
        )
    except VoiceExpenseError as exc:
        status_code = 503 if "OPENAI_API_KEY" in str(exc) else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/ledgers/{ledger_id}/expenses", response_model=ExpenseResponse, status_code=status.HTTP_201_CREATED)
def create_expense(
    ledger_id: UUID,
    expense: ExpenseCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new expense in a ledger"""
    # Check if ledger exists and user is a member
    ledger = get_ledger_or_404(db, ledger_id)
    require_ledger_member(db, ledger_id, current_user)

    resolved_splits, payer_member = _resolve_expense_splits(
        ledger_id=ledger_id,
        payload=expense,
        db=db,
    )

    # Create expense
    db_expense = _persist_expense_row(
        db=db,
        ledger_id=ledger_id,
        payload=expense,
        created_by=current_user.id,
        resolved_splits=resolved_splits,
        group_id=expense.group_id,
        commit=True,
    )

    required_participants = _required_participants_for(resolved_splits, current_user.id, expense.payer_id)
    if required_participants:
        send_push_safely(db, required_participants, build_payload(
            event=PushEvent.EXPENSE_CREATED,
            actor_name=current_user.display_name or current_user.username,
            ledger_name=ledger.name,
            ledger_id=str(ledger_id),
            expense_name=db_expense.title,
            expense_id=str(db_expense.id),
        ))

    return db_expense


def _equal_splits(total: Decimal, member_ids: list[UUID]) -> list[ExpenseSplitCreate]:
    n = len(member_ids)
    cents = int((total * 100).to_integral_value())
    base, rem = divmod(cents, n)
    splits: list[ExpenseSplitCreate] = []
    for i, mid in enumerate(member_ids):
        part = base + (1 if i < rem else 0)
        splits.append(ExpenseSplitCreate(member_id=mid, amount=Decimal(part) / 100))
    return splits


def _required_participants_for(resolved_splits, created_by, payer_id) -> set:
    registered = {m.user_id for _, m in resolved_splits if m.user_id is not None}
    return required_confirmation_user_ids(
        registered,
        created_by=created_by,
        payer_id=payer_id,
    )


def _persist_expense_row(
    *,
    db: Session,
    ledger_id: UUID,
    payload: ExpenseCreate,
    created_by,
    resolved_splits,
    group_id=None,
    commit: bool = True,
) -> Expense:
    db_expense = Expense(
        ledger_id=ledger_id,
        payer_id=payload.payer_id,
        created_by=created_by,
        title=payload.title,
        total_amount=payload.total_amount,
        kind=payload.kind,
        group_id=group_id if group_id is not None else payload.group_id,
        note=payload.note,
        category=payload.category,
        icon_type=payload.icon_type,
        icon_value=payload.icon_value,
        expense_date=payload.expense_date,
        status=ExpenseStatus.PENDING,
    )
    db.add(db_expense)
    db.flush()

    for split, member in resolved_splits:
        db.add(
            ExpenseSplit(
                expense_id=db_expense.id,
                user_id=member.user_id,
                member_id=member.id,
                amount=split.amount,
            )
        )

    registered_participant_ids = {
        member.user_id for _, member in resolved_splits if member.user_id is not None
    }
    auto_confirm_ids = confirmation_exempt_user_ids(
        created_by=created_by,
        payer_id=payload.payer_id,
    ) & registered_participant_ids
    for user_id in auto_confirm_ids:
        db.add(
            ExpenseConfirmation(
                expense_id=db_expense.id,
                user_id=user_id,
                status="confirmed",
            )
        )

    required = required_confirmation_user_ids(
        registered_participant_ids,
        created_by=created_by,
        payer_id=payload.payer_id,
    )
    if not required:
        db_expense.status = ExpenseStatus.CONFIRMED

    if commit:
        db.commit()
        db.refresh(db_expense)
    return db_expense


@router.post(
    "/ledgers/{ledger_id}/expenses/compound",
    response_model=CompoundExpenseResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_compound_expense(
    ledger_id: UUID,
    body: CompoundExpenseCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create linked cost (expense) + income rows that appear as one bill in clients."""
    ledger = get_ledger_or_404(db, ledger_id)
    require_ledger_member(db, ledger_id, current_user)

    member_ids = list(dict.fromkeys(body.participant_member_ids))  # preserve order, unique
    if len(member_ids) < 1:
        raise HTTPException(status_code=400, detail="At least one participant is required")

    cost_splits = _equal_splits(body.cost_amount, member_ids)
    income_splits = _equal_splits(body.income_amount, member_ids)
    group_id = uuid4()

    cost_payload = ExpenseCreate(
        title=body.title,
        total_amount=body.cost_amount,
        kind="expense",
        note=body.note,
        expense_date=body.expense_date,
        category=body.category,
        icon_type=body.icon_type,
        icon_value=body.icon_value,
        payer_id=body.cost_payer_id,
        splits=cost_splits,
        group_id=group_id,
    )
    income_payload = ExpenseCreate(
        title=body.title,
        total_amount=body.income_amount,
        kind="income",
        note=body.note,
        expense_date=body.expense_date,
        category=body.category,
        icon_type=body.icon_type,
        icon_value=body.icon_value,
        payer_id=body.income_receiver_id,
        splits=income_splits,
        group_id=group_id,
    )

    cost_resolved, _ = _resolve_expense_splits(ledger_id=ledger_id, payload=cost_payload, db=db)
    income_resolved, _ = _resolve_expense_splits(ledger_id=ledger_id, payload=income_payload, db=db)

    cost_row = _persist_expense_row(
        db=db,
        ledger_id=ledger_id,
        payload=cost_payload,
        created_by=current_user.id,
        resolved_splits=cost_resolved,
        group_id=group_id,
        commit=False,
    )
    income_row = _persist_expense_row(
        db=db,
        ledger_id=ledger_id,
        payload=income_payload,
        created_by=current_user.id,
        resolved_splits=income_resolved,
        group_id=group_id,
        commit=False,
    )
    db.commit()
    db.refresh(cost_row)
    db.refresh(income_row)

    notify_ids = (
        _required_participants_for(cost_resolved, current_user.id, body.cost_payer_id)
        | _required_participants_for(income_resolved, current_user.id, body.income_receiver_id)
    )
    if notify_ids:
        send_push_safely(
            db,
            notify_ids,
            build_payload(
                event=PushEvent.EXPENSE_CREATED,
                actor_name=current_user.display_name or current_user.username,
                ledger_name=ledger.name,
                ledger_id=str(ledger_id),
                expense_name=body.title,
                expense_id=str(cost_row.id),
            ),
        )

    return CompoundExpenseResponse(
        group_id=group_id,
        cost=ExpenseResponse.model_validate(cost_row),
        income=ExpenseResponse.model_validate(income_row),
    )


@router.put("/{expense_id}", response_model=ExpenseResponse)
def update_expense(
    expense_id: UUID,
    payload: ExpenseUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit a pending expense (creator only). Resets other members' confirmations."""
    expense = db.query(Expense).filter(Expense.id == expense_id).first()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")

    require_ledger_member(db, expense.ledger_id, current_user)
    if expense.created_by != current_user.id:
        raise HTTPException(status_code=403, detail="Only the expense creator can edit this expense")
    if expense.status != ExpenseStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail=f"Only pending expenses can be edited (current: {expense.status.value})",
        )

    resolved_splits, _payer_member = _resolve_expense_splits(
        ledger_id=expense.ledger_id,
        payload=payload,
        db=db,
    )

    expense.title = payload.title
    expense.total_amount = payload.total_amount
    expense.kind = payload.kind
    expense.note = payload.note
    expense.category = payload.category
    expense.icon_type = payload.icon_type
    expense.icon_value = payload.icon_value
    expense.expense_date = payload.expense_date
    expense.payer_id = payload.payer_id
    expense.status = ExpenseStatus.PENDING

    # Replace splits + confirmations so previous acknowledgements cannot stick.
    db.query(ExpenseSplit).filter(ExpenseSplit.expense_id == expense.id).delete(
        synchronize_session=False
    )
    db.query(ExpenseConfirmation).filter(ExpenseConfirmation.expense_id == expense.id).delete(
        synchronize_session=False
    )
    db.flush()

    for split, member in resolved_splits:
        db.add(
            ExpenseSplit(
                expense_id=expense.id,
                user_id=member.user_id,
                member_id=member.id,
                amount=split.amount,
            )
        )

    registered_participant_ids = {
        member.user_id for _, member in resolved_splits if member.user_id is not None
    }
    auto_confirm_ids = confirmation_exempt_user_ids(
        created_by=expense.created_by,
        payer_id=expense.payer_id,
    ) & registered_participant_ids
    for user_id in auto_confirm_ids:
        db.add(
            ExpenseConfirmation(
                expense_id=expense.id,
                user_id=user_id,
                status="confirmed",
            )
        )

    required_participants = required_confirmation_user_ids(
        registered_participant_ids,
        created_by=expense.created_by,
        payer_id=expense.payer_id,
    )
    if not required_participants:
        expense.status = ExpenseStatus.CONFIRMED

    db.commit()
    db.refresh(expense)

    recipients = required_participants
    if recipients:
        ledger = get_ledger_or_404(db, expense.ledger_id)
        send_push_safely(
            db,
            recipients,
            build_payload(
                event=PushEvent.EXPENSE_UPDATED,
                actor_name=current_user.display_name or current_user.username,
                ledger_name=ledger.name,
                ledger_id=str(expense.ledger_id),
                expense_name=expense.title,
                expense_id=str(expense.id),
            ),
        )

    return expense


@router.get("/ledgers/{ledger_id}/expenses", response_model=List[ExpenseWithDetails])
def get_expenses(
    ledger_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all expenses in a ledger"""
    # Check if user is a member
    require_ledger_member(db, ledger_id, current_user)

    expenses = (
        db.query(Expense)
        .options(
            joinedload(Expense.payer),
            selectinload(Expense.splits),
            selectinload(Expense.confirmations),
        )
        .filter(Expense.ledger_id == ledger_id)
        .order_by(Expense.created_at.desc())
        .all()
    )

    result = []
    for exp in expenses:
        payer = exp.payer
        splits = exp.splits
        confirmations = exp.confirmations
        if exp.status == ExpenseStatus.PENDING:
            split_ids = {s.user_id for s in splits if s.user_id is not None}
            required_ids = required_confirmation_user_ids(
                split_ids,
                created_by=exp.created_by,
                payer_id=exp.payer_id,
            )
            confirmed_ids = {c.user_id for c in confirmations if c.status == "confirmed"}
            if required_ids <= confirmed_ids:
                exp.status = ExpenseStatus.CONFIRMED

        result.append(
            expense_to_with_details(
                exp,
                status=exp.status.value,
                payer=payer,
                splits=splits,
                confirmations=confirmations,
            )
        )

    if db.dirty:
        db.commit()
    return result


@router.post("/{expense_id}/confirm", response_model=ExpenseResponse)
def confirm_expense(
    expense_id: UUID,
    request: ConfirmExpenseRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Confirm or reject an expense"""
    expense = db.query(Expense).filter(Expense.id == expense_id).first()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")

    # Check if user is a member of the ledger
    require_ledger_member(db, expense.ledger_id, current_user)

    split_participants = {
        s.user_id
        for s in db.query(ExpenseSplit).filter(ExpenseSplit.expense_id == expense_id).all()
        if s.user_id is not None
    }
    if current_user.id in confirmation_exempt_user_ids(
        created_by=expense.created_by,
        payer_id=expense.payer_id,
    ):
        raise HTTPException(
            status_code=400,
            detail="Expense creator and payer do not need to confirm",
        )
    if current_user.id not in split_participants:
        raise HTTPException(status_code=403, detail="Only expense participants can confirm this expense")

    # Check if already confirmed or rejected
    if expense.status != ExpenseStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Expense is already {expense.status.value}")

    # Validate status
    if request.status not in ["confirmed", "rejected"]:
        raise HTTPException(status_code=400, detail="Status must be 'confirmed' or 'rejected'")

    # Check if user already confirmed/rejected this expense
    existing = db.query(ExpenseConfirmation).filter(
        ExpenseConfirmation.expense_id == expense_id,
        ExpenseConfirmation.user_id == current_user.id
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="You have already responded to this expense")

    # Create confirmation record
    confirmation = ExpenseConfirmation(
        expense_id=expense_id,
        user_id=current_user.id,
        status=request.status,
    )
    db.add(confirmation)
    db.flush()  # Flush to get the new confirmation in the query

    # Check if all required participants have confirmed
    if request.status == "confirmed":
        confirmations = db.query(ExpenseConfirmation).filter(
            ExpenseConfirmation.expense_id == expense_id,
            ExpenseConfirmation.status == "confirmed"
        ).all()
        confirmed_ids = {c.user_id for c in confirmations}

        required_participants = required_confirmation_user_ids(
            split_participants,
            created_by=expense.created_by,
            payer_id=expense.payer_id,
        )
        if required_participants <= confirmed_ids:
            expense.status = ExpenseStatus.CONFIRMED

    elif request.status == "rejected":
        expense.status = ExpenseStatus.REJECTED

    db.commit()
    db.refresh(expense)

    if expense.created_by != current_user.id:
        ledger = get_ledger_or_404(db, expense.ledger_id)
        event = PushEvent.EXPENSE_CONFIRMED if request.status == "confirmed" else PushEvent.EXPENSE_REJECTED
        send_push_safely(db, [expense.created_by], build_payload(
            event=event,
            actor_name=current_user.display_name or current_user.username,
            ledger_name=ledger.name,
            ledger_id=str(expense.ledger_id),
            expense_name=expense.title,
            expense_id=str(expense.id),
        ))

    return expense


@router.post("/{expense_id}/reject", response_model=ExpenseResponse)
def reject_expense(
    expense_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Reject an expense (alias for confirm with rejected status)"""
    return confirm_expense(expense_id, ConfirmExpenseRequest(status="rejected"), db, current_user)


@router.delete("/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_expense(
    expense_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete an expense (creator or ledger owner)."""
    expense = db.query(Expense).filter(Expense.id == expense_id).first()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")

    # Defensive: caller must be an active member of the ledger this expense belongs to
    require_ledger_member(db, expense.ledger_id, current_user)

    ledger = get_ledger_or_404(db, expense.ledger_id)
    if expense.created_by != current_user.id and ledger.owner_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the expense creator or ledger owner can delete this expense",
        )

    db.delete(expense)
    db.commit()
    return None


@router.get("/{expense_id}", response_model=ExpenseWithDetails)
def get_expense(
    expense_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get expense details"""
    expense = db.query(Expense).filter(Expense.id == expense_id).first()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")

    # Check if user is a member
    require_ledger_member(db, expense.ledger_id, current_user)

    payer = db.query(User).filter(User.id == expense.payer_id).first()
    splits = db.query(ExpenseSplit).filter(ExpenseSplit.expense_id == expense.id).all()
    confirmations = db.query(ExpenseConfirmation).filter(ExpenseConfirmation.expense_id == expense.id).all()

    return expense_to_with_details(
        expense,
        status=expense.status.value,
        payer=payer,
        splits=splits,
        confirmations=confirmations,
    )
