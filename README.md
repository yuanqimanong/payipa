# payipa

Data Scraping Platform

## 常用 Alembic 命令

### 初始化项目

- alembic init <directory> - 创建新的 alembic 环境

### 迁移管理

- alembic revision --autogenerate -m "message" - 自动生成迁移脚本
- alembic upgrade head - 应用所有未应用的迁移
- alembic downgrade -1 - 回滚一个版本
- alembic current - 查看当前版本
- alembic history - 显示迁移历史

### 其他命令

- alembic stamp <revision> - 设置数据库版本而不执行迁移
- alembic show <revision> - 显示特定迁移的信息
- alembic branches - 显示分支状态
- alembic merge - 合并多个头版本