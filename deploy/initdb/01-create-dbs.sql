-- 三库初始化（P0-02）：POSTGRES_DB 已由镜像入口建平台库 pyp_sys，这里补建另外两库。
-- 仅在数据卷首次初始化时执行（postgres 镜像 docker-entrypoint-initdb.d 机制）；库名与
-- deploy/.env.compose 默认值一致——若改了 PG_DB_DATA_CENTER / PG_DB_BUSINESS，须同步改这里。
CREATE DATABASE data_center;
CREATE DATABASE business;
