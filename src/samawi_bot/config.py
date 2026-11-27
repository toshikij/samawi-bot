"""Runtime configuration, read from the environment (spec §8.6).

Secrets arrive through ECS from AWS Secrets Manager; nothing here has a real default for a
secret. Plain switches (LAUNCH_MODE, TRANSLATION_MODE, PUBLISH_LIVE) are ordinary env vars so a
change needs no code change.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LaunchMode(StrEnum):
    PRELAUNCH = "prelaunch"
    LIVE = "live"


class TranslationMode(StrEnum):
    KEMENAG = "kemenag"
    MAKNA = "makna"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"  # judges: claim checks and hook scores
    claude_writer_model: str = "claude-haiku-5-5"  # drafts and hook rewrites; checked by claude_model

    ig_user_id: str = ""
    ig_access_token: str = ""
    threads_user_id: str = ""
    threads_access_token: str = ""
    meta_app_id: str = ""
    meta_app_secret: str = ""
    graph_base_url: str = "https://graph.facebook.com/v23.0"
    threads_base_url: str = "https://graph.threads.net/v1.0"

    database_url: str = "sqlite:///./samawi-bot.db"
    s3_bucket: str = ""
    cdn_base_url: str = ""
    aws_region: str = "ap-southeast-3"

    alert_email: str = ""
    alert_from: str = ""
    email_transport: str = "log"  # "ses" in production

    samawi_repo: str = "toshikij/samawi"
    samawi_repo_token: str = ""
    samawi_repo_ref: str = "main"
    snapshot_dir: Path = Path("snapshot")

    launch_mode: LaunchMode = LaunchMode.PRELAUNCH
    play_store_url: str = ""
    translation_mode: TranslationMode = TranslationMode.KEMENAG
    kill_switch: bool = False
    publish_live: bool = False

    hadeethenc_base_url: str = "https://hadeethenc.com/api/v1"
    fawaz_hadith_base_url: str = "https://cdn.jsdelivr.net/gh/fawazahmed0/hadith-api@1"

    session_secret: str = Field(default="dev-only-not-secret")
    admin_base_path: str = "/samawi"
    dashboard_url: str = "http://localhost:3200/samawi"

    # The ECS bits the publish scheduler needs to create one-off schedules (spec §8.1).
    scheduler_group: str = "samawi"
    scheduler_role_arn: str = ""
    ecs_cluster_arn: str = ""
    ecs_task_definition_arn: str = ""
    ecs_subnets: str = ""
    ecs_security_groups: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
