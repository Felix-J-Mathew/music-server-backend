import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def authenticate():
    """Run the Google OAuth 2.0 Installed App flow to generate token.json.
    
    Prerequisites:
        - client_secret.json must exist in the same directory as this script.
        - A web browser must be available for the consent screen.
    
    Output:
        - Writes token.json with access and refresh tokens.
    """
    scopes = [
        'https://www.googleapis.com/auth/drive.file',
        'https://www.googleapis.com/auth/spreadsheets'
    ]

    secret_path = os.path.join(BASE_DIR, 'client_secret.json')
    token_path = os.path.join(BASE_DIR, 'token.json')

    # Pre-flight check
    if not os.path.exists(secret_path):
        print("Error: 'client_secret.json' not found.")
        print("Download it from Google Cloud Console:")
        print("  https://console.cloud.google.com/apis/credentials")
        print(f"Expected location: {secret_path}")
        sys.exit(1)

    # Run OAuth flow with explicit offline access to guarantee a refresh token
    flow = InstalledAppFlow.from_client_secrets_file(secret_path, scopes)
    creds = flow.run_local_server(port=0, access_type='offline', prompt='consent')

    # Write token.json with strict file permissions (owner read/write only)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    mode = 0o600
    fd = os.open(token_path, flags, mode)
    try:
        with os.fdopen(fd, 'w') as token_file:
            token_file.write(creds.to_json())
    except Exception:
        os.close(fd)
        raise

    print(f"Success! token.json has been generated at: {token_path}")
    print("Permissions set to 0600 (owner read/write only).")

if __name__ == '__main__':
    authenticate()