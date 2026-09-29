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
    drive_folder_id = os.getenv("DRIVE_FOLDER_ID")
    sheet_id = os.getenv("SHEET_ID")
    if not drive_folder_id or not sheet_id:
        raise RuntimeError(
            "Missing required environment variables: DRIVE_FOLDER_ID and SHEET_ID. "
            "Set them before running the server."
        )
    return drive_folder_id, sheet_id
