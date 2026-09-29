import os
import re
import hashlib
import mimetypes
import requests
from fastapi import FastAPI, HTTPException, Depends, Security, UploadFile, File, Form
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from typing import List, Optional
from google_client import get_google_services, get_config
from googleapiclient.http import MediaFileUpload


# --- App Setup ---
app = FastAPI(title="MusicServer Backend")

# --- Authentication ---
API_KEY = os.getenv("API_KEY") or os.getenv("API_KAEY") or os.getenv("api_key")
if API_KEY:
    API_KEY = API_KEY.strip().strip('"').strip("'")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(api_key: Optional[str] = Security(api_key_header)):
    """Verify the API key if one is configured on the server."""
    if API_KEY:
        cleaned_incoming = (api_key or "").strip().strip('"').strip("'")
        if cleaned_incoming != API_KEY:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return api_key


# --- Request Models ---
class TrackQuery(BaseModel):
    query: str

class PlaylistRequest(BaseModel):
    name: str
    track_ids: List[str]
    cover_url: Optional[str] = None

class RenamePlaylistRequest(BaseModel):
    old_name: str
    new_name: str

class PlaylistCoverRequest(BaseModel):
    playlist_name: str
    cover_url: str

class RemoveTrackRequest(BaseModel):
    playlist_name: str
    track_id: str


# --- Helper: Resolve Sheet Tab ID ---
def _get_sheet_gid(sheets_service, spreadsheet_id: str, tab_name: str) -> int:
    """Resolve the numeric sheet (tab) GID for batchUpdate operations."""
    spreadsheet = sheets_service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties"
    ).execute()
    for sheet in spreadsheet.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == tab_name:
            return props.get("sheetId", 0)
    return 0  # Default to first sheet


def _delete_sheet_row(sheets_service, spreadsheet_id: str, tab_name: str, row_index: int):
    """Delete an entire row (1-indexed) from a sheet tab using batchUpdate."""
    sheet_gid = _get_sheet_gid(sheets_service, spreadsheet_id, tab_name)
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [{
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet_gid,
                        "dimension": "ROWS",
                        "startIndex": row_index - 1,  # 0-indexed
                        "endIndex": row_index
                    }
                }
            }]
        }
    ).execute()


import yt_dlp
import base64
import re


def _prepare_cookie_file() -> Optional[str]:
    """Prepare, auto-repair, and validate a Netscape format cookie file."""
    target_path = "/tmp/render_cookies.txt"
    env_cookies = os.getenv("YOUTUBE_COOKIES")
    if env_cookies:
        content = env_cookies.strip().strip('"').strip("'")
        # Try base64 decoding first
        try:
            decoded = base64.b64decode(content).decode('utf-8', errors='ignore')
            if 'youtube.com' in decoded:
                content = decoded
        except Exception:
            pass

        lines = content.splitlines()
        cleaned_lines = ['# Netscape HTTP Cookie File']
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith('#'):
                if not line.startswith('# Netscape'):
                    cleaned_lines.append(line)
                continue
            # If line is space-separated instead of tab-separated, convert to tabs
            if '\t' not in line:
                parts = re.split(r'\s+', line)
                if len(parts) >= 7:
                    line = '\t'.join(parts[:7])
            cleaned_lines.append(line)

        # Only write if we have actual cookie records
        if len(cleaned_lines) > 1:
            final_text = '\n'.join(cleaned_lines) + '\n'
            with open(target_path, 'w', encoding='utf-8') as f:
                f.write(final_text)
            return target_path

    cookie_candidates = [
        "/etc/secrets/cookies.txt",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt"),
        os.path.join(os.getcwd(), "cookies.txt"),
    ]
    for path in cookie_candidates:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            try:
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    first_line = f.readline()
                if '# Netscape' in first_line:
                    return path
            except Exception:
                pass
    return None


# --- Core: Audio Extraction ---
def extract_audio(query: str) -> dict:
    """Extract audio from a direct YouTube URL or search query using yt-dlp."""
    cookie_path = _prepare_cookie_file()

    def _build_ydl_opts(use_cookies: bool = True) -> dict:
        opts = {
            'format': 'best/bestaudio/ba/18',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192'
            }],
            'outtmpl': '/tmp/%(id)s.%(ext)s',
            'noplaylist': True,
            'quiet': True,
            'default_search': 'ytsearch1',
            'extractor_args': {
                'youtube': {
                    'player_client': ['android']
                }
            }
        }
        if use_cookies and cookie_path and os.path.exists(cookie_path):
            opts['cookiefile'] = cookie_path
        return opts

    try:
        # Try download (with cookie if available; if cookie causes ANY error, retry without)
        try:
            with yt_dlp.YoutubeDL(_build_ydl_opts(use_cookies=True)) as ydl:
                info = ydl.extract_info(query, download=True)
        except Exception as e:
            if cookie_path:
                with yt_dlp.YoutubeDL(_build_ydl_opts(use_cookies=False)) as ydl:
                    info = ydl.extract_info(query, download=True)
            else:
                raise

        entry = info['entries'][0] if 'entries' in info and info['entries'] else info
        track_id = entry.get('id')
        if not track_id:
            track_id = hashlib.sha256(query.encode()).hexdigest()[:16]
        title = entry.get('title') or query
        artist = entry.get('uploader') or entry.get('channel') or "Unknown Artist"
        mp3_path = f"/tmp/{track_id}.mp3"

        # Clean up intermediate files
        for ext in ['webm', 'm4a', 'opus', 'ogg', 'wav', 'part']:
            inter = f"/tmp/{track_id}.{ext}"
            if os.path.exists(inter):
                try:
                    os.remove(inter)
                except OSError:
                    pass

        if not os.path.exists(mp3_path):
            return {"success": False, "error": f"Extracted MP3 file not found at {mp3_path}"}

        return {
            "success": True,
            "track_id": track_id,
            "title": title,
            "artist": artist,
            "file_path": mp3_path
        }
    except Exception as e:
        return {"success": False, "error": f"Audio extraction failed: {str(e)}"}


