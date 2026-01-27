import uvicorn
from fastapi import FastAPI

from app.api.main import api_router
from app.core.config import settings

app = FastAPI()


app.include_router(api_router, prefix=settings.API_VERSION_STR)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
