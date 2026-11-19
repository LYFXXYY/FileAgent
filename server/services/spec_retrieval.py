"""规范库向量检索（阶段 6 A2）—— 按条款召回用户自己的规范库

【这一层只管"取回哪些条款"，不管"怎么用"】
拼进 prompt、截断、把引用记进结果，都在调用方（批改链路）。检索层保持纯函数化，
才能在没有 Postgres 的单测里跑（§7：单测不连 Postgres 也不出网）。

【为什么必须带 user_id】
`spec_chunks` 自己不挂 user_id，只通过 `spec_docs` 间接归属。这里用 JOIN 把归属压进
SQL，而不是"先查用户的所有 doc_id 再 IN"——后者在规范库变大后会把一条长列表塞进语句，
而且漏掉 JOIN 就等于跨用户读别人的规范。§3.1 的隔离底线在检索层同样是底线。

【为什么排除 embedding IS NULL】
加列的迁移不回填，`SPEC_EMBED_ON_INGEST=false` 期间入库的块也没有向量。
NULL 参与 `<=>` 排序会得到 NULL score，位置不可解释——直接排除，宁可少召回不要乱序。

【为什么要滑窗查询】
bge-m3 对超长输入**不报错、静默截断**（实测 16,280 字仍返回 1024 维，与截断到 1200 字
的余弦只有 0.8811）。整篇 5000 字的文档直接编码，等于只拿开头一小段去检索，
文档后半部分该命中的条款永远进不来。所以按窗口切开发多次、再合并取最优分。
本地端点一次编码约 30ms，4 个窗口的代价可以忽略。

【失败语义】
编码端点不可用 → 抛 EmbeddingError，**由调用方决定怎么降级**。本层不吞异常，
也不返回空列表假装"库里没有相关条款"——那两种情况在结果里长得一模一样，
用户没法区分"没查到"和"查不了"。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from sqlalchemy import text
from sqlalchemy.orm import Session

from config import get_settings
from models.spec_doc import SpecChunk, SpecDoc
from services.embedding import embed_query

logger = logging.getLogger("fileagent.spec_retrieval")

# 条款号必须在行首（可带 Markdown 井号）：`split_by_clause` 保证一块一条款，
# 块头是"标题行 + 章行 + 条款行"这样的连续若干行，条款号可能在第 2 也可能在第 3 行
# （实测那份教学示例稿的第 0 块就是 `# 文档标题 / 第1章 错别字类 / 1.1.1 同音别字`，
#  只看前两行会把它的编号读成 doc3-#0，用户看到的引用和模型引用的编号都是假的）。
# 锚在行首 + 遇到【开头的正文行就停，两件事一起才既不漏也不误读：
# 正文里的"行距 1.5 倍""参见 3.1.6"都在行中，不会冒充标题。
_CLAUSE_RE = re.compile(r"^\s*(?:#{1,6}\s*)?(\d+(?:\.\d+){1,3})\s")

# 块头最多扫几行。条款标题之前只可能有文档标题和章标题，三行足够；
# 再多就开始进正文，误读风险大于收益
_HEAD_LINES = 4


# pgvector 的向量字面量就是 "[f1,f2,...]"；pgvector 自己解析，不做数值校验
_VECTOR_SQL = text(
    """
    SELECT s.doc_id, s.seq, s.start, s.end, s.text,
           1 - (s.embedding <=> CAST(:q AS vector)) AS score
    FROM spec_chunks s
    JOIN spec_docs d ON d.id = s.doc_id
    WHERE d.user_id = :uid AND s.embedding IS NOT NULL
    ORDER BY s.embedding <=> CAST(:q AS vector)
    LIMIT :k
    """
)


@dataclass(frozen=True)
class SpecHit:
    """一条命中的规范条款"""

    doc_id: int
    seq: int
    start: int
    end: int
    text: str
    score: float
    # 这一条是靠哪一路召回的：vector=余弦、lexical=字二元组重合、both=两路都命中。
    # 两个分数标尺不同（余弦 vs Dice），不标来源的话 refs 里的数字没法解释
    via: str = "vector"

    @property
    def clause(self) -> str:
        """条款号（给评测台和引用显示用）；解析不出就回落到 doc-seq 定位符"""
        return clause_id(self.text) or f"doc{self.doc_id}-#{self.seq}"


def _bigrams(s: str) -> set[str]:
    """
    去空白后的相邻二字组。

    为什么用字二元组而不是分词：镜像里没有 pg_jieba / zhparser，应用侧也没引分词库，
    而中文的检索信号大量在二字组合上。它不是 BM25 的替代，是"没有分词器时唯一站得住
    的词法信号"。
    """
    s = "".join(ch for ch in s if not ch.isspace())
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _dice(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def clause_id(chunk_text: str) -> str | None:
    """
    从块头摘条款号。

    只认行首，并且遇到正文标记（【…】）就停——标题区之后就是正文，
    再往里找数字会把交叉引用读成编号。
    """
    for line in chunk_text.splitlines()[:_HEAD_LINES]:
        stripped = line.strip()
        if stripped.startswith("【"):
            return None
        m = _CLAUSE_RE.match(line)
        if m:
            return m.group(1)
    return None


def search_by_vector(
    db: Session,
    *,
    user_id: int,
    vector: Sequence[float],
    limit: int,
) -> list[SpecHit]:
    """
    精确余弦扫描（顺序扫描，**刻意不建 ANN 索引**）。

    pgvector 的近似索引带 `user_id` 过滤会静默少返回 top-k，而隔离要求每个查询都带
    user_id——两者正好对撞。量级到万级再连同过滤策略一起重审。
    """
    rows = (
        db.execute(_VECTOR_SQL, {"q": str(list(vector)), "uid": user_id, "k": limit})
        .mappings()
        .all()
    )
    return [
        SpecHit(
            doc_id=r["doc_id"],
            seq=r["seq"],
            start=r["start"],
            end=r["end"],
            text=r["text"],
            score=float(r["score"]),
        )
        for r in rows
    ]


def lexical_hits(
    db: Session,
    *,
    user_id: int,
    document_text: str,
    limit: int,
) -> list[SpecHit]:
    """
    词法召回：条款与文档的字二元组重合度（Dice）排序。

    为什么放在应用侧算而不是 SQL：镜像里没有中文分词扩展，`to_tsvector` 对中文等于
    按整串切，起不到关键词作用；而字二元组不需要分词器，66 条条款一次全读进来
    算重合度是毫秒级。库到几千条时这里要改成先按长度/章节预筛，别默认它永远便宜。

    为什么不要求 `embedding IS NOT NULL`：词法这一路压根不用向量，
    所以"回填还没跑完"的库也能召回——这是混合模式比纯向量更抗故障的地方。
    """
    rows = (
        db.query(SpecChunk, SpecDoc.id.label("d_id"))
        .join(SpecDoc, SpecDoc.id == SpecChunk.doc_id)
        .filter(SpecDoc.user_id == user_id)
        .all()
    )
    dg = _bigrams(document_text)
    scored = []
    for chunk, _ in rows:
        score = _dice(dg, _bigrams(chunk.text))
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda x: x[0], reverse=True)

    return [
        SpecHit(
            doc_id=c.doc_id,
            seq=c.seq,
            start=c.start,
            end=c.end,
            text=c.text,
            score=round(float(s), 4),
            via="lexical",
        )
        for s, c in scored[:limit]
    ]


def fuse_rrf(arms: dict[str, list[SpecHit]], top_k: int, rrf_k: int = 60) -> list[SpecHit]:
    """
    倒数排名融合（RRF）：`sum(1 / (rrf_k + rank))`。

    为什么不用"分数相加"：余弦和 Dice 是两个不同标尺（前者常见 0.4–0.6，后者常见
    0.05–0.2），直接相加等于让标尺大的那一路永远说了算，而且换个模型比例就变。
    RRF 只看名次，对量纲不敏感，是跨检索器融合的标准做法。

    同一条款在两路都命中时分数累加，自然排到前面——`via` 标成 both，
    让引用记录能说明"它是被两路共同选中的"。
    """
    fused: dict[tuple[int, int], float] = {}
    keep: dict[tuple[int, int], SpecHit] = {}
    for source, hits in arms.items():
        for rank, hit in enumerate(hits):
            key = (hit.doc_id, hit.seq)
            fused[key] = fused.get(key, 0.0) + 1 / (rrf_k + rank + 1)
            current = keep.get(key)
            if current is None:
                keep[key] = hit
            elif current.via != source and current.via != "both":
                # 保留先出现那一路的分数标尺（向量臂排在前面，所以 refs 里的数字是余弦），
                # 只把来源改成 both。换成后到那一路的分会让同一个字段两种含义
                keep[key] = replace(current, via="both")

    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    return [keep[key] for key, _ in ordered]


def plan_windows(text_: str, window_chars: int, max_windows: int) -> list[str]:
    """
    把待检索文本切成查询窗口。

    超过 `max_windows` 段时**等步长抽样**而不是只取前几段：整篇 3 万字的文档若只查
    开头 4 段，后面的条款永远进不来，而那种"漏在末尾"在结果里完全没有信号。
    抽样会漏中间，但至少覆盖首尾，且漏掉的量是可见的（窗口数被钳住了）。
    """
    body = (text_ or "").strip()
    if not body or window_chars <= 0:
        return [body] if body else []
    if max_windows <= 0:
        max_windows = 1

    pieces = [body[i : i + window_chars] for i in range(0, len(body), window_chars)]
    if len(pieces) <= max_windows:
        return pieces
    if max_windows == 1:
        return [pieces[0]]

    # 等步长取样必须把首段和末段都算进去：`int(i * step)` 那种取法末段永远落不进
    # 窗口（10 段抽 4 个只到第 7 段），文档尾巴的条款就静默查不到了
    last = len(pieces) - 1
    idxs = {round(i * last / (max_windows - 1)) for i in range(max_windows)}
    return [pieces[i] for i in sorted(idxs)]


def merge_hits(batches: Sequence[Sequence[SpecHit]], top_k: int) -> list[SpecHit]:
    """
    多窗口结果合并：同一条款只留最高分，再按分数降序取 top_k。

    不去重的话，一条命中多个窗口的条款会占掉好几个坑位，top_k 实际只有两三条不同条款。
    """
    best: dict[tuple[int, int], SpecHit] = {}
    for batch in batches:
        for hit in batch:
            key = (hit.doc_id, hit.seq)
            current = best.get(key)
            if current is None or hit.score > current.score:
                best[key] = hit
    return sorted(best.values(), key=lambda h: h.score, reverse=True)[:top_k]


def retrieve_for_document(
    db: Session,
    *,
    user_id: int,
    document_text: str,
    top_k: int | None = None,
    mode: str | None = None,
) -> list[SpecHit]:
    """
    给一份待处理文档召回条款：向量滑窗 +（默认）词法，RRF 融合。

    向量这一路每个窗口都走 `embed_query`（前缀由配置决定），文档端入库向量不带——
    两边怎么配对取决于所用模型的训练方式，见 config.embedding_query_prefix 那条实测。

    混合是默认而不是可选项的理由：纯向量在这批夹具上 recall@5 只有 0.491，
    而一份 58 字的真实短文能五条全不相关；词法补的正是"文档里出现了规范举过的错例"
    这类表面重合。反过来词法对没见过的措辞无能为力，那部分仍由向量兜。
    ⚠ 但夹具的错例本来就是从规范里抄出来的，这结构性偏向词法——所以别把
      0.72 当成"词法更强"的世界性结论，它只说明这一层信号在本库里可区分。
    """
    settings = get_settings()
    k = top_k if top_k and top_k > 0 else settings.spec_retrieval_top_k
    # mode 参数给评测台做消融用；线上调用方不传，走配置
    mode = (mode or settings.spec_retrieval_mode or "hybrid").lower()
    if mode not in ("hybrid", "vector", "lexical"):
        logger.warning("未知的 SPEC_RETRIEVAL_MODE=%s，按 hybrid 处理", mode)
        mode = "hybrid"

    if not document_text.strip():
        return []

    arms: dict[str, list[SpecHit]] = {}
    # 每路各取 k 条再融合成 k 条。实测（9 夹具 / 66 条款）每路取 2k 反而更差：
    # k=10 时 hybrid 从 0.778 掉到 0.741——向量那一路的名次靠后也要占坑，
    # 把词法的高置信条款挤出去了。融合的意义是"任一路失灵都还能召回"，不是多召回。
    arm_limit = k

    if mode in ("vector", "hybrid"):
        windows = plan_windows(
            document_text,
            settings.spec_query_window_chars,
            settings.spec_query_max_windows,
        )
        batches = []
        for w in windows:
            vec = embed_query(w)
            batches.append(search_by_vector(db, user_id=user_id, vector=vec, limit=arm_limit))
        arms["vector"] = merge_hits(batches, arm_limit)

    if mode in ("lexical", "hybrid"):
        arms["lexical"] = lexical_hits(
            db,
            user_id=user_id,
            document_text=document_text,
            limit=arm_limit,
        )

    if mode == "vector":
        hits = arms["vector"][:k]
    elif mode == "lexical":
        hits = arms["lexical"][:k]
    else:
        hits = fuse_rrf(arms, k)

    logger.info(
        "规范检索(mode=%s)：向量 %d 条 / 词法 %d 条 → 取 %d 条 → %s",
        mode,
        len(arms.get("vector", [])),
        len(arms.get("lexical", [])),
        len(hits),
        ", ".join(f"{h.clause}({h.score:.3f}/{h.via})" for h in hits),
    )
    return hits


def build_context(hits: Sequence[SpecHit], max_chars: int | None = None) -> str:
    """
    把命中条款拼成一段可直接进 prompt 的上下文。

    超预算时从尾部截断（保留分数高的），并在末尾留一句"已截断"——
    不写这句的话，模型和用户都以为看到的是完整条款集合。
    """
    if not hits:
        return ""
    budget = max_chars if max_chars is not None else get_settings().spec_context_max_chars

    parts: list[str] = []
    used = 0
    dropped = 0
    for h in hits:
        block = f"【{h.clause}】{h.text.strip()}"
        if used + len(block) > budget:
            dropped += 1
            continue
        parts.append(block)
        used += len(block)

    if dropped:
        parts.append(f"（其余 {dropped} 条因上下文预算未列出）")
    return "\n\n".join(parts)


def spec_doc_count(db: Session, *, user_id: int) -> int:
    """库里有没有规范文档。批改链路用它决定"要不要尝试检索"，避免空库也去编码"""
    return db.query(SpecDoc.id).filter(SpecDoc.user_id == user_id).count()


@dataclass(frozen=True)
class SpecContext:
    """给批改链路的一次检索结果：进 prompt 的文本 + 可溯源的引用 + 降级说明"""

    context: str
    refs: list[dict]
    note: str | None


def collect_spec_context(db: Session, *, user_id: int, document_text: str) -> SpecContext:
    """
    批改前的规范召回，**旁路设计**：任何异常都不许把已能跑通的批改判成失败。

    四种出口互不混淆：
    - 开关关掉 / 库里没文档 → 空上下文、无说明（本来就不该有动作）
    - 端点不可用 → 空上下文 + note（用户必须能看出"这次没参考规范是因为查不了"）
    - 查到了但都不相关 → 空上下文、无 note
    - 正常 → 上下文 + refs

    和 §3.7 的 `issues=None` vs `[]` 同一个道理：两种"没有"在结果里长得一样，
    就是撒谎。
    """
    settings = get_settings()
    if not settings.spec_rag_enabled or not document_text.strip():
        return SpecContext(context="", refs=[], note=None)
    if spec_doc_count(db, user_id=user_id) == 0:
        return SpecContext(context="", refs=[], note=None)

    try:
        hits = retrieve_for_document(db, user_id=user_id, document_text=document_text)
    except Exception as e:  # EmbeddingError 及 SQL 方言意外
        logger.warning("规范检索失败，本次批改不带条款上下文：%s", e)
        return SpecContext(
            context="",
            refs=[],
            note="规范库检索不可用（编码端点未就绪），本次批改未参考规范条款",
        )

    if not hits:
        return SpecContext(context="", refs=[], note=None)

    return SpecContext(
        context=build_context(hits),
        refs=[
            {
                "clause": h.clause,
                "score": round(h.score, 4),
                "via": h.via,
                "doc_id": h.doc_id,
                "seq": h.seq,
            }
            for h in hits
        ],
        note=None,
    )
