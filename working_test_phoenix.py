import os, asyncio
from phoenix.otel import register
from openinference.instrumentation.openai import OpenAIInstrumentor
from openai import AsyncOpenAI

register(project_name="debug-smoke")

OpenAIInstrumentor().instrument()         # uses the global tracer provider
client = AsyncOpenAI()

async def demo():
    response = await client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": "ping"}]
    )
    print(response.choices[0].message.content)

asyncio.run(demo())