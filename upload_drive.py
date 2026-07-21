import os
import argparse
import json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Define Scope
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def authenticate_drive_oauth(token_json_content):
    """Authenticate using OAuth Token (User Account)"""
    try:
        creds_data = json.loads(token_json_content)
        creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
        service = build("drive", "v3", credentials=creds)
        return service
    except Exception as e:
        print(f"❌ Auth Error (OAuth): {e}")
        return None


def escape_drive_query_value(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def find_existing_file(service, folder_id, file_name):
    """Return the newest non-trashed file with this exact name in the target folder."""
    escaped_folder = escape_drive_query_value(folder_id)
    escaped_name = escape_drive_query_value(file_name)
    query = (
        f"'{escaped_folder}' in parents and "
        f"name = '{escaped_name}' and "
        "trashed = false"
    )
    result = (
        service.files()
        .list(
            q=query,
            spaces="drive",
            fields="files(id, name, modifiedTime)",
            orderBy="modifiedTime desc",
            pageSize=10,
        )
        .execute()
    )
    files = result.get("files", [])
    return files[0] if files else None


def upload_file(service, file_path, folder_id):
    """Upload or Update a file in the specific folder"""
    file_name = os.path.basename(file_path)
    from datetime import datetime, timezone, timedelta

    KST = timezone(timedelta(hours=9))
    date_str = datetime.now(KST).strftime("%Y-%m-%d")
    upload_name = f"{date_str}_{file_name}"

    file_metadata = {"name": upload_name, "parents": [folder_id]}

    media = MediaFileUpload(file_path, resumable=True)

    try:
        existing = find_existing_file(service, folder_id, upload_name)
        if existing:
            print(
                f"♻️ Updating existing Drive file '{upload_name}' "
                f"(File ID: {existing.get('id')})..."
            )
            file = (
                service.files()
                .update(
                    fileId=existing["id"],
                    media_body=media,
                    fields="id",
                )
                .execute()
            )
            print(f"✅ Update Complete. File ID: {file.get('id')}")
            return True

        print(f"📤 Uploading '{file_name}' as '{upload_name}'...")
        file = (
            service.files()
            .create(body=file_metadata, media_body=media, fields="id")
            .execute()
        )
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
    parser.add_argument(
        "--files", nargs="+", required=True, help="List of files to upload"
    )
    parser.add_argument("--folder", required=True, help="Target Google Drive Folder ID")
    parser.add_argument("--token", help="OAuth Token JSON Content")
    parser.add_argument("--token-file", help="Path to OAuth token JSON file")

    args = parser.parse_args()

    token_content = args.token
    if args.token_file:
        with open(args.token_file, "r", encoding="utf-8") as f:
            token_content = f.read()

    if not token_content:
        parser.error("one of --token or --token-file is required")

    service = authenticate_drive_oauth(token_content)

    if not service:
        raise SystemExit(1)

    all_ok = True
    for f in args.files:
        if os.path.exists(f):
            all_ok = upload_file(service, f, args.folder) and all_ok
        else:
            print(f"⚠️ File not found: {f}")
            all_ok = False

    raise SystemExit(0 if all_ok else 1)
