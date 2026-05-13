#!/usr/bin/env python3
"""
MAGI MCP SERVER (Model Context Protocol)
=========================================
Exposes MAGI tools to OpenClaw and other MCP-compatible clients.
This allows OpenClaw to use MAGI's web research and skill capabilities.

Run with: python3 magi_mcp_server.py
"""

import sys
import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
import json
import asyncio
from typing import Any

# Add MAGI to path
sys.path.insert(0, _MAGI_ROOT)

# Import MAGI modules
from skills.research.web_research import search_web, research_topic, fetch_url_content
from skills.evolution.skill_genesis import generate_skill, list_skills, validate_skill_safety

# MCP Protocol Implementation (stdio-based)
class MCPServer:
    def __init__(self):
        self.tools = {
            "magi_search": {
                "description": "Search the web using DuckDuckGo. Returns search results with titles, URLs, and snippets.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "num_results": {"type": "integer", "description": "Number of results (default 5)", "default": 5}
                    },
                    "required": ["query"]
                }
            },
            "magi_research": {
                "description": "Deep research a topic: searches web and fetches content from top results.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string", "description": "Topic to research"},
                        "depth": {"type": "integer", "description": "Number of sources to fetch (default 3)", "default": 3}
                    },
                    "required": ["topic"]
                }
            },
            "magi_fetch_url": {
                "description": "Fetch and extract main content from a URL.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to fetch"}
                    },
                    "required": ["url"]
                }
            },
            "magi_list_skills": {
                "description": "List all installed MAGI skills.",
                "inputSchema": {
                    "type": "object",
                    "properties": {}
                }
            },
            "magi_create_skill": {
                "description": "Create a new MAGI skill (with Iron Dome safety check).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Skill name (lowercase, hyphens)"},
                        "description": {"type": "string", "description": "What the skill does"},
                        "instructions": {"type": "string", "description": "Main instructions for the skill"}
                    },
                    "required": ["name", "description", "instructions"]
                }
            }
        }
    
    def handle_request(self, request: dict) -> dict:
        """Handle incoming MCP request."""
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id")
        
        if method == "initialize":
            return self._response(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "magi-mcp", "version": "1.0.0"}
            })
        
        elif method == "tools/list":
            tools_list = [
                {"name": name, "description": info["description"], "inputSchema": info["inputSchema"]}
                for name, info in self.tools.items()
            ]
            return self._response(req_id, {"tools": tools_list})
        
        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            
            try:
                result = self._execute_tool(tool_name, arguments)
                return self._response(req_id, {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]
                })
            except Exception as e:
                return self._error(req_id, -32000, str(e))
        
        elif method == "notifications/initialized":
            return None  # No response needed for notifications
        
        else:
            return self._error(req_id, -32601, f"Method not found: {method}")
    
    def _execute_tool(self, name: str, args: dict) -> Any:
        """Execute a MAGI tool."""
        if name == "magi_search":
            return search_web(args["query"], args.get("num_results", 5))
        
        elif name == "magi_research":
            result = research_topic(args["topic"], args.get("depth", 3))
            # Simplify output for readability
            return {
                "topic": result["topic"],
                "sources": [{"title": s["title"], "url": s["url"]} for s in result.get("sources", [])],
                "content_preview": result.get("combined_content", "")[:3000]
            }
        
        elif name == "magi_fetch_url":
            return fetch_url_content(args["url"])
        
        elif name == "magi_list_skills":
            return list_skills()
        
        elif name == "magi_create_skill":
            return generate_skill(
                name=args["name"],
                description=args["description"],
                instructions=args["instructions"],
                author="CASPER-via-MCP"
            )
        
        else:
            raise ValueError(f"Unknown tool: {name}")
    
    def _response(self, req_id, result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    
    def _error(self, req_id, code, message):
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
    
    def run(self):
        """Run the MCP server (stdio mode)."""
        print("MAGI MCP Server starting...", file=sys.stderr)
        
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            
            try:
                request = json.loads(line)
                response = self.handle_request(request)
                
                if response:
                    print(json.dumps(response), flush=True)
                    
            except json.JSONDecodeError as e:
                print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {e}"}}), flush=True)
            except Exception as e:
                print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": f"Internal error: {e}"}}), flush=True)


if __name__ == "__main__":
    server = MCPServer()
    server.run()
