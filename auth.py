from google_auth_oauthlib.flow import InstalledAppFlow

def authenticate():
    scopes = [
        'https://www.googleapis.com/auth/drive.file',
        'https://www.googleapis.com/auth/spreadsheets'
    ]
    # Reads the secret and opens your web browser to log in
    flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', scopes)
    creds = flow.run_local_server(port=0)
    
    # Saves your permanent session key
    with open('token.json', 'w') as token:
        token.write(creds.to_json())
    print("Success! token.json has been generated.")

if __name__ == '__main__':
    authenticate()