# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "fastapi",
#     "uvicorn",
#     "pydantic",
#     "python-dotenv", # Required if client.py uses load_dotenv()
# ]
# ///

# --- This is your my_web_app.py file ---

import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sys
import os
import json # Used for logging/debugging tool results
from typing import Optional
import datetime # Required for _tool_call (assuming client.py's _tool_call is part of OllamaMCPClient)

# These imports should now work as 'src' is in sys.path
from clients.ollama_client import OllamaMCPClient
from abstract.config_container import ConfigContainer
from mcp.types import TextContent # May also be from 'src/mcp/types.py' depending on your structure


app = FastAPI(
    title="AI Chat API with MCP Tools",
    description="API to interact with the LLM Orchestrator via web interface.",
    version="1.0.0",
)

# --- CORS Configuration (Essential for browser access) ---
# Allows access from HTML files opened locally or from localhost
origins = [
    "http://localhost",
    "http://localhost:8000", # Default Uvicorn port
    "http://127.0.0.1",
    "http://127.0.0.1:8000",
    "null", # For HTML files opened directly in a browser
    "file://", # For direct file access (less secure)
    "*" # Permissive for development, restrict in production
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Global variable to store OllamaMCPClient instance ---
ollama_mcp_client: Optional[OllamaMCPClient] = None
# Path to your MCP server configuration file (e.g., examples/server.json)
# Adjust if your server.json is in a different location relative to teep_mcp_path
MCP_SERVER_CONFIG_PATH = os.getenv("MCP_SERVER_CONFIG_FILE", "examples/server.json")


# --- Data Schema for Chat Input ---
class ChatInput(BaseModel):
    message: str

# --- Event Handler for application startup and shutdown ---
@app.on_event("startup")
async def startup_event():
    global ollama_mcp_client
    try:
        # Direktori file my_web_app.py sekarang di '.../teep-switch-mcp/examples'
        current_dir = os.path.dirname(os.path.abspath(__file__))  # .../teep-switch-mcp/examples

        # Folder teep-switch-mcp adalah parent dari examples
        teep_mcp_path = os.path.dirname(current_dir)  # .../teep-switch-mcp

        # src path
        src_path = os.path.join(teep_mcp_path, 'src')
        if src_path not in sys.path:
            sys.path.insert(0, src_path)

        # path ke config server.json yang ada di folder yang sama dengan my_web_app.py
        config_path = os.path.join(current_dir, 'server.json')

        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found at: {config_path}. Please check the path or set MCP_SERVER_CONFIG_FILE env var.")

        config = ConfigContainer.form_file(config_path)
        ollama_mcp_client = await OllamaMCPClient.create(config)
        await ollama_mcp_client.prepare_prompt()
        print("INFO: OllamaMCPClient initiated and connected successfully.")
    except Exception as e:
        print(f"ERROR: Failed to initialize OllamaMCPClient: {e}", file=sys.stderr)
        ollama_mcp_client = None


@app.on_event("shutdown")
async def shutdown_event():
    if ollama_mcp_client:
        await ollama_mcp_client.__aexit__(None, None, None) # Ensure client connection is properly closed
        print("INFO: OllamaMCPClient shut down.")

# --- Chat Endpoint ---
@app.post("/chat")
async def chat_endpoint(input_data: ChatInput):
    if not ollama_mcp_client:
        raise HTTPException(status_code=503, detail="AI service not ready. Please check server logs for client initialization errors.")
    
    # Generator function for streaming LLM responses
    async def generate_response():
        async for part in ollama_mcp_client.process_message(input_data.message):
            if part.get("role") == "assistant":
                yield part.get("content", "")
            # Tool output is printed in Python server logs for debugging.
            # Not displayed directly in web chat for a cleaner UI.
            # Uncomment below to display tool output in chat:
            # elif part.get("role") == "tool":
            #    yield f"\n(Tool executed: {part.get('content', '')})\n"

    return StreamingResponse(generate_response(), media_type="text/plain")

# --- Test Endpoint (Optional) ---
@app.get("/")
async def read_root():
    return HTMLResponse("<h1>AI Chat Web API is running!</h1><p>Send POST requests to /chat</p>")

# Instructions to run: uvicorn my_web_app:app --reload --host 0.0.0.0 --port 8000