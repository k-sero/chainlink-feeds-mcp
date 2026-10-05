from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8016
    env: str = "development"
    base_url: str = "http://localhost:8016"

    api_key: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    allowed_email_domains: str = ""
    allowed_emails: str = ""

    # Durable FastMCP OAuth storage (required outside development when Google OAuth is on)
    redis_url: str = ""
    jwt_signing_key: str = ""
    storage_encryption_key: str = ""
    oauth_storage_prefix: str = "chainlink-feeds-mcp"

    fastmcp_stateless_http: bool = True

    infura_api_key: str = ""


settings = Settings()
