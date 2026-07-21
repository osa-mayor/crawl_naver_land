import requests
import argparse
import sys
import os

def send_discord_message(webhook_url, message):
    if not webhook_url:
        print("⚠️ No Webhook URL provided.")
        return False

    payload = {
        "content": message
    }
    
    try:
        response = requests.post(webhook_url, json=payload, timeout=30)
        response.raise_for_status()
        print("✅ Discord Notification Sent!")
        return True
    except Exception as e:
        print(f"❌ Failed to send Discord notification: {e}")
        return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", required=True, help="Message to send")
    parser.add_argument("--webhook", help="Discord Webhook URL")
    
    args = parser.parse_args()
    
    # Priority: DB Argument > Environment Variable
    webhook = args.webhook or os.environ.get("DISCORD_WEBHOOK_URL")
    
    raise SystemExit(0 if send_discord_message(webhook, args.message) else 1)
