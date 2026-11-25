"""The kill switch (spec §8.6). Checked before every publish, including each post of a chain.

ON if either the `KILL_SWITCH` env var is true or the dashboard's flag is set. If the flag cannot be
read (database down), it is treated as ON: when in doubt, nothing publishes.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .db.models import Flag

FLAG = "kill_switch"


class KillSwitchOn(RuntimeError):
    pass


def is_on(session: Session, env_value: bool) -> bool:
    if env_value:
        return True
    try:
        flag = session.get(Flag, FLAG)
    except SQLAlchemyError:
        return True
    return flag is not None and flag.value == "on"


def ensure_off(session: Session, env_value: bool) -> None:
    if is_on(session, env_value):
        raise KillSwitchOn("Kill switch is ON; publishing halted")


def set_switch(session: Session, on: bool, by: str, now: datetime) -> None:
    flag = session.get(Flag, FLAG)
    value = "on" if on else "off"
    if flag is None:
        session.add(Flag(name=FLAG, value=value, updated_at=now, updated_by=by))
    else:
        flag.value, flag.updated_at, flag.updated_by = value, now, by
    session.flush()
