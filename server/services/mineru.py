"""
MinerU 云 API 传输层 —— 扫描件 PDF 的 OCR 兜底提取

【职责边界】（与 services/llm.py、services/embedding.py 同款）
- 本模块**不碰 DB**：只做 HTTP 往返 + 解 zip，落库由调用方（process 路由）负责。
  这样才能用假 client 单测，也不在 async 函数里阻塞事件循环（调用方丢线程池）。
- **不重试、不降级**：解析是幂等的，失败后整份重跑比留半成品便宜
  （同 §3.12 embedding 的哲学）。别把 LLM 那套重试白名单搬过来。
- **不写 llm_calls**：MinerU 按页计费，响应里没有 token 可记，
  编一个数字进计量表就是造假（同 embedding 的成本边界）。

【官方云 v4 batch 流程】（2026-11-20 核实）
  1. POST /file-urls/batch        申请预签名上传地址（24h 有效），返回 batch_id
  2. PUT  <file_url>              上传文件字节，**不带 Content-Type 头**（签名不含它）
  3. 上传完成后服务端自动提交解析任务
  4. GET  /extract-results/batch/{batch_id}  轮询：
     state = waiting-file / pending / running / done / failed
  5. done 后下载 full_zip_url：zip 内含 full.md / content_list.json / images/

【Java 对比】
- 类似一个只封装 RestClient 调用的 Service：无状态、无事务，
  事务边界留在上层（Spring 里就是 @Transactional 不标在这一层）
"""

from __future__ import annotations

import io
import json
import logging
import time
import zipfile
from dataclasses import dataclass, field

import httpx

from config import get_settings, normalize_no_proxy_env

logger = logging.getLogger("fileagent.mineru")


class MinerUError(Exception):
    """MinerU 解析失败。文案直接给用户看，调用方转 400 或 _mark_failed"""


@dataclass(frozen=True)
class MinerUResult:
    """双轨输出的原料：markdown 给 plain_text 归一化，其余落盘留存供 RAG/PPT 消费"""

    markdown: str
    content_list: list | None = None
    images: dict[str, bytes] = field(default_factory=dict)


_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    """共享 httpx client。mineru.net 是境外公网端点，默认读系统代理环境变量"""
    global _client
    if _client is None:
        # trust_env=True 会读 NO_PROXY，裸 [::1] 会让 httpx 构造即炸；
        # 独立脚本不经过 main.py 的启动清理，这里自己兜一次（幂等）
        normalize_no_proxy_env()
        settings = get_settings()
        _client = httpx.Client(
            timeout=settings.mineru_http_timeout,
            trust_env=True,
            follow_redirects=True,
        )
    return _client


def close_client() -> None:
    """释放共享 client（测试隔离 / 进程退出用）"""
    global _client
    if _client is not None:
        _client.close()
        _client = None


def _check_payload(resp: httpx.Response, action: str) -> dict:
    """HTTP 层 + 业务码两层都要查：云 API 会回 200 但 code 非 0 的业务错误"""
    if resp.status_code != 200:
        raise MinerUError(f"MinerU {action}失败（HTTP {resp.status_code}）：{resp.text[:500]}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise MinerUError(f"MinerU {action}响应不是 JSON：{resp.text[:200]}") from e
    code = payload.get("code")
    if code not in (0, 200):
        raise MinerUError(f"MinerU {action}失败：{payload.get('msg') or payload}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MinerUError(f"MinerU {action}响应缺少 data 字段")
    return data


def _unzip_result(zip_bytes: bytes) -> MinerUResult:
    """解出 full.md / content_list / images/。md 缺失 = 产物不完整，响亮失败

    真实云 zip 的 content_list 带任务 UUID 前缀（{uuid}_content_list.json，
    2026-11-20 实测），且有 v2 变体——按后缀匹配、v2 优先。假端点用的裸
    content_list.json 同样命中。model.json / layout.json / origin.pdf 不留存。
    """
    markdown: str | None = None
    content_list: list | None = None
    content_list_v2: list | None = None
    images: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        logger.info("MinerU 产物 zip 条目：%s", zf.namelist())
        for name in zf.namelist():
            base = name.rsplit("/", 1)[-1]
            if not base or name.endswith("/"):
                continue
            # 只取 basename 落盘，zip 里带 ../ 的路径条目直接跳过（zip-slip 防护）
            if ".." in name.replace("\\", "/").split("/"):
                continue
            if base == "full.md":
                markdown = zf.read(name).decode("utf-8")
            elif base.endswith(("content_list_v2.json", "content_list.json")):
                try:
                    parsed = json.loads(zf.read(name).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    parsed = None
                if isinstance(parsed, list):
                    if base.endswith("content_list_v2.json"):
                        content_list_v2 = parsed
                    else:
                        content_list = parsed
            elif "/images/" in name.replace("\\", "/") or name.startswith("images/"):
                images[base] = zf.read(name)
    if markdown is None:
        raise MinerUError("MinerU 结果 zip 里没有 full.md，产物不完整")
    return MinerUResult(
        markdown=markdown,
        content_list=content_list_v2 if content_list_v2 is not None else content_list,
        images=images,
    )


def parse_pdf_via_mineru(pdf_bytes: bytes, filename: str) -> MinerUResult:
    """
    走完整 batch 流程解析一份 PDF，同步阻塞直到 done/failed/超时。

    调用方负责丢线程池（本函数含 time.sleep 轮询，绝不能直接在事件循环里跑）。
    未配置 token 时响亮报错——静默回空文本会让扫描件假装处理成功。
    """
    settings = get_settings()
    if not settings.mineru_token:
        raise MinerUError("未配置 MINERU_TOKEN，无法使用 MinerU 解析扫描件")

    base = settings.mineru_base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {settings.mineru_token}",
        "Content-Type": "application/json",
    }
    client = _get_client()

    # 1. 申请上传地址。enable_table/enable_formula 开着：表格不进批改正文，
    # 但 structured 留存要完整（后续 PPT 带图 / 规范库扩容靠它）
    data = _check_payload(
        client.post(
            f"{base}/file-urls/batch",
            json={
                "enable_formula": True,
                "enable_table": True,
                "language": "ch",
                "files": [{"name": filename, "is_ocr": True}],
            },
            headers=headers,
        ),
        "申请上传地址",
    )
    batch_id = data.get("batch_id")
    file_urls = data.get("file_urls") or []
    if not batch_id or not file_urls:
        raise MinerUError("MinerU 未返回上传地址（batch_id / file_urls 缺失）")

    # 2. PUT 上传：预签名 URL 的签名不含 Content-Type，带了反而可能被拒
    put_resp = client.put(file_urls[0], content=pdf_bytes)
    if put_resp.status_code not in (200, 201):
        raise MinerUError(f"MinerU 文件上传失败（HTTP {put_resp.status_code}）")

    # 3. 轮询直到终态。deadline 用单调时钟，不受系统改时间影响
    deadline = time.monotonic() + settings.mineru_max_wait
    result_url = f"{base}/extract-results/batch/{batch_id}"
    while True:
        data = _check_payload(client.get(result_url, headers=headers), "查询解析结果")
        items = data.get("extract_result") or []
        item = items[0] if items else {}
        state = item.get("state")
        if state == "done":
            zip_url = item.get("full_zip_url")
            if not zip_url:
                raise MinerUError("MinerU 任务完成但没有 full_zip_url")
            break
        if state == "failed":
            raise MinerUError(f"MinerU 解析失败：{item.get('err_msg') or '未知原因'}")
        if time.monotonic() >= deadline:
            raise MinerUError(
                f"MinerU 解析超时（等待超过 {settings.mineru_max_wait:.0f}s，"
                f"最后状态 {state or '未知'}），可稍后重试"
            )
        time.sleep(settings.mineru_poll_interval)

    # 4. 下载产物 zip（签名 URL 自带鉴权，不带 Authorization 头）
    zip_resp = client.get(zip_url)
    if zip_resp.status_code != 200:
        raise MinerUError(f"MinerU 结果下载失败（HTTP {zip_resp.status_code}）")
    result = _unzip_result(zip_resp.content)
    logger.info(
        "MinerU 解析完成：%s → markdown %s 字符、图片 %s 张",
        filename,
        len(result.markdown),
        len(result.images),
    )
    return result
