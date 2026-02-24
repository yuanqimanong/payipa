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


@router.get("/task", response_class=HTMLResponse)
async def task_page(request: Request):
    return serve_template("task")


@router.get("/data", response_class=HTMLResponse)
async def data_page(request: Request):
    return serve_template("data")


@router.get("/data_query", response_class=HTMLResponse)
async def data_query_page(request: Request):
    return serve_template("data_query")


@router.get("/data_detail", response_class=HTMLResponse)
async def data_detail_page(request: Request):
    return serve_template("data_detail")


@router.get("/aggregated_search", response_class=HTMLResponse)
async def aggregated_search_page(request: Request):
    return serve_template("aggregated_search")


@router.get("/log_view", response_class=HTMLResponse)
async def log_view_page(request: Request):
    return serve_template("log_view")


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    return serve_template("users")
