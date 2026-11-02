"""应用配置。所有可调参数集中在这里，通过 .env 覆盖。"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- 应用 ----------
    app_env: str = "dev"
    debug: bool = True
    secret_key: str = "dev-only-please-change-me"
    access_token_expire_days: int = 7

    # ---------- 数据库 ----------
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "raoxin_genealogy"
    db_charset: str = "utf8mb4"

    # ---------- 微信 ----------
    wx_appid: str = ""
    wx_secret: str = ""
    wx_mock_login: bool = True

    # ---------- 树布局参数（设计文档定义，端上按此还原） ----------
    # 节点尺寸与间距决定 canvas 尺寸，改这里等于改端上视觉，谨慎。
    # 2026-11-02 从 88×96 缩到 64×72：用户反馈"一屏看不了几个人"，
    # 目标是 375 宽的屏（去掉 62px 丁口栏）横向能放下 4 张牌：
    # 4×64 + 3×16 间距 = 304 ≤ 313，正好。
    tree_node_width: int = 64
    tree_node_height: int = 72
    tree_gen_gap: int = 140
    tree_sibling_gap: int = 16

    # 我的一家：以锚点为中心取的代数。
    # 2026-10-21 从「上 3 下 3」收到「上 2 下 1」：实测老谱导入后
    # 一个族 15 代 / 500+ 节点，上下各 3 代再加兄弟和配偶，4 代就宽到
    # 整屏适配只剩 31%（字高 3.7px）。宁可少一代，也要字能看清。
    # 焦点视图的上下代际：上 1（父母）下 1（子女），一屏三代。
    # 2026-10-21 启明定：不做全图，靠"点子女→焦点下移"逐代往下看，
    # 所以不需要靠多看一代祖先来给方向感；省下的高度留给子女那一行。
    mine_up_generations: int = 1
    mine_down_generations: int = 1
    # 同代（兄弟姐妹）最多显示几个**男丁节点**（含锚点本人）。
    # 一屏横向只容得下 3—4 个夫妻单元，再多只能靠缩放到看不清。
    # 超出的折叠成「还有 N 位」，点它按 siblings 参数重新拉取。
    mine_sibling_limit: int = 3
    # 外孙及外姓后代向下展示层数（决策第 2 条：仅"我的一家"可见）
    mine_external_desc_generations: int = 2

    # 编辑码默认授权范围（决策第 5 条）
    invite_default_scope: dict = {
        "up": 3,
        "down": 2,
        "spouse": True,
        "siblings": False,
        "externalDesc": 2,
    }
    invite_default_expire_days: int = 30

    # 全族总览缩略模式最多返回节点数，超出则抽稀
    overview_max_nodes: int = 6000

    @property
    def database_url(self) -> str:
        # 密码可能含 @ / : 等字符，必须 URL 编码
        pwd = quote_plus(self.db_password or "")
        user = quote_plus(self.db_user)
        return (
            f"mysql+pymysql://{user}:{pwd}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset={self.db_charset}"
        )

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
