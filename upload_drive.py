import os
import argparse
import json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Define Scope
SCOPES = ['https://www.googleapis.com/auth/drive.file']

def authenticate_drive_oauth(token_json_content):
    """Authenticate using OAuth Token (User Account)"""
    try:
        creds_data = json.loads(token_json_content)
        creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
        service = build('drive', 'v3', credentials=creds)
        return service
    except Exception as e:
        print(f"❌ Auth Error (OAuth): {e}")
        return None

def upload_file(service, file_path, folder_id):
    """Upload or Update a file in the specific folder"""
    file_name = os.path.basename(file_path)
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
        # Print detailed error for debugging quota issues
        if "storageQuotaExceeded" in str(e):
            print("🚨 쿼터 초과! 구글 드라이브 용량이 꽉 찼습니다.")
        return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", nargs="+", required=True, help="List of files to upload")
    parser.add_argument("--folder", required=True, help="Target Google Drive Folder ID")
    # Changed: Accepts 'token' instead of 'key'
    parser.add_argument("--token", required=True, help="OAuth Token JSON Content")
    
    args = parser.parse_args()
    
    service = authenticate_drive_oauth(args.token)
    
    if service:
        for f in args.files:
            if os.path.exists(f):
                upload_file(service, f, args.folder)
            else:
                print(f"⚠️ File not found: {f}")
