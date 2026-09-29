import os
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = [
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/spreadsheets'
]

def get_credentials_path():
    """Resolve the credentials file path based on the environment."""
    candidates = [
        "/etc/secrets/token.json",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json"),
        os.path.join(os.getcwd(), "token.json"),
        "token.json",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    if os.getenv("RENDER"):
        return "/etc/secrets/token.json"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json")

def get_google_services(credentials_path=None):
    """Initialize and return Google Drive and Sheets API service clients.
    
    Args:
        credentials_path: Optional explicit path to token.json. 
                         If None, auto-resolved via get_credentials_path().
    
    Returns:
        Tuple of (drive_service, sheets_service)
    """
    path = credentials_path or get_credentials_path()
    creds = Credentials.from_authorized_user_file(path, SCOPES)
    drive_service = build('drive', 'v3', credentials=creds)
    sheets_service = build('sheets', 'v4', credentials=creds)
    return drive_service, sheets_service

def get_config():
    """Return Drive folder ID and Sheet ID from environment variables."""
    drive_folder_id = (
        os.getenv("DRIVE_FOLDER_ID")
        or os.getenv("Drive_Folder_ID")
        or os.getenv("drive_folder_id")
        or "1qmk43cya5p_j64lrxf1Cppza8EVrIv-h"
    )
    sheet_id = (
        os.getenv("SHEET_ID")
        or os.getenv("Sheet_ID")
        or os.getenv("sheet_id")
        or "1_8NNiKRldOQLEtr8iwqUCoJUm0AgrkgoXH-xO_ptDfc"
    )
    return drive_folder_id, sheet_id
