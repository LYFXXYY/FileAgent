"""一次性迁移：给 model_providers 增加 capabilities 列（按能力路由模型）。

create_all() 只建新表、不会 ALTER 已存在的表，所以新列必须手动迁移。
新表 llm_calls 由启动时的 create_all() 自动建，不在本脚本职责内。

幂等：列已存在直接跳过，可重复运行。
"""

from sqlalchemy import text

import database

# 老提供商全是纯文本模型，回填成 text 才符合实际
DEFAULT_CAPABILITIES = "text"


def run() -> None:
    with database.engine.begin() as c:
        cols = [r[0] for r in c.execute(text("SHOW COLUMNS FROM model_providers")).fetchall()]
        if "capabilities" in cols:
            print(f"[skip] capabilities 列已存在（当前默认值 {DEFAULT_CAPABILITIES}）")
            return

        # DDL 里的默认值无法参数化，这里是硬编码常量，不接受外部输入
        c.execute(
            text(
                "ALTER TABLE model_providers "
                "ADD COLUMN capabilities VARCHAR(100) NOT NULL DEFAULT 'text' "
                "AFTER model_name"
            )
        )
        print(f"[done] model_providers.capabilities 已添加，现有行默认 {DEFAULT_CAPABILITIES}")


if __name__ == "__main__":
    run()
