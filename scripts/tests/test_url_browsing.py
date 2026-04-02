import sys
import os
import logging

# Configure logging to see Orchestrator output
logging.basicConfig(level=logging.INFO)

# Add project root
sys.path.append(os.getcwd())

from api.orchestrator import Orchestrator

def test_url_browsing():
    orc = Orchestrator()
    # Test Wikipedia link
    url = "https://zh.wikipedia.org/zh-tw/東方三博士"
    print(f"🚀 Testing URL: {url}")
    
    # Simulate a Discord message
    response = orc.process_message("test_user", url, platform="TEST", role="user")
    
    print("\n📝 Response:")
    print(response)
    
    if "東方三博士" in response and "來源" in response:
        print("\n✅ Test Passed!")
    else:
        print("\n❌ Test Failed!")

if __name__ == "__main__":
    test_url_browsing()
