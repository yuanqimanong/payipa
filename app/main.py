import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles

from app.api.main import api_router, pages_router
from app.core.config import settings


def custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}"


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_VERSION_STR}/openapi.json",
    generate_unique_id_function=custom_generate_unique_id,
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 在生产环境中应该限制为特定域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(pages_router)
app.include_router(api_router, prefix=settings.API_VERSION_STR)

if __name__ == "__main__":
    # uvicorn.run("app.main:app", host="127.0.0.1", port=80, workers=1, reload=True)
    uvicorn.run("app.main:app", host="0.0.0.0", port=8081, workers=1, reload=True)