# --- Routes ---

@app.get("/health")
def health_check():
    return {"status": "awake", "message": "Extraction server ready"}


@app.post("/ingest")
def ingest_track(track: TrackQuery, _key: str = Depends(verify_api_key)):
    query = track.query
    local_file_path = None
    try:
        extraction_result = extract_audio(query)
        if not extraction_result.get("success"):
            raise HTTPException(status_code=500, detail=extraction_result.get("error"))

        local_file_path = extraction_result["file_path"]
        title = extraction_result["title"]
        artist = extraction_result["artist"]
        track_id = extraction_result["track_id"]

        drive_service, sheets_service = get_google_services()
        drive_folder_id, sheet_id = get_config()

        file_metadata = {
            'name': f"{title}.mp3",
            'parents': [drive_folder_id]
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
            spreadsheetId=sheet_id,
            range="Sheet1!A:D",
            valueInputOption="USER_ENTERED",
            body={"values": row_data}
        ).execute()

        return {
            "status": "success",
            "message": f"Successfully extracted and uploaded '{title}'",
            "drive_file_id": drive_file_id
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if local_file_path and os.path.exists(local_file_path):
            os.remove(local_file_path)


@app.post("/upload")
async def upload_track(
    file: UploadFile = File(...),
    title: str = Form(...),
    artist: str = Form("Unknown Artist"),
    track_id: Optional[str] = Form(None),
    _key: str = Depends(verify_api_key)
):
    """Client-assisted ingestion endpoint: accepts direct audio upload from Flutter client."""
    if not track_id:
        track_id = hashlib.sha256(f"{title}_{artist}".encode()).hexdigest()[:16]

    orig_ext = os.path.splitext(file.filename or "")[1].lower()
    ext = orig_ext if orig_ext in [".mp3", ".m4a", ".aac", ".ogg", ".opus", ".webm", ".wav"] else ".mp3"
    mimetype = file.content_type or (
        "audio/mp4" if ext == ".m4a" else
        "audio/webm" if ext == ".webm" else
        "audio/ogg" if ext in [".ogg", ".opus"] else
        "audio/mpeg"
    )

    local_file_path = f"/tmp/{track_id}{ext}"
    try:
        with open(local_file_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)

        drive_service, sheets_service = get_google_services()
        drive_folder_id, sheet_id = get_config()

        clean_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or "Track"
        file_metadata = {
            'name': f"{clean_title}{ext}",
            'parents': [drive_folder_id]
        }
        media = MediaFileUpload(local_file_path, mimetype=mimetype, resumable=True)
        drive_file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()

        drive_file_id = drive_file.get('id')

        row_data = [[track_id, title, artist, drive_file_id]]
        sheets_service.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range="Sheet1!A:D",
            valueInputOption="USER_ENTERED",
            body={"values": row_data}
        ).execute()

        return {
            "status": "success",
            "message": f"Successfully uploaded '{title}'",
            "drive_file_id": drive_file_id,
            "track_id": track_id
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(local_file_path):
            try:
                os.remove(local_file_path)
            except OSError:
                pass


@app.get("/songs")
def get_library(_key: str = Depends(verify_api_key)):
    try:
        _, sheets_service = get_google_services()
        _, sheet_id = get_config()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Sheet1!A:D"
        ).execute()

        rows = result.get('values', [])
        if not rows:
            return {"status": "success", "total_tracks": 0, "data": []}

        library = []
        for row in rows:
            if len(row) < 4:
                continue
            track_id, title, artist, drive_id = row[:4]
            stream_url = f"https://drive.google.com/uc?export=download&id={drive_id}"
            library.append({
                "track_id": track_id,
                "title": title,
                "artist": artist,
                "stream_url": stream_url
            })

        return {"status": "success", "total_tracks": len(library), "data": library}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/songs/{track_id}")
