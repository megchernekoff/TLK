from __future__ import print_function
import os.path
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Read-only Gmail access
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def main():
    creds = None

    # If we already authenticated before, load the saved token
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    else:
        # Otherwise, start an OAuth browser flow
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
        creds = flow.run_local_server(port=0)

        # Save the token for future runs
        with open("token.json", "w") as token:
            token.write(creds.to_json())

    # Test Gmail connection by listing labels
    service = build("gmail", "v1", credentials=creds)
    results = service.users().labels().list(userId="me").execute()
    labels = results.get("labels", [])
    print("Your Gmail labels:")
    for label in labels:
        print(" -", label["name"])

if __name__ == "__main__":
    main()