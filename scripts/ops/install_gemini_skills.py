import os
import shutil
import logging
import sys

# Add project root to path for imports
sys.path.insert(0, _MAGI_ROOT)
from skills.evolution.skill_genesis import validate_skill_safety
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

REPO_PATH = "/tmp/gemini_skills_repo"
SKILLS_SOURCE_DIR = os.path.join(REPO_PATH, "skills")
MAGI_SKILLS_DIR = f"{_MAGI_ROOT}/skills"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SkillInstaller")

def install_skills():
    if not os.path.exists(SKILLS_SOURCE_DIR):
        logger.error(f"❌ Source directory not found: {SKILLS_SOURCE_DIR}")
        return

    logger.info(f"🚀 Starting Skill Installation from {SKILLS_SOURCE_DIR}...")
    
    # Iterate through potential skill directories
    for item in os.listdir(SKILLS_SOURCE_DIR):
        source_skill_path = os.path.join(SKILLS_SOURCE_DIR, item)
        
        if os.path.isdir(source_skill_path):
            skill_md_path = os.path.join(source_skill_path, "SKILL.md")
            
            if os.path.exists(skill_md_path):
                logger.info(f"🔍 Analyzing skill: {item}...")
                
                # Read content
                with open(skill_md_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    
                # IRON DOME CHECK
                is_safe, violations = validate_skill_safety(content)
                
                if is_safe:
                    target_path = os.path.join(MAGI_SKILLS_DIR, item)
                    
                    # Install (Copy)
                    if os.path.exists(target_path):
                        logger.info(f"⚠️ Skill '{item}' already exists. Overwriting...")
                        shutil.rmtree(target_path)
                        
                    shutil.copytree(source_skill_path, target_path)
                    logger.info(f"✅ Installed Safe Skill: {item}")
                    
                else:
                    logger.warning(f"🛡️ IRON DOME BLOCKED '{item}': {violations}")
            else:
                logger.warning(f"⚠️ Skipping {item}: No SKILL.md found.")

    logger.info("✨ Installation Complete.")

if __name__ == "__main__":
    install_skills()
