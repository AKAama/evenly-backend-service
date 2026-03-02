import json
import hashlib
import hmac
import time
from datetime import datetime
import requests

from app.config import settings


def sign(key, msg):
    """计算签名摘要"""
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


class EmailService:
    def __init__(self):
        if not settings.smtp:
            raise RuntimeError("SMTP not configured")

        self.secret_id = settings.smtp.secret_id
        self.secret_key = settings.smtp.secret_key
        self.from_email = settings.smtp.from_email

    def send_email(self, to_email: str, subject: str, template_id = None, template_data: dict = None) -> bool:
        """Send an email via Tencent Cloud SES REST API"""
        try:
            endpoint = "ses.tencentcloudapi.com"
            region = "ap-hongkong"
            service = "ses"
            action = "SendEmail"
            version = "2020-10-02"
            algorithm = "TC3-HMAC-SHA256"

            # 当前时间戳
            timestamp = int(time.time())
            date = datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")

            # 请求参数 - 使用 Destination 数组
            payload = {
                "FromEmailAddress": self.from_email,
                "Destination": [to_email],
                "Subject": subject
            }

            if template_id and template_data:
                payload["Template"] = {
                    "TemplateID": int(template_id),
                    "TemplateData": json.dumps(template_data)
                }

            # ************* 步骤 1：拼接规范请求串 *************
            http_request_method = "POST"
            canonical_uri = "/"
            canonical_querystring = ""
            ct = "application/json"
            payload_str = json.dumps(payload)
            canonical_headers = f"content-type:{ct}\nhost:{endpoint}\nx-tc-action:{action.lower()}\n"
            signed_headers = "content-type;host;x-tc-action"
            hashed_request_payload = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

            canonical_request = (
                http_request_method + "\n" +
                canonical_uri + "\n" +
                canonical_querystring + "\n" +
                canonical_headers + "\n" +
                signed_headers + "\n" +
                hashed_request_payload
            )

            # ************* 步骤 2：拼接待签名字符串 *************
            credential_scope = date + "/" + service + "/" + "tc3_request"
            hashed_canonical_request = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
            string_to_sign = (
                algorithm + "\n" +
                str(timestamp) + "\n" +
                credential_scope + "\n" +
                hashed_canonical_request
            )

            # ************* 步骤 3：计算签名 *************
            secret_date = sign(("TC3" + self.secret_key).encode("utf-8"), date)
            secret_service = sign(secret_date, service)
            secret_signing = sign(secret_service, "tc3_request")
            signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

            # ************* 步骤 4：拼接 Authorization *************
            authorization = (
                algorithm + " " +
                "Credential=" + self.secret_id + "/" + credential_scope + ", " +
                "SignedHeaders=" + signed_headers + ", " +
                "Signature=" + signature
            )

            # 发送请求
            url = "https://" + endpoint + "/"
            headers = {
                "Authorization": authorization,
                "Content-Type": ct,
                "Host": endpoint,
                "X-TC-Action": action,
                "X-TC-Timestamp": str(timestamp),
                "X-TC-Version": version,
                "X-TC-Region": region
            }

            response = requests.post(url, headers=headers, data=payload_str, timeout=30)
            result = response.json()

            print(f"SES API Response: {result}")

            if "Response" in result and "RequestId" in result["Response"]:
                return True
            else:
                if "Response" in result and "Error" in result["Response"]:
                    print(f"SES API Error: {result['Response']['Error']}")
                return False

        except Exception as e:
            print(f"Failed to send email: {e}")
            import traceback
            traceback.print_exc()
            return False

    def send_verification_code(self, to_email: str, code: str) -> bool:
        """Send verification code email using Tencent Cloud template"""
        subject = "【Evenly】邮箱验证码"
        template_id = getattr(settings.smtp, 'template_id', None)

        if template_id:
            return self.send_email(
                to_email,
                subject,
                template_id=template_id,
                template_data={"CODE": code}
            )
        else:
            print("Warning: Template ID not configured")
            return False


_email_service: EmailService | None = None


def get_email_service() -> EmailService | None:
    global _email_service
    if _email_service is None:
        if settings.smtp:
            try:
                _email_service = EmailService()
            except Exception as e:
                print(f"Failed to initialize email service: {e}")
                _email_service = None
    return _email_service
