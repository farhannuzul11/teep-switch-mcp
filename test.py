import asyncio
from ollama import AsyncClient

print("HALO")

async def main():
    client = AsyncClient(host="http://localhost:11434")
    try:
        response = await client.chat(
            model="qwen2.5:3b",
            messages=[{"role": "user", "content": "Halo"}],
            stream=False,
        )
        print("✅ Terhubung ke Ollama!")
        print("Jawaban:", response.message.content)
    except Exception as e:
        print("❌ Gagal terhubung ke Ollama.")
        print("Error:", e)

asyncio.run(main())
