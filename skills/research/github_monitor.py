# -*- coding: utf-8 -*-
"""
GitHub Monitor Skill (社群趨勢)
Iron Dome Audit: ✅ SAFE — Read-only API calls via requests

Provides: GitHub search and trending (via Search API sorting)
"""

import requests
import logging
import os

from skills.engine.scraping_adapter import fetch_json

logger = logging.getLogger("GitHubMonitor")

def _internet_enabled() -> bool:
    # Read env dynamically so toggles apply without restart.
    return os.environ.get("MAGI_ALLOW_INTERNET", "0").strip().lower() in {"1", "true", "yes", "on"}

def search_repos(query):
    """
    Search GitHub repositories.
    """
    if not _internet_enabled():
        return "⛔ 外網已停用（MAGI_ALLOW_INTERNET=0），GitHub 查詢不可用。"
    try:
        url = f"https://api.github.com/search/repositories?q={query}&sort=stars&order=desc"
        headers = {'User-Agent': 'MAGI-AI-Agent'}
        fetched = fetch_json(url, headers=headers, timeout=10)
        if not fetched.get("use_fallback"):
            if not fetched.get("success"):
                return f"❌ GitHub API Error: {fetched.get('status_code', 'unknown')}"
            data = fetched.get("data") or {}
        else:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code != 200:
                return f"❌ GitHub API Error: {response.status_code}"
            data = response.json()
        items = data.get('items', [])
        
        if not items:
            return f"🔍 找不到關於 `{query}` 的專案。"
            
        report = f"🐙 **GitHub 搜尋結果: {query}**\n\n"
        for item in items[:5]:
            name = item['full_name']
            desc = item['description'] or "No description"
            stars = item['stargazers_count']
            link = item['html_url']
            
            report += f"⭐ **{stars}** | [{name}]({link})\n"
            report += f"   _{desc}_\n\n"
            
        return report
        
    except Exception as e:
        return f"❌ GitHub 搜尋失敗: {e}"

def get_trending(language=None):
    """
    Simulate 'Trending' by searching for recently created popular repos.
    Real 'Trending' page scraping is fragile.
    """
    import datetime
    last_week = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    
    query = f"created:>{last_week}"
    if language:
        query += f" language:{language}"
        
    return search_repos(query)

if __name__ == "__main__":
    print(search_repos("openclaw"))
