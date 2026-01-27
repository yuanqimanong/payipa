from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["pages"])


def get_templates():
    return Jinja2Templates(directory="templates")


@router.get("/")
async def root_redirect():
    return RedirectResponse(url="/index", status_code=301)


@router.get("/index", response_class=HTMLResponse)
async def home_page(request: Request, templates: Jinja2Templates = Depends(get_templates)):
    return templates.TemplateResponse("index.html", {"request": request, "message": "欢迎来到首页"})
