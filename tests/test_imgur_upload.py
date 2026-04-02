import requests
import os

# Test Configuration
# Try a different public Client ID often used in examples if the previous one failed
# Or use the one from the bridge to test
CLIENT_ID = "d3101526084ac92"
IMGUR_API_URL = "https://api.imgur.com/3/image"

def test_upload():
    # Create a dummy image
    from PIL import Image
    img = Image.new('RGB', (100, 100), color = 'red')
    img.save('test_image.png')
    
    print(f"Testing upload with Client ID: {CLIENT_ID}")
    
    headers = {
        "Authorization": f"Client-ID {CLIENT_ID}"
    }
    
    with open('test_image.png', "rb") as file:
        files = {"image": file}
        try:
            response = requests.post(IMGUR_API_URL, headers=headers, files=files, timeout=30)
            
            print(f"Status Code: {response.status_code}")
            print(f"Response Body: {response.text}")
            
        except Exception as e:
            print(f"Exception: {e}")

if __name__ == "__main__":
    test_upload()
