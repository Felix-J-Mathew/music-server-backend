import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import yt_dlp
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

app = FastAPI(title="MusicServer Backend")

if os.getenv("RENDER"):
    CREDENTIALS_PATH = "/etc/secrets/token.json"
else:
    CREDENTIALS_PATH = "token.json"

# Define Google Drive folder ID/Google Sheets ID
DRIVE_FOLDER_ID = "1qmk43cya5p_j64lrxf1Cppza8EVrIv-h" 
SHEET_ID = "1_8NNiKRldOQLEtr8iwqUCoJUm0AgrkgoXH-xO_ptDfc"


CREDENTIALS_PATH = "token.json" 

def get_google_services():
    
    scopes = [
        'https://www.googleapis.com/auth/drive.file',
        'https://www.googleapis.com/auth/spreadsheets'
    ]
    creds = Credentials.from_authorized_user_file(CREDENTIALS_PATH, scopes)
    
    drive_service = build('drive', 'v3', credentials=creds)
    sheets_service = build('sheets', 'v4', credentials=creds)
    
    return drive_service, sheets_service

class TrackQuery(BaseModel):
    query: str

def extract_audio(query: str) -> dict:
# Finds the song,downloads the song.
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': '/tmp/%(id)s.%(ext)s',
        'noplaylist': True,
        'quiet': True,
        'default_search': 'ytsearch1'
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)
            entry = info['entries'][0] if 'entries' in info else info
            
            track_id = entry['id']
            title = entry.get('title', query)
            artist = entry.get('uploader', 'Unknown Artist')
            
            local_file_path = f"/tmp/{track_id}.mp3"
            
            return {
                "success": True,
                "track_id": track_id,
                "title": title,
                "artist": artist,
                "file_path": local_file_path
            }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/health")
def health_check():

   # The Wake Ping: Flutter hits this every 10 seconds.

    return {"status": "awake", "message": "Extraction server ready"}

@app.post("/ingest")
def ingest_track(track: TrackQuery):
  
    #The Ingest Execution: Receives the search query, extracts audio, uploads to Drive, and logs to Sheets.

    query = track.query
    
    try:
        # yt-dlp extraction
        extraction_result = extract_audio(query)
        
        if not extraction_result.get("success"):
            raise HTTPException(status_code=500, detail=extraction_result.get("error"))
            
        local_file_path = extraction_result["file_path"]
        title = extraction_result["title"]
        artist = extraction_result["artist"]
        track_id = extraction_result["track_id"]
        
        # Google APIs Integration
        drive_service, sheets_service = get_google_services()
        
        # Upload to Google Drive
        file_metadata = {
            'name': f"{title}.mp3",
            'parents': [DRIVE_FOLDER_ID]
        }
        media = MediaFileUpload(local_file_path, mimetype='audio/mpeg', resumable=True)
        drive_file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        drive_file_id = drive_file.get('id')
        
        # Append to Google Sheets
        row_data = [[track_id, title, artist, drive_file_id]]
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range="Sheet1!A:D",
            valueInputOption="USER_ENTERED",
            body={"values": row_data}
        ).execute()
        
        # Clean up local storage
        if os.path.exists(local_file_path):
            os.remove(local_file_path)
        
        return {
            "status": "success",
            "message": f"Successfully extracted and uploaded '{title}'",
            "drive_file_id": drive_file_id
        }
    except Exception as e:
        if 'local_file_path' in locals() and os.path.exists(local_file_path):
            os.remove(local_file_path)
        raise HTTPException(status_code=500, detail=str(e))