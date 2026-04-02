# -*- coding: utf-8 -*-
"""
RSS Reader Skill (資訊收集)
Iron Dome Audit: ✅ SAFE — Read-only, no external execution
Dependencies: Standard Library only (xml.etree)

Provides: Feed subscription and reading
"""

import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import json
import logging
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

logger = logging.getLogger("RSSReader")

FEED_FILE = f"{_MAGI_ROOT}/rss_feeds.json"

class RSSReader:
    def __init__(self):
        self._load_feeds()

    def _load_feeds(self):
        if os.path.exists(FEED_FILE):
            try:
                with open(FEED_FILE, 'r', encoding='utf-8') as f:
                    self.feeds = json.load(f)
            except Exception:
                self.feeds = []
        else:
            self.feeds = []

    def _save_feeds(self):
        try:
            with open(FEED_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.feeds, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save feeds: {e}")

    def add_feed(self, url, name=None):
        """Add a new RSS feed."""
        if any(f['url'] == url for f in self.feeds):
            return "⚠️ 該 RSS 已在訂閱清單中。"
        
        # Verify feed first
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                content = response.read()

            # Simple parse check
            root = ET.fromstring(content)
            title = name
            if not title:
                channel = root.find('channel')
                if channel is not None:
                    t = channel.find('title')
                    if t is not None:
                        title = t.text

            if not title:
                title = "Unknown Feed"

            self.feeds.append({"url": url, "name": title})
            self._save_feeds()
            return f"✅ 已訂閱: **{title}**"
        except urllib.error.URLError as e:
            reason = str(e.reason) if hasattr(e, 'reason') else str(e)
            if 'nodename nor servname' in reason or 'Name or service not known' in reason or 'Errno 8' in reason:
                return f"❌ 訂閱失敗：無法解析網址 `{url}`，請確認 URL 是否正確。"
            return f"❌ 訂閱失敗 (網路錯誤): {reason}"
        except Exception as e:
            return f"❌ 訂閱失敗 (無法讀取 RSS): {e}"

    def list_feeds(self):
        if not self.feeds:
            return "📭 目前沒有訂閱任何 RSS。"
        
        report = "📰 **RSS 訂閱清單**\n"
        for f in self.feeds:
            report += f"- [{f['name']}]({f['url']})\n"
        return report

    def read_latest(self, max_items=5):
        """Read latest news from all feeds."""
        if not self.feeds:
            return "📭 請先訂閱 RSS (使用 `@MAGI 訂閱 <URL>`)。"

        report = f"📰 **最新消息 ({datetime.now().strftime('%H:%M')})**\n\n"
        
        for feed in self.feeds:
            try:
                with urllib.request.urlopen(feed['url'], timeout=5) as response:
                    content = response.read()
                
                root = ET.fromstring(content)
                channel = root.find('channel')
                items = channel.findall('item')
                
                if not items:
                    continue
                    
                report += f"**{feed['name']}**\n"
                count = 0
                for item in items:
                    if count >= 3: break # Max 3 per feed
                    
                    title = item.find('title').text
                    link = item.find('link').text
                    
                    report += f"- [{title}]({link})\n"
                    count += 1
                report += "\n"
            except Exception as e:
                report += f"❌ {feed['name']}: 讀取失敗\n"
        
        return report

if __name__ == "__main__":
    r = RSSReader()
    # print(r.add_feed("https://news.ycombinator.com/rss", "Hacker News"))
    print(r.read_latest())
