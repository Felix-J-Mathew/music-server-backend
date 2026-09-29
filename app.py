import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import yt_dlp
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from typing import List

class PlaylistRequest(BaseModel):
    name: str
    track_ids: List[str]

class RemoveTrackRequest(BaseModel):
    playlist_name: str
    track_id: str

class DeletePlaylistRequest(BaseModel):
    playlist_name: str

app = FastAPI(title="MusicServer Backend")

if os.getenv("RENDER"):
    CREDENTIALS_PATH = "/etc/secrets/token.json"
else:
    CREDENTIALS_PATH = "token.json"

# Strict security: No fallbacks. Must be provided via environment variables.
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")
SHEET_ID = os.getenv("SHEET_ID")

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
    ydl_opts = {
        'format': 'bestaudio/best',
        'cookiefile': 'cookies.txt',
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
    return {"status": "awake", "message": "Extraction server ready"}

@app.post("/ingest")
def ingest_track(track: TrackQuery):
    query = track.query
    try:
        extraction_result = extract_audio(query)
        if not extraction_result.get("success"):
            raise HTTPException(status_code=500, detail=extraction_result.get("error"))
            
        local_file_path = extraction_result["file_path"]
        title = extraction_result["title"]
        artist = extraction_result["artist"]
        track_id = extraction_result["track_id"]
        
        drive_service, sheets_service = get_google_services()
        
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
        
        row_data = [[track_id, title, artist, drive_file_id]]
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range="Sheet1!A:D",
            valueInputOption="USER_ENTERED",
            body={"values": row_data}
        ).execute()
        
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

@app.get("/songs")
def get_library():
    try:
        _, sheets_service = get_google_services()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range="Sheet1!A:D"
        ).execute()
        
        rows = result.get('values', [])
        if not rows:
            return {"status": "success", "data": []}
            
        library = []
        for row in rows:
            if len(row) < 4:
                continue
            track_id, title, artist, drive_id = row
            stream_url = f"https://drive.google.com/uc?export=download&id={drive_id}"
            library.append({
                "track_id": track_id,
                "title": title,
                "artist": artist,
                "stream_url": stream_url
            })
            
        return {"status": "success", "total_tracks": len(library), "data": library}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.delete("/songs/{track_id}")
def delete_song(track_id: str):
    try:
        _, sheets_service = get_google_services()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range="Sheet1!A:D"
        ).execute()
        rows = result.get('values', [])
        
        row_index = -1
        for i, row in enumerate(rows):
            if row and row[0] == track_id:
                row_index = i + 1
                break
                
        if row_index == -1:
            raise HTTPException(status_code=404, detail="Song not found")
            
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=SHEET_ID,
            range=f"Sheet1!A{row_index}:D{row_index}"
        ).execute()
        
        return {"status": "success", "message": "Song deleted from library"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/playlists")
def get_playlists():
    try:
        _, sheets_service = get_google_services()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range="Playlists!A:B"
        ).execute()
        
        rows = result.get('values', [])
        playlists = []
        
        for row in rows:
            if len(row) < 1:
                continue
            name = row[0]
            track_string = row[1] if len(row) > 1 else ""
            track_ids = track_string.split(',') if track_string else []
            playlists.append({"name": name, "track_ids": track_ids})
            
        return {"status": "success", "data": playlists}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/playlists")
def save_playlist(playlist: PlaylistRequest):
    try:
        _, sheets_service = get_google_services()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range="Playlists!A:B"
        ).execute()
        
        rows = result.get('values', [])
        row_index = -1
        
        for i, row in enumerate(rows):
            if row and row[0] == playlist.name:
                row_index = i + 1
                break
        
        track_string = ",".join(playlist.track_ids)
        body = {"values": [[playlist.name, track_string]]}
        
        if row_index != -1:
            sheets_service.spreadsheets().values().update(
                spreadsheetId=SHEET_ID,
                range=f"Playlists!A{row_index}:B{row_index}",
                valueInputOption="RAW",
                body=body
            ).execute()
        else:
            sheets_service.spreadsheets().values().append(
                spreadsheetId=SHEET_ID,
                range="Playlists!A:B",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body=body
            ).execute()
            
        return {"status": "success", "message": f"Playlist '{playlist.name}' synced."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/playlists/remove")
def remove_track_from_playlist(req: RemoveTrackRequest):
    try:
        _, sheets_service = get_google_services()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range="Playlists!A:B"
        ).execute()
        rows = result.get('values', [])
        
        row_index = -1
        track_ids = []
        for i, row in enumerate(rows):
            if row and row[0] == req.playlist_name:
                row_index = i + 1
                track_ids = row[1].split(',') if len(row) > 1 and row[1] else []
                break
                
        if row_index == -1:
            raise HTTPException(status_code=404, detail="Playlist not found")
            
        if req.track_id in track_ids:
            track_ids.remove(req.track_id)
            
        track_string = ",".join(track_ids)
        body = {"values": [[req.playlist_name, track_string]]}
        
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SHEET_ID,
            range=f"Playlists!A{row_index}:B{row_index}",
            valueInputOption="RAW",
            body=body
        ).execute()
        
        return {"status": "success", "message": "Track removed from playlist"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/playlists/{playlist_name}")
def delete_playlist(playlist_name: str):
    try:
        _, sheets_service = get_google_services()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range="Playlists!A:B"
        ).execute()
        rows = result.get('values', [])
        
        row_index = -1
        for i, row in enumerate(rows):
            if row and row[0] == playlist_name:
                row_index = i + 1
                break
                
        if row_index == -1:
            raise HTTPException(status_code=404, detail="Playlist not found")
            
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=SHEET_ID,
            range=f"Playlists!A{row_index}:B{row_index}"
        ).execute()
        
        return {"status": "success", "message": "Playlist deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))