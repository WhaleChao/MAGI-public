
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from skills.bridge import melchior_client

def test_small_review():
    print("🚀 Testing Melchior with small payload...")
    status = melchior_client.check_health()
    print(f"📡 Status: {status}")
    
    if not status.get('online'):
        print("❌ Offline")
        return

    code = "def hello(): print('Hello World')"
    prompt = f"Review this code: {code}"
    
    print(f"📤 Sending prompt: {prompt}")
    try:
        response = melchior_client.chat(prompt)
        print(f"📥 Response keys: {response.keys()}")
        print(f"📄 Full Response: {response}")
        print(f"📝 Response content: {response.get('response')}")
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    test_small_review()
