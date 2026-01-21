import requests
import argparse
import sys
import os

def send_discord_message(webhook_url, message):
    if not webhook_url:
        print("⚠️ No Webhook URL provided.")
        return

    payload = {
        "content": message
    }
    
    try:
        response = requests.post(webhook_url, json=payload)
        response.raise_for_status()
        print("✅ Discord Notification Sent!")
    except Exception as e:
        print(f"❌ Failed to send Discord notification: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", required=True, help="Message to send")
    parser.add_argument("--webhook", help="Discord Webhook URL")
    
    args = parser.parse_args()
    
    # Priority: DB Argument > Environment Variable
    webhook = args.webhook or os.environ.get("DISCORD_WEBHOOK_URL")
    
    send_discord_message(webhook, args.message)
