import asyncio
from ollama_mcp_client import OllamaMCPClient

# Jika Anda tidak punya class ConfigContainer, ganti dengan dict
# from abstract.config_container import ConfigContainer

async def main():
    """
    Main function to configure and run the Ollama MCP client.
    """
    # Konfigurasi berisi nama server dan URL-nya.
    # Pastikan URL ini sesuai dengan host dan port server Anda.
    server_config = {
        "zabbix": "http://127.0.0.1:8000"
    }

    # config = ConfigContainer(server_config) # Gunakan jika ada

    print("Connecting to MCP server...")
    try:
        async with await OllamaMCPClient.create(server_config) as client:
            print("🤖 Assistant is ready! Type your message or 'exit' to quit.")
            
            while True:
                message = input("You: ")
                if message.lower() == 'exit':
                    break
                
                print("Assistant: ", end="", flush=True)
                async for part in client.process_message(message):
                    if part.role == "assistant":
                        print(part.content, end="", flush=True)
                print("\n")

    except ConnectionRefusedError:
        print("\n❌ Connection failed. Is the zabbix_server.py running?")
    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}")

if __name__ == "__main__":
    asyncio.run(main())