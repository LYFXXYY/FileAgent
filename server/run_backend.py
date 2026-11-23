"""
后端启动器（替代 `uvicorn main:app`）

【为什么不能直接 `uvicorn main:app`】
uvicorn 在 Windows 上硬编码 ProactorEventLoop（`uvicorn/loops/asyncio.py:9`：
`if sys.platform == "win32" and not use_subprocess: return asyncio.ProactorEventLoop`），
完全不看 `asyncio` 全局 policy。psycopg3 异步模式（AsyncPostgresSaver / AsyncPostgresStore）
撞上 Proactor 直接报错 "Psycopg cannot use the 'ProactorEventLoop' to run in async mode"。

main.py 顶部那段 `set_event_loop_policy(Selector)` 只对独立脚本（smoke、eval）有效，
对 uvicorn 起的进程没用——loop 类型是 uvicorn 工厂直接挑的，跟 policy 无关。

【这个启动器怎么修】
两件事：
1. 切 policy → Selector（与 main.py 同款，独立 asyncio.run 时也能受益）；
2. monkey-patch `uvicorn.loops.asyncio.asyncio_loop_factory`，让它**永远返回** Selector 工厂，
   覆盖 uvicorn 的 Windows→Proactor 硬编码。
然后调 `uvicorn.run("main:app", ...)`。

【reload 怎么办】
`--reload` 走子进程入口 `uvicorn._subprocess.spawn`，子进程**不**会重跑本文件，
策略和工厂都不会被改。日常单测和 e2e 不需要 reload，本启动器够用。
真要热重载就两个选择：
- 接受 reload 模式下分块批改跑不通，临时用 `uv run uvicorn main:app --reload` 跑非分块路径；
- 或者写一个 reload 子进程的 entry module，同样 monkey-patch。

【psycopg 异步另一道坑】
psycopg3 异步模式默认 `connect_timeout=None`，Windows 上 TCP 握手会卡到内核层超时才报错。
图代码（graphs/grading_graph.py:_open_pg_conn）已经强制 5 秒超时，不在本启动器里管。
"""

import asyncio
import os
import sys


def _force_selector_on_windows() -> None:
    if sys.platform != "win32":
        return
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # uvicorn 的 asyncio_loop_factory 在 win32 + 无子进程时直接返回 Proactor 类，
    # 这里覆盖成 Selector——下面 import_from_string 读到的就是改过的版本
    def _patched_loop_factory(use_subprocess: bool = False):
        return asyncio.SelectorEventLoop

    import uvicorn.loops.asyncio as _uasyncio

    _uasyncio.asyncio_loop_factory = _patched_loop_factory


_force_selector_on_windows()

import uvicorn  # noqa: E402  策略 + 工厂先就位，再让 uvicorn 起 loop

if __name__ == "__main__":
    # host 传列表：同时绑 IPv4 与 IPv6 环回。
    # 只绑 127.0.0.1 时，浏览器/Vite 代理解析 localhost 会先试 ::1 吃一次连接拒绝再回退，
    # 本机实测每个请求凭空多 ~220ms（health 端点 0.004s → 0.23s），这就是"界面加载慢"的主因。
    #
    # FA_HOSTS 用于"让别人也能连上"：逗号分隔，例如 `FA_HOSTS=127.0.0.1,::1,10.201.211.9`
    # 把服务额外绑到局域网网卡上。默认只绑环回——不设这个变量时行为和从前逐字一致，
    # 不会因为拉了一次代码就把 API 暴露到局域网。
    hosts = [h.strip() for h in os.environ.get("FA_HOSTS", "127.0.0.1,::1").split(",") if h.strip()]
    uvicorn.run(
        "main:app",
        host=hosts,
        # FA_PORT 给 e2e 用：test_e2e_mineru.py 要带 MinerU 环境变量另起一个后端，
        # 不占用日常开发的 8000
        port=int(os.environ.get("FA_PORT", "8000")),
        log_level="info",
        # 只信本机这几跳转发的 X-Forwarded-For。uvicorn 的 proxy_headers 默认就是 True，
        # 写出来是为了让"访客真实 IP 从哪来"这件事在代码里可见：
        # 前端代理必须带 xfwd（client/vite.config.ts），Caddy 的 reverse_proxy 默认就带。
        # 不带的话所有访客在登录锁定与限流眼里都是同一个 ::1（AGENTS §3.22）。
        # 容器化时 Caddy 来自 docker 网段，要用 FORWARDED_ALLOW_IPS 把那个网段加进来。
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1,::1"),
    )
