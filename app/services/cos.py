import uuid
import io
from typing import BinaryIO

from qcloud_cos import CosConfig, CosServiceError
from qcloud_cos.cos_threadpool import SimpleThreadPool

from app.config import settings


class COSService:
    """Tencent Cloud COS service for file uploads"""

    def __init__(self):
        if not settings.cos:
            raise RuntimeError("COS not configured")

        self.config = CosConfig(
            SecretId=settings.cos.secret_id,
            SecretKey=settings.cos.secret_key,
            Region=settings.cos.region,
        )
        self.bucket = settings.cos.bucket
        self.base_url = settings.cos.base_url

    def upload_file(
        self,
        file_data: BinaryIO | bytes,
        filename: str,
        folder: str = "avatars",
    ) -> str:
        """
        Upload file to COS and return the URL

        Args:
            file_data: File content (file object or bytes)
            filename: Original filename
            folder: COS folder path

        Returns:
            Public URL of the uploaded file
        """
        # Generate unique filename
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "jpg"
        key = f"{folder}/{uuid.uuid4()}.{ext}"

        # Handle both bytes and file-like objects
        if isinstance(file_data, bytes):
            file_data = io.BytesIO(file_data)

        from qcloud_cos import CosS3Client
        client = CosS3Client(self.config)

        client.put_object(
            Bucket=self.bucket,
            Body=file_data,
            Key=key,
            EnableMD5=False,
            ContentType=self._get_content_type(ext),
        )

        return f"{self.base_url}/{key}"

    def delete_file(self, url: str) -> bool:
        """Delete file from COS by URL"""
        key = url.replace(f"{self.base_url}/", "")

        from qcloud_cos import CosS3Client
        client = CosS3Client(self.config)

        try:
            client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except CosServiceError:
            return False

    def get_presigned_url(self, key: str, expires: int = 3600) -> str:
        """Generate a presigned URL for private bucket access"""
        from qcloud_cos import CosS3Client
        client = CosS3Client(self.config)

        presigned_url = client.get_presigned_url(
            Bucket=self.bucket,
            Key=key,
            Expired=expires,
        )
        return presigned_url

    def _get_content_type(self, ext: str) -> str:
        """Get content type by file extension"""
        content_types = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "gif": "image/gif",
            "webp": "image/webp",
            "svg": "image/svg+xml",
        }
        return content_types.get(ext.lower(), "application/octet-stream")


# Singleton instance
_cos_service: COSService | None = None


def get_cos_service() -> COSService:
    global _cos_service
    if _cos_service is None:
        if settings.cos:
            _cos_service = COSService()
    return _cos_service