def delete_song(track_id: str, _key: str = Depends(verify_api_key)):
    try:
        drive_service, sheets_service = get_google_services()
        _, sheet_id = get_config()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Sheet1!A:D"
        ).execute()
        rows = result.get('values', [])

        row_index = -1
        drive_file_id = None
        for i, row in enumerate(rows):
            if row and row[0] == track_id:
                row_index = i + 1  # 1-indexed for Sheets API
                drive_file_id = row[3] if len(row) > 3 else None
                break

        if row_index == -1:
            raise HTTPException(status_code=404, detail="Song not found")

        # Delete the row from Sheets (proper deletion, not just clearing)
        _delete_sheet_row(sheets_service, sheet_id, "Sheet1", row_index)

        # Also delete the file from Google Drive to prevent storage leaks
        if drive_file_id:
            try:
                drive_service.files().delete(fileId=drive_file_id).execute()
            except Exception:
                pass  # Drive file may already be deleted; don't fail the request

        return {"status": "success", "message": "Song deleted from library and Drive"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/playlists")
def get_playlists(_key: str = Depends(verify_api_key)):
    try:
        _, sheets_service = get_google_services()
        _, sheet_id = get_config()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Playlists!A:C"
        ).execute()

        rows = result.get('values', [])
        playlists = []

        for row in rows:
            if len(row) < 1:
                continue
            name = row[0]
            track_string = row[1] if len(row) > 1 else ""
            cover_url = row[2] if len(row) > 2 else ""
            track_ids = [tid for tid in track_string.split(',') if tid] if track_string else []
            playlists.append({"name": name, "track_ids": track_ids, "cover_url": cover_url})

        return {"status": "success", "data": playlists}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/playlists")
def save_playlist(playlist: PlaylistRequest, _key: str = Depends(verify_api_key)):
    try:
        _, sheets_service = get_google_services()
        _, sheet_id = get_config()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Playlists!A:C"
        ).execute()

        rows = result.get('values', [])
        row_index = -1
        existing_cover = ""

        for i, row in enumerate(rows):
            if row and row[0] == playlist.name:
                row_index = i + 1
                if len(row) > 2:
                    existing_cover = row[2]
                break

        track_string = ",".join(playlist.track_ids)
        cover_val = playlist.cover_url if playlist.cover_url is not None else existing_cover
        body = {"values": [[playlist.name, track_string, cover_val]]}

        if row_index != -1:
            sheets_service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"Playlists!A{row_index}:C{row_index}",
                valueInputOption="RAW",
                body=body
            ).execute()
        else:
            sheets_service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range="Playlists!A:C",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body=body
            ).execute()

        return {"status": "success", "message": f"Playlist '{playlist.name}' synced."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/playlists/rename")
def rename_playlist(req: RenamePlaylistRequest, _key: str = Depends(verify_api_key)):
    try:
        _, sheets_service = get_google_services()
        _, sheet_id = get_config()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Playlists!A:C"
        ).execute()
        rows = result.get('values', [])
        row_index = -1
        for i, row in enumerate(rows):
            if row and row[0] == req.old_name:
                row_index = i + 1
                break
        if row_index == -1:
            raise HTTPException(status_code=404, detail="Playlist not found")

        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"Playlists!A{row_index}",
            valueInputOption="RAW",
            body={"values": [[req.new_name]]}
        ).execute()
        return {"status": "success", "message": f"Playlist renamed to '{req.new_name}'"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/playlists/cover")
def set_playlist_cover(req: PlaylistCoverRequest, _key: str = Depends(verify_api_key)):
    try:
        _, sheets_service = get_google_services()
        _, sheet_id = get_config()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Playlists!A:C"
        ).execute()
        rows = result.get('values', [])
        row_index = -1
        for i, row in enumerate(rows):
            if row and row[0] == req.playlist_name:
                row_index = i + 1
                break
        if row_index == -1:
            raise HTTPException(status_code=404, detail="Playlist not found")

        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"Playlists!C{row_index}",
            valueInputOption="RAW",
            body={"values": [[req.cover_url]]}
        ).execute()
        return {"status": "success", "message": f"Playlist cover updated for '{req.playlist_name}'"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/playlists/remove")
def remove_track_from_playlist(req: RemoveTrackRequest, _key: str = Depends(verify_api_key)):
    try:
        _, sheets_service = get_google_services()
        _, sheet_id = get_config()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range="Playlists!A:B"
        ).execute()
        rows = result.get('values', [])

        row_index = -1
        track_ids = []
        for i, row in enumerate(rows):
            if row and row[0] == req.playlist_name:
                row_index = i + 1
                track_ids = [tid for tid in row[1].split(',') if tid] if len(row) > 1 and row[1] else []
                break

        if row_index == -1:
            raise HTTPException(status_code=404, detail="Playlist not found")

        if req.track_id in track_ids:
            track_ids.remove(req.track_id)

        track_string = ",".join(track_ids)
        body = {"values": [[req.playlist_name, track_string]]}

        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"Playlists!A{row_index}:B{row_index}",
            valueInputOption="RAW",
            body=body
        ).execute()

        return {"status": "success", "message": "Track removed from playlist"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/playlists/{playlist_name}")
def delete_playlist(playlist_name: str, _key: str = Depends(verify_api_key)):
    try:
        _, sheets_service = get_google_services()
        _, sheet_id = get_config()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
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

        # Proper row deletion instead of clearing cells
        _delete_sheet_row(sheets_service, sheet_id, "Playlists", row_index)

        return {"status": "success", "message": "Playlist deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))