
import json
import os
import requests
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from api.runtime_paths import get_config_path

# Load Config
CONFIG_PATH = str(get_config_path("config.json"))

def load_token():
    print(f"Reading config from: {CONFIG_PATH}")
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                config = json.load(f)
                return config.get('discord_bot_token')
        except Exception as e:
            print(f"Error reading config: {e}")
    else:
        print("Config file not found at path.")
    return None

def check_identity():
    token = load_token()
    if not token:
        print("❌ No discord_bot_token found in config.json")
        return

    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.get("https://discord.com/api/v10/users/@me", headers=headers)
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Token is Valid.")
            print(f"🤖 Bot Name: {data['username']}#{data['discriminator']}")
            print(f"🆔 Bot ID: {data['id']}")
            print(f"🔗 Invite Link (Admin): https://discord.com/api/oauth2/authorize?client_id={data['id']}&permissions=8&scope=bot")
        else:
            print(f"❌ Failed to authenticate: {response.status_code}")
            print(response.text)
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    check_identity()
