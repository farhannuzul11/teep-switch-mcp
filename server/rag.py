# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "httpx",
#     "mcp[cli]",
# ]
# ///

import httpx
from mcp.server.fastmcp import FastMCP
import uvicorn

# Inisialisasi FastMCP dalam mode HTTP
mcp = FastMCP("RAGClientHTTP", stateless_http=True)

@mcp.tool()
async def local_rag_query(query: str) -> str:
    """Query local RAG server on port 8002

    Args:
        query (str): Your query

    Returns:
        str: Result or error
    """
    try:
        async with httpx.AsyncClient(timeout=120.0) as client: 
            res = await client.post(
                "http://localhost:8002/query/local",  # Ganti ke port sesuai server kamu
                json={"query": query},
            )
            res.raise_for_status()
            data = res.json()
            return data.get("result", "No result found")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        return f"Error executing tool local_rag_query: {e}\nTraceback:\n{tb}"

if __name__ == "__main__":
    uvicorn.run(mcp.streamable_http_app, host="localhost", port=3400)
