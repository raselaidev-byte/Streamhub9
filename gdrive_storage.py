"""
Google Drive storage backend.
Uses google-auth + google-api-python-client.
Only loaded when STORAGE_BACKEND=gdrive.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Optional

from monitoring.logger import get_logger

log = get_logger(__name__)


def _get_service():
    """Lazy-load Drive service to avoid import errors when not configured."""
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    from config.settings import settings

    creds = Credentials.from_service_account_file(
        str(settings.GDRIVE_CREDENTIALS_FILE),
        scopes=["https://www.googleapis.com/auth/drive.file"],
    )
    return build("drive", "v3", credentials=creds)


def upload_to_drive(local_path: Path, filename: Optional[str] = None) -> Optional[str]:
    """
    Upload a file to Google Drive.
    Returns the shareable web link or None on failure.
    """
    from config.settings import settings
    from googleapiclient.http import MediaFileUpload

    try:
        service = _get_service()
        mime = mimetypes.guess_type(str(local_path))[0] or "video/mp4"
        fname = filename or local_path.name

        file_metadata = {
            "name": fname,
            "parents": [settings.GDRIVE_FOLDER_ID],
        }
        media = MediaFileUpload(str(local_path), mimetype=mime, resumable=True)
        result = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id,webViewLink",
        ).execute()

        link = result.get("webViewLink")
        log.info("gdrive_upload_success", file=fname, link=link)
        return link

    except Exception as e:
        log.error("gdrive_upload_error", path=str(local_path), error=str(e))
        return None
