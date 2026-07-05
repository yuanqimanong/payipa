# pyp-server

payipa 主控进程（导入名 `pyp_server`），唯一常驻进程。薄入口：FastAPI 应用工厂装配 `payipa-core`（同进程直接函数调用，非网络 API）、应用 REST API、agent WS 接入端点、调度循环、监控 API、SSR 页面（`templates/`+`static/`）、OpenAPI（`/openapi.json` + `/docs`）。

启动：`uv run uvicorn pyp_server.main:app`（M0 不依赖活 DB，引擎懒建）。
