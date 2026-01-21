import os
import sys
import argparse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import json

# Define Scope
SCOPES = ['https://www.googleapis.com/auth/drive']

def authenticate_drive(sa_key_content):
    """Authenticate and return Drive Service"""
    # Create a temporary file for the SA key because from_service_account_file needs a file path
    # OR use from_service_account_info with a dict.
    
    try:
        creds_dict = json.loads(sa_key_content)
        creds = service_account.Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        service = build('drive', 'v3', credentials=creds)
        return service
    except Exception as e:
        print(f"❌ Auth Error: {e}")
        return None

def upload_file(service, file_path, folder_id):
    """Upload or Update a file in the specific folder"""
    file_name = os.path.basename(file_path)
    
    # Check if file exists to update it (optional, or just create new with timestamp?)
    # User said "Daily Backup", so maybe allow duplicates with timestamps OR overwrite 'db_seoul.db'?
    # GitHub storage overwrites 'db_seoul.db', but keeps history via Git.
    # Google Drive: Overwriting is better to save space, OR use date-based names.
    # Let's use Date-Based Name for Backup: '2026-01-21_db_seoul.db'
    
    from datetime import datetime
    date_str = datetime.now().strftime("%Y-%m-%d")
    upload_name = f"{date_str}_{file_name}"

    file_metadata = {
        'name': upload_name,
        'parents': [folder_id]
    }
    
    media = MediaFileUpload(file_path, resumable=True)
    
    try:
        print(f"📤 Uploading '{file_name}' as '{upload_name}'...")
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        print(f"✅ Upload Complete. File ID: {file.get('id')}")
        return True
    except Exception as e:
        print(f"❌ Upload Failed: {e}")
        return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", nargs="+", required=True, help="List of files to upload")
    parser.add_argument("--folder", required=True, help="Target Google Drive Folder ID")
    parser.add_argument("--key", required=True, help="Service Account JSON Content")
    
    args = parser.parse_args()
    
    service = authenticate_drive(args.key)
    if service:
        for f in args.files:
            if os.path.exists(f):
                upload_file(service, f, args.folder)
            else:
                print(f"⚠️ File not found: {f}")
