from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Agent 服务配置。

    所有字段均可通过环境变量或 `.env` 覆盖（前缀 `MUYING_AGENT_`）。
    默认值仅供本地启动兜底，**严禁在此处硬编码任何密钥**。
    """

    # ===== 业务集成 =====
    # Spring Boot 电商平台 API 根地址
    spring_base_url: str = "http://localhost:8080/api"
    # 调用 Spring Boot 与大模型接口的超时时间（秒）
    request_timeout_seconds: int = 30

    # ===== 大模型 =====
    # 是否启用大模型润色；关闭时仅使用业务草稿，便于本地演示
    enable_llm: bool = False
    # OpenAI 兼容接口地址
    openai_base_url: str = "https://api.openai.com/v1"
    # 大模型密钥；务必通过 .env 或运行时环境变量注入，不要写进代码默认值
    openai_api_key: str = ""
    # 大模型名称；具体模型由部署方按账号能力指定
    openai_model: str = "gpt-4o-mini"

    # ===== 安全 =====
    # 允许跨域来源，逗号分隔；默认仅放行常见前端本地开发端口和 muying-mall。
    # 生产环境必须显式配置，禁止保留 "*"。
    allowed_origins: str = "http://localhost:3000,http://localhost:5173,http://localhost:8080"
    # 内网共享密钥；非空时所有 /api/v1/* 请求必须带 X-Internal-Token 头。
    # 留空表示不开启（仅推荐本地调试场景）。
    internal_token: str = ""

    # ===== 上下文裁剪 =====
    # 历史消息总字符上限；超过则从最早的开始丢弃。
    # 256k 字符约 ~64k tokens 的安全上界，按需在 .env 调小。
    history_max_chars: int = 256 * 1024

    # ===== 日志 =====
    # 日志级别：DEBUG / INFO / WARNING / ERROR
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MUYING_AGENT_",
        extra="ignore",
    )

    @property
    def allowed_origins_list(self) -> list[str]:
        """将逗号分隔字符串解析为列表，去除空白。"""
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


settings = Settings()
