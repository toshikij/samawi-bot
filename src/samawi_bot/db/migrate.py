"""Alembic, driven from code so the image needs no alembic.ini."""

from __future__ import annotations

from importlib import resources

from alembic import command
from alembic.config import Config


def config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(resources.files("samawi_bot.db").joinpath("migrations")))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    from . import make_engine

    engine = make_engine(url)
    cfg = config(str(engine.url.render_as_string(hide_password=False)))
    command.upgrade(cfg, revision)
