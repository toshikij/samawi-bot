"""Alert and report email (spec §8: email to the owner only)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger("samawi.alerts")


class Mailer(Protocol):
    def send(self, subject: str, body: str) -> None: ...


@dataclass
class LogMailer:
    """Local/dev: alerts go to the log, and are kept for tests."""

    sent: list[tuple[str, str]] = field(default_factory=list)

    def send(self, subject: str, body: str) -> None:
        self.sent.append((subject, body))
        log.warning("ALERT %s\n%s", subject, body)


@dataclass
class SesMailer:
    client: Any
    sender: str
    to: str

    def send(self, subject: str, body: str) -> None:
        self.client.send_email(
            Source=self.sender,
            Destination={"ToAddresses": [self.to]},
            Message={"Subject": {"Data": f"[SAMAWI] {subject}"[:200]}, "Body": {"Text": {"Data": body}}},
        )


def safe_send(mailer: Mailer, subject: str, body: str) -> bool:
    """Alerts must never take a job down with them."""
    try:
        mailer.send(subject, body)
        return True
    except Exception:
        log.exception("could not send alert %s", subject)
        return False
