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
    ExpenseResponse,
    ExpenseWithDetails,
    ConfirmExpenseRequest,
    ExpenseSplitResponse,
    ExpenseConfirmationResponse,
    VoiceExpenseDraft,
)
from app.schemas.user import UserResponse
from app.services.auth import decode_token, get_user_by_id
from app.services.tencent_asr import TencentASRError, stream_tencent_asr
from app.services.voice_expense import (
    VoiceExpenseError,
    create_voice_expense_draft,
    create_voice_expense_draft_from_transcript,
)
from app.utils.deps import get_current_user, get_ledger_or_404, require_ledger_member

router = APIRouter(prefix="/expenses", tags=["expenses"])
logger = logging.getLogger(__name__)

CENT = Decimal("0.01")


def normalize_money(value: Decimal) -> Decimal:
    return value.quantize(CENT)


def get_active_voice_members(db: Session, ledger_id: UUID) -> list[dict[str, str | bool | None]]:
    rows = (
        db.query(LedgerMember)
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
    get_ledger_or_404(db, ledger_id)
    require_ledger_member(db, ledger_id, current_user)

    if expense.total_amount <= 0:
        raise HTTPException(status_code=400, detail="Expense amount must be greater than zero")

    if not expense.splits:
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
    for split in expense.splits:
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

    if any(split.amount <= 0 for split in expense.splits):
        raise HTTPException(status_code=400, detail="Split amounts must be greater than zero")

    payer_member = members_by_user_id.get(expense.payer_id)
    if payer_member is None or payer_member.is_temporary:
        raise HTTPException(status_code=400, detail="Payer must be a registered ledger member")

    # Validate splits total equals expense total
    split_total = normalize_money(sum(s.amount for s in expense.splits))
    expense_total = normalize_money(expense.total_amount)
    if split_total != expense_total:
        raise HTTPException(
            status_code=400,
            detail=f"Split total ({split_total}) must equal expense amount ({expense_total})"
        )

    # Validate payer is in splits
    payer_in_splits = any(member.id == payer_member.id for _, member in resolved_splits)
    if not payer_in_splits:
        raise HTTPException(status_code=400, detail="Payer must be included in splits")

    # Create expense
    db_expense = Expense(
        ledger_id=ledger_id,
        payer_id=expense.payer_id,
        created_by=current_user.id,
        title=expense.title,
        total_amount=expense.total_amount,
        note=expense.note,
        expense_date=expense.expense_date,
        status=ExpenseStatus.PENDING,
    )
    db.add(db_expense)
    # Keep the expense and all split rows in one transaction. If any split
    # violates a constraint, no orphan expense is left behind.
    db.flush()

    # Create splits
    for split, member in resolved_splits:
        db_split = ExpenseSplit(
            expense_id=db_expense.id,
            user_id=member.user_id,
            member_id=member.id,
            amount=split.amount,
        )
        db.add(db_split)

    # Creating an expense is itself the creator's acknowledgement. Record it
    # immediately when the creator is one of the registered participants, so
    # they are never asked to confirm their own expense again.
    registered_participant_ids = {
        member.user_id for _, member in resolved_splits if member.user_id is not None
    }
    if current_user.id in registered_participant_ids:
        db.add(ExpenseConfirmation(
            expense_id=db_expense.id,
            user_id=current_user.id,
            status="confirmed",
        ))
        if not (registered_participant_ids - {current_user.id}):
            db_expense.status = ExpenseStatus.CONFIRMED

    db.commit()
    db.refresh(db_expense)

    return db_expense


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
            required_ids = {s.user_id for s in splits if s.user_id is not None} - {exp.created_by}
            confirmed_ids = {c.user_id for c in confirmations if c.status == "confirmed"}
            if required_ids <= confirmed_ids:
                exp.status = ExpenseStatus.CONFIRMED

        response = ExpenseWithDetails(
            id=exp.id,
            ledger_id=exp.ledger_id,
            payer_id=exp.payer_id,
            created_by=exp.created_by,
            title=exp.title,
            total_amount=exp.total_amount,
            note=exp.note,
            expense_date=exp.expense_date,
            status=exp.status.value,
            created_at=exp.created_at,
            updated_at=exp.updated_at,
            payer=UserResponse.model_validate(payer),
            splits=[
                ExpenseSplitResponse(
                    id=s.id,
                    expense_id=s.expense_id,
                    user_id=s.user_id,
                    member_id=s.member_id,
                    amount=s.amount,
                    created_at=s.created_at
                )
                for s in splits
            ],
            confirmations=[
                ExpenseConfirmationResponse(
                    id=c.id,
                    expense_id=c.expense_id,
                    user_id=c.user_id,
                    status=c.status,
                    created_at=c.created_at
                )
                for c in confirmations
            ]
        )
        result.append(response)

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
    if current_user.id == expense.created_by:
        raise HTTPException(status_code=400, detail="Expense creator does not need to confirm")
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

    # Check if all members have confirmed
    if request.status == "confirmed":
        # Check if all members have now confirmed
        confirmations = db.query(ExpenseConfirmation).filter(
            ExpenseConfirmation.expense_id == expense_id,
            ExpenseConfirmation.status == "confirmed"
        ).all()
        confirmed_ids = {c.user_id for c in confirmations}

        required_participants = split_participants - {expense.created_by}
        if required_participants <= confirmed_ids:
            expense.status = ExpenseStatus.CONFIRMED

    elif request.status == "rejected":
        expense.status = ExpenseStatus.REJECTED

    db.commit()
    db.refresh(expense)

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

    return ExpenseWithDetails(
        id=expense.id,
        ledger_id=expense.ledger_id,
        payer_id=expense.payer_id,
        created_by=expense.created_by,
        title=expense.title,
        total_amount=expense.total_amount,
        note=expense.note,
        expense_date=expense.expense_date,
        status=expense.status.value,
        created_at=expense.created_at,
        updated_at=expense.updated_at,
        payer=UserResponse.model_validate(payer),
        splits=[
            ExpenseSplitResponse(
                id=s.id,
                expense_id=s.expense_id,
                user_id=s.user_id,
                member_id=s.member_id,
                amount=s.amount,
                created_at=s.created_at
            )
            for s in splits
        ],
        confirmations=[
            ExpenseConfirmationResponse(
                id=c.id,
                expense_id=c.expense_id,
                user_id=c.user_id,
                status=c.status,
                created_at=c.created_at
            )
            for c in confirmations
        ]
    )
