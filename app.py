import os
import hashlib
import requests
from fastapi import FastAPI, HTTPException, Depends, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from typing import List, Optional
from google_client import get_google_services, get_config
from googleapiclient.http import MediaFileUpload


# --- App Setup ---
app = FastAPI(title="MusicServer Backend")

# --- Authentication ---
API_KEY = os.getenv("API_KEY") or os.getenv("API_KAEY") or os.getenv("api_key")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(api_key: Optional[str] = Security(api_key_header)):
    """Verify the API key if one is configured on the server."""
    if API_KEY and api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return api_key


# --- Request Models ---
class TrackQuery(BaseModel):
    query: str

class PlaylistRequest(BaseModel):
    name: str
    track_ids: List[str]

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


# --- Core: Audio Extraction ---
def extract_audio(query: str) -> dict:
    """Extract audio from a URL or search query via the Cobalt API."""
    wrapper_api_url = "https://api.cobalt.tools/api/json"

    # Cobalt only accepts direct video URLs, not search pages.
    if not query.startswith(("http://", "https://")):
        return {
            "success": False,
            "error": (
                "Please provide a direct YouTube URL. "
                "Search-by-keyword is not supported by the extraction backend."
            )
        }

    payload = {
        "url": query,
        "audioFormat": "mp3",
        "isAudioOnly": True
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    temp_file_path = None

    try:
        response = requests.post(
            wrapper_api_url,
            json=payload,
            headers=headers,
            timeout=30
        )

        try:
            res_data = response.json()
        except ValueError:
            return {
                "success": False,
                "error": f"Cobalt returned non-JSON response (HTTP {response.status_code})"
            }

        if response.status_code != 200:
            return {
                "success": False,
                "error": f"Cobalt extraction failed (HTTP {response.status_code}): {res_data}"
            }

        audio_download_url = res_data.get("url")

        if not audio_download_url and res_data.get("tunnel"):
            tunnel = res_data["tunnel"]
            audio_download_url = tunnel[0] if isinstance(tunnel, list) else tunnel

        if not audio_download_url:
            return {
                "success": False,
                "error": f"Cobalt did not return a downloadable audio URL: {res_data}"
            }

        # Deterministic track ID using SHA-256 (stable across restarts)
        track_id = (
            str(res_data.get("id"))
            if res_data.get("id")
            else hashlib.sha256(query.encode()).hexdigest()[:16]
        )

        title = res_data.get("title") or query
        artist = res_data.get("artist") or res_data.get("uploader") or "Unknown Artist"

        temp_file_path = f"/tmp/{track_id}.mp3"

        # Stream audio download to avoid OOM on large files
        with requests.get(audio_download_url, stream=True, timeout=120) as audio_res:
            audio_res.raise_for_status()
            with open(temp_file_path, "wb") as f:
                for chunk in audio_res.iter_content(chunk_size=8192):
                    f.write(chunk)

        return {
            "success": True,
            "track_id": track_id,
            "title": title,
            "artist": artist,
            "file_path": temp_file_path
        }

    except requests.RequestException as e:
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        return {"success": False, "error": f"Media download request failed: {str(e)}"}

    except Exception as e:
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)
        return {"success": False, "error": str(e)}


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
            range="Playlists!A:B"
        ).execute()

        rows = result.get('values', [])
        playlists = []

        for row in rows:
            if len(row) < 1:
                continue
            name = row[0]
            track_string = row[1] if len(row) > 1 else ""
            track_ids = [tid for tid in track_string.split(',') if tid] if track_string else []
            playlists.append({"name": name, "track_ids": track_ids})

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
                spreadsheetId=sheet_id,
                range=f"Playlists!A{row_index}:B{row_index}",
                valueInputOption="RAW",
                body=body
            ).execute()
        else:
            sheets_service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range="Playlists!A:B",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body=body
            ).execute()

        return {"status": "success", "message": f"Playlist '{playlist.name}' synced."}
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