import os
import shutil
import tempfile
import requests
from unittest.mock import MagicMock, patch

# Mock Logger
class Logger:
    def info(self, msg): print(f"[INFO] {msg}")
    def error(self, msg): print(f"[ERROR] {msg}")

logger = Logger()

def test_sync_skills():
    print("=== Testing Skill Sync Logic (Simulation) ===")
    
    # 1. Create Dummy Skills Dir
    temp_dir = tempfile.mkdtemp()
    skills_dir = os.path.join(temp_dir, "skills")
    os.makedirs(skills_dir)
    with open(os.path.join(skills_dir, "dummy_skill.md"), 'w') as f:
        f.write("# Dummy Skill")
        
    print(f"Created dummy skills at {skills_dir}")
    
    # 2. Simulate Zip Creation (Logic from night_talk.py)
    zip_path_base = os.path.join(temp_dir, "magi_skills_sync")
    shutil.make_archive(zip_path_base, 'zip', skills_dir)
    final_zip = zip_path_base + ".zip"
    
    if os.path.exists(final_zip):
        print(f"✅ Zip created successfully: {final_zip}")
        print(f"Size: {os.path.getsize(final_zip)} bytes")
    else:
        print("❌ Zip creation failed")
        return

    # 3. Simulate API Call (Mocked)
    with patch('requests.post') as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "message": "Simulated Sync"}
        mock_post.return_value = mock_response
        
        # Call Melchior Bridge Logic
        print("Simulating Melchior Bridge Call...")
        print(f"File to send: {os.path.basename(final_zip)}")
        
        # Mock logic
        files = {'file': (os.path.basename(final_zip), open(final_zip, 'rb'), 'application/zip')}
        response = requests.post("http://mocked_melchior/api/skills/sync", files=files)
        
        if response.status_code == 200:
            print("✅ Simulated API Call Success")
        else:
            print("❌ Simulated API Call Failed")
            
    # Cleanup
    shutil.rmtree(temp_dir)
    print("=== Test Complete ===")

if __name__ == "__main__":
    test_sync_skills()
