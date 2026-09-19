"""
数据库层 —— SQLAlchemy 2.0 引擎、会话管理

【Java 对比】
- engine         ≈ javax.sql.DataSource（连接池）
- Session        ≈ Hibernate Session（一次数据库会话）
- Base           ≈ @Entity 注解 —— 继承 Base 的 class 就是 ORM 实体
- get_db()       ≈ Spring 的 @Transactional + Session 生命周期管理
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config import get_settings

settings = get_settings()

# ---------- 引擎（连接池） ----------
# echo=False → 不打印 SQL；开发时改 True 可以看到所有执行的 SQL 语句
# pool_size / max_overflow → 对应 HikariCP 的 maximumPoolSize / additionalPools
# pool_pre_ping → 借出连接前先 ping 一下，防止 MySQL 关闭闲置连接（wait_timeout 默认 8 小时）
engine = create_engine(
    settings.database_url,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=3600,  # 连接回收时间（秒），比 MySQL wait_timeout 短即可
)

# ---------- 会话工厂 ----------
# SessionLocal() 相当于创建一个数据库会话
# autocommit=False: 不自动提交，必须显式 session.commit()（类似 Spring @Transactional）
# autoflush=False:   不自动 flush，查询前不会把 pending 写入 DB（减少意外提交）
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


# ---------- ORM 基类 ----------
# 所有 Model 继承这个 Base，等价于 JPA 的 @MappedSuperclass
# create_all() 只会为继承 Base 的 class 建表
class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI 依赖注入用的 Session 生成器

    【用法】在路由函数中：def handler(db: Session = Depends(get_db))
    【生命周期】每个 HTTP 请求 → 创建新 Session → 请求结束自动 close()
    【Java 对比】类似 Spring 的 @Transactional 事务边界——请求开始开事务，结束回滚或提交

    yield 前 = @Before / try 块开始
    yield 后 = @After / finally 块（无论请求成功失败都执行）
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()  # 归还连接到池，不销毁引擎
