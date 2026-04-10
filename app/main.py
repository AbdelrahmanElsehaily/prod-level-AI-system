from fastapi import FastAPI

app = FastAPI(title="Chat API", version="0.1.0")


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok"}
