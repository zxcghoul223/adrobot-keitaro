from typing import Literal

import pycountry
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateCampaign(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    country_code: str
    offer_id: int = Field(gt=0)
    domain_id: int = Field(gt=0)
    group_id: int = Field(gt=0)
    traffic_source_id: int = Field(gt=0)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("Укажите название кампании без управляющих символов")
        return value

    @field_validator("country_code")
    @classmethod
    def country(cls, value):
        value = value.strip().upper()
        if not pycountry.countries.get(alpha_2=value):
            raise ValueError("Нужен существующий двухбуквенный ISO-код страны")
        return value


class Revision(StrictModel):
    revision: int = Field(ge=1)


class FetchRequest(StrictModel):
    revision: int | None = Field(default=None, ge=1)
    discard_draft: bool = False


class DraftAction(Revision):
    action: Literal["add", "remove", "restore", "pin", "unpin"]
    offer_id: int = Field(gt=0)
    share: int | None = Field(default=None, ge=0, le=100, strict=True)
