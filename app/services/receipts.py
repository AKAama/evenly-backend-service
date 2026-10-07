"""Receipt storage uses the same COS service as avatars and covers."""
import logging
import re
from urllib.parse import urlsplit

from fastapi import HTTPException

from app.config import settings
from app.models import Expense

logger = logging.getLogger(__name__)
MAX_RECEIPTS = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def validate_receipt_urls(urls, ledger_id, user_id, existing=()):
    if len(urls) > MAX_RECEIPTS or len(set(urls)) != len(urls):
        raise HTTPException(400, "凭据最多 3 张，且不能重复")
    for url in urls:
        if url in existing:
            continue
        if not settings.cos:
            raise HTTPException(503, "图片上传未配置")
        base = urlsplit(settings.cos.base_url.rstrip("/"))
        parsed = urlsplit(url)
        prefix = f"{base.path}/receipts/{ledger_id}/{user_id}/"
        if (parsed.scheme != base.scheme or parsed.netloc != base.netloc
                or parsed.query or parsed.fragment or not parsed.path.startswith(prefix)
                or not re.fullmatch(r"[0-9a-f-]{36}\.(jpg|png|gif|webp)", parsed.path[len(prefix):])):
            raise HTTPException(400, "无效的凭据地址")


def image_extension(contents, content_type):
    if not contents or len(contents) > MAX_IMAGE_BYTES:
        raise HTTPException(400, "每张凭据图片不能超过 5MB，且不能为空")
    valid = {
        "image/jpeg": ("jpg", contents.startswith(b"\xff\xd8\xff")),
        "image/png": ("png", contents.startswith(b"\x89PNG\r\n\x1a\n")),
        "image/gif": ("gif", contents.startswith((b"GIF87a", b"GIF89a"))),
        "image/webp": ("webp", contents.startswith(b"RIFF") and contents[8:12] == b"WEBP"),
    }
    ext, matches = valid.get(content_type, (None, False))
    if not matches:
        raise HTTPException(400, "凭据仅支持 JPEG、PNG、GIF 或 WebP 图片")
    return ext


def receipt_object_key(url, ledger_id):
    """Sign only receipt objects in this bucket and ledger, never arbitrary URLs."""
    if not settings.cos:
        raise HTTPException(503, "图片上传未配置")
    parsed = urlsplit(url)
    base = urlsplit(settings.cos.base_url.rstrip("/"))
    bucket_host = f"{settings.cos.bucket}.cos.{settings.cos.region}.myqcloud.com"
    if parsed.scheme != "https" or parsed.netloc not in (base.netloc, bucket_host) or parsed.query or parsed.fragment:
        raise HTTPException(400, "无效的凭据地址")
    # CDN URLs may have a configured path prefix; COS keys do not include it.
    prefix = base.path + "/" if parsed.netloc == base.netloc else "/"
    if not parsed.path.startswith(prefix):
        raise HTTPException(400, "无效的凭据地址")
    key = parsed.path[len(prefix):]
    if not re.fullmatch(rf"receipts/{re.escape(str(ledger_id))}/[0-9a-f-]{{36}}/[0-9a-f-]{{36}}\.(jpg|png|gif|webp)", key):
        raise HTTPException(400, "无效的凭据地址")
    return key


def cleanup_unreferenced_receipts(db, ledger_id, urls):
    """Delete only uploaded objects that are no longer referenced in this ledger."""
    if not urls:
        return
    from app.services.cos import get_cos_service
    try:
        referenced = {url for row in db.query(Expense).filter(Expense.ledger_id == ledger_id).all()
                      for url in (row.receipt_urls or [])}
        service = get_cos_service()
        if service is None:
            return
        for url in urls:
            if url not in referenced:
                if not service.delete_file(url):
                    logger.warning("删除未使用的凭据失败 ledger_id=%s", ledger_id)
    except Exception:
        logger.exception("清理未使用的凭据失败 ledger_id=%s", ledger_id)
