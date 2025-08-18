# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "fastapi",
#     "uvicorn",
#     "pydantic",
#     "python-dotenv",
# ]
# ///

# --- This is my_web_app.py file ---
import asyncio
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sys
import os
from typing import Optional

# These imports should now work as 'src' is in sys.path
from clients.ollama_client import OllamaMCPClient
from abstract.config_container import ConfigContainer
from mcp.types import TextContent  # May also be from 'src/mcp/types.py' depending on structure

app = FastAPI(
    title="AI Chat API with MCP Tools",
    description="API to interact with the LLM Orchestrator via web interface.",
    version="1.0.0",
)

# --- CORS Configuration ---
# Allows access from HTML files opened locally or from localhost
origins = [
    "http://localhost",
    "http://localhost:8000", 
    "http://127.0.0.1",
    "http://127.0.0.1:8000",
    "null", 
    "file://",
    "*"
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
MCP_SERVER_CONFIG_PATH = os.getenv("MCP_SERVER_CONFIG_FILE", "examples/server.json")


# --- Data Schema for Chat Input ---
class ChatInput(BaseModel):
    message: str

# --- Event Handler for application startup and shutdown ---
@app.on_event("startup")
async def startup_event():
    global ollama_mcp_client
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))  

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

# --- Ensure client connection is properly closed ---
@app.on_event("shutdown")
async def shutdown_event():
    if ollama_mcp_client:
        await ollama_mcp_client.__aexit__(None, None, None)
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

    return StreamingResponse(generate_response(), media_type="text/plain")

# --- Test Endpoint (Optional) ---
@app.get("/")
async def read_root():
    return HTMLResponse("<h1>AI Chat Web API is running!</h1><p>Send POST requests to /chat</p>")

# Instructions to run:
# source .venv/bin/activate
# uvicorn my_web_app:app --reload --host 0.0.0.0 --port 8000
