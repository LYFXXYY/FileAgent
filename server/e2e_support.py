"""五套 e2e 共用的前置检查（是被 import 的助手，不是可运行脚本）。

【为什么需要】
假端点全都跑在 `127.0.0.1:1900x`，而 provider 的 `base_url` 要过 SSRF 闸
（AGENTS §3.21：默认拒绝私有/环回地址）。`.env` 里那行 `ALLOW_PRIVATE_PROVIDER_URLS`
注释掉之后，用"日常启动命令"起的后端跑 e2e，会在**建 provider 那一步**收到 400——
现象像"后端坏了"，实际是启动方式不对，而且 structural / rag 会跑到中途才炸，
前面几节白跑。这里把它变成开场的一句可行动文案。

【退出码】4 = 环境前置不满足（同 `test_e2e_rag.py` 探不到 embedding 端点时的约定）。
"""

from __future__ import annotations

import sys

import httpx

LAUNCH_HINT = (
    "ALLOW_PRIVATE_PROVIDER_URLS=true LONGDOC_CHUNK_CHARS=60 LLM_TRUST_ENV_PROXY=false "
    "uv run python run_backend.py"
)

# 调用方的 finally 里普遍有 `sys.exit(...)`，它会把自己抛出的 SystemExit 连同退出码一起替换掉
# （test_e2e.py 第 305 行的注释记过这条教训）。所以除了抛，还要留一个旗子，
# 让那些 finally 能算出正确的退出码。
BLOCKED = False


def require_private_providers(client: httpx.Client, api: str, headers: dict) -> None:
    """
    确认后端允许指向本地端点的 provider，否则当场退出

    探针用一个不可能的端口（127.0.0.1:1）：校验发生在落库之前，所以被拒时什么都没写进去；
    201 时立刻删掉，不留脏数据。
    """
    global BLOCKED
    r = client.post(
        f"{api}/providers",
        headers=headers,
        json={
            "name": "_preflight_local_probe",
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": "sk-probe",
            "model_name": "probe",
        },
    )
    if r.status_code == 201:
        client.delete(f"{api}/providers/{r.json()['id']}", headers=headers)
        return

    BLOCKED = True
    print(
        "\n后端拒绝指向 127.0.0.1 的 provider（SSRF 闸生效，这是正确的默认值）。\n"
        f"跑 e2e 需要后端用这条命令启动：\n  {LAUNCH_HINT}\n"
        f"接口原话：{str(r.json().get('detail'))[:200]}",
        file=sys.stderr,
    )
    raise SystemExit(4)
