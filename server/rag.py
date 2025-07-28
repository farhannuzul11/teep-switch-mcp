import httpx
from mcp.server.fastmcp import FastMCP
import uvicorn
import json

mcp = FastMCP("RAGClientHTTP", stateless_http=True)

@mcp.tool()
async def local_rag_query(query: str) -> str:
    """Query local RAG server on port 8002
    
    Args:
        query (str): Your query to send to the RAG server
    
    Returns:
        str: JSON formatted result containing the RAG response
    """
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            res = await client.post(
                "http://localhost:8002/query/local",
                json={"query": query},
            )
            res.raise_for_status()
            data = res.json()
            
            # Return structured JSON instead of plain text
            result = {
                "status": "success",
                "query": query,
                "result": data.get("result", "No result found"),
                "source": "local_rag_server",
                "timestamp": None  # You can add timestamp if needed
            }
            
            # Return as JSON string so MCP client can parse it properly
            return json.dumps(result, ensure_ascii=False, indent=2)
            
    except httpx.HTTPStatusError as e:
        error_result = {
            "status": "error",
            "error_type": "http_error",
            "error": f"HTTP {e.response.status_code}: {e.response.text}",
            "query": query
        }
        return json.dumps(error_result, ensure_ascii=False, indent=2)
        
    except httpx.TimeoutException:
        error_result = {
            "status": "error", 
            "error_type": "timeout",
            "error": "Request timed out after 120 seconds",
            "query": query
        }
        return json.dumps(error_result, ensure_ascii=False, indent=2)
        
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        error_result = {
            "status": "error",
            "error_type": "general_error", 
            "error": str(e),
            "traceback": tb,
            "query": query
        }
        return json.dumps(error_result, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    uvicorn.run(mcp.streamable_http_app, host="localhost", port=3100)

