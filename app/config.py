from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Agent 服务配置。"""

    spring_base_url: str = "http://localhost:8080/api"
    request_timeout_seconds: int = 20
    enable_llm: bool = False
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MUYING_AGENT_",
        extra="ignore",
    )


settings = Settings()
