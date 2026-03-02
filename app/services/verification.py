import random
import string
import time
from typing import Dict

# 内存存储验证码（生产环境建议用 Redis）
# 格式: { email: { code: str, expires_at: float } }
verification_codes: Dict[str, dict] = {}

# 验证码有效期（秒）
CODE_EXPIRE_SECONDS = 600  # 10 分钟


def generate_code(length: int = 6) -> str:
    """生成纯数字验证码"""
    return ''.join(random.choices(string.digits, k=length))


def send_verification_code(email: str) -> bool:
    """发送验证码到邮箱"""
    # 检查是否已发送过验证码，且未过期
    if email in verification_codes:
        existing = verification_codes[email]
        # 如果 60 秒内重复发送，返回 False
        if time.time() - existing.get('sent_at', 0) < 60:
            return False

    # 生成新验证码
    code = generate_code()
    verification_codes[email] = {
        'code': code,
        'expires_at': time.time() + CODE_EXPIRE_SECONDS,
        'sent_at': time.time()
    }

    # 发送邮件
    from app.services.email import get_email_service
    email_service = get_email_service()
    if email_service:
        return email_service.send_verification_code(email, code)
    else:
        # 如果没有配置邮件服务，打印到控制台
        print(f"\n{'='*50}")
        print(f"验证码发送（演示模式）: {email}")
        print(f"验证码: {code}")
        print(f"{'='*50}\n")
        return True


def verify_code(email: str, code: str) -> bool:
    """验证验证码"""
    if email not in verification_codes:
        return False

    stored = verification_codes[email]

    # 检查是否过期
    if time.time() > stored['expires_at']:
        del verification_codes[email]
        return False

    # 验证验证码
    if stored['code'] == code:
        # 验证成功后删除验证码
        del verification_codes[email]
        return True

    return False
