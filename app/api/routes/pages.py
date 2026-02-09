from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(tags=["pages"])


def serve_template(template_name: str):
    """返回指定模板的响应"""
    with open(f"templates/{template_name}.html", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content)


@router.get("/")
async def root_redirect():
    return RedirectResponse(url="/index", status_code=301)


@router.get("/index", response_class=HTMLResponse)
async def home_page(request: Request):
    return serve_template("index")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return serve_template("login")
