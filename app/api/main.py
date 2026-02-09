from fastapi import APIRouter

from app.api.routes import login, pages, tasks, users, utils

api_router = APIRouter()
api_router.include_router(login.router)
api_router.include_router(users.router)
api_router.include_router(utils.router)
api_router.include_router(tasks.router)

pages_router = APIRouter()
pages_router.include_router(pages.router)
