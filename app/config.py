from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_mode: Literal["live", "demo"] = "demo"
    database_url: str = "sqlite:///./adrobot.db"
    keitaro_url: str = ""
    keitaro_api_key: SecretStr = SecretStr("")
    campaign_prefix: str = "test_Dyachkov_"
    request_timeout: float = 20

    @field_validator("campaign_prefix")
    @classmethod
    def valid_prefix(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"test_[A-Za-z][A-Za-z0-9]*_", value):
            raise ValueError("Use test_Surname_ as CAMPAIGN_PREFIX")
        return value

    @field_validator("request_timeout")
    @classmethod
    def valid_timeout(cls, value: float) -> float:
        if not 1 <= value <= 120:
            raise ValueError("REQUEST_TIMEOUT must be 1..120 seconds")
        return value

    @model_validator(mode="after")
    def live_credentials(self):
        if self.app_mode == "live":
            url = urlsplit(self.keitaro_url)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in ("", "/")
            ):
                raise ValueError("KEITARO_URL must be an HTTPS origin without credentials or path")
            if not self.keitaro_api_key.get_secret_value().strip():
                raise ValueError("KEITARO_API_KEY is required in live mode")
        return self
