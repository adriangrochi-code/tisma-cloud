"""E-mail of the master: the one-time codes to confirm an account and to choose a new password.

Sent through the SMTP server of the domain (TISMA_SMTP_*), with STARTTLS or SSL. Without a server (development) the
messages are only logged.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

log = logging.getLogger("tisma_cloud.mail")


class Mailer:
    def send(self, to: str, subject: str, text: str) -> None:
        raise NotImplementedError


class LogMailer(Mailer):
    """Development: the message goes to the log instead of being sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, text: str) -> None:
        self.sent.append((to, subject, text))
        log.warning("E-mail not sent (no TISMA_SMTP_HOST): to %s: %s\n%s", to, subject, text)


class SmtpMailer(Mailer):
    def __init__(self, host: str, port: int, security: str, user: str, password: str, sender: str) -> None:
        self.host, self.port, self.security = host, port, security
        self.user, self.password, self.sender = user, password, sender

    def send(self, to: str, subject: str, text: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=False)
        msg["Message-ID"] = make_msgid(domain=self.sender.rsplit("@", 1)[-1].strip("> ") or None)
        msg.set_content(text)
        context = ssl.create_default_context()
        if self.security == "ssl":
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=20, context=context)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=20)
        with server:
            if self.security == "starttls":
                server.starttls(context=context)
            if self.user:
                server.login(self.user, self.password)
            server.send_message(msg)


def mailer_for(settings) -> Mailer:  # noqa: ANN001
    if not settings.smtp_host:
        return LogMailer()
    return SmtpMailer(settings.smtp_host, settings.smtp_port, settings.smtp_security, settings.smtp_user,
                      settings.smtp_password, settings.smtp_from)


def code_message(purpose: str, code: str) -> tuple[str, str]:
    """Subject and text of the e-mail of a code (Spanish: the users are in Uruguay)."""
    if purpose == "verify":
        return ("Tu código para confirmar la cuenta de Tisma Cloud",
                f"Hola:\n\nTu código para confirmar la cuenta de Tisma Cloud es:\n\n    {code}\n\n"
                "Vence en 15 minutos. Si no creaste una cuenta, ignorá este correo.\n\nTisma Cloud")
    if purpose == "delete":
        return ("Tu código para borrar la cuenta de Tisma Cloud",
                f"Hola:\n\nTu código para borrar tu cuenta de Tisma Cloud y todos sus datos es:\n\n    {code}\n\n"
                "Vence en 15 minutos. Si no lo pediste, ignorá este correo y cambiá tu contraseña: tu cuenta no se "
                "borra sin este código.\n\nTisma Cloud")
    return ("Tu código para cambiar la contraseña de Tisma Cloud",
            f"Hola:\n\nTu código para elegir una contraseña nueva en Tisma Cloud es:\n\n    {code}\n\n"
            "Vence en 15 minutos. Si no lo pediste, ignorá este correo: tu contraseña no cambia.\n\nTisma Cloud")


def reward_message(title: str, days: int, until: str) -> tuple[str, str]:
    """A report of the user helped to improve Tisma: the days of the plan it earned."""
    return ("Gracias por tu informe: ganaste días de Tisma Cloud",
            f"Hola:\n\nTu informe «{title}» nos ayudó a mejorar Tisma. Como agradecimiento sumamos {days} días a tu plan "
            f"de Tisma Cloud: ahora está pago hasta el {until}.\n\nGracias por avisarnos.\n\nTisma Cloud")


def exists_message() -> tuple[str, str]:
    """Someone tried to make an account with an address that has one (the page answers the same in both cases, so
    that it does not tell which addresses have an account)."""
    return ("Ya tenés una cuenta de Tisma Cloud",
            "Hola:\n\nAlguien intentó crear una cuenta de Tisma Cloud con esta dirección, que ya tiene una. Si fuiste "
            "vos, entrá con tu contraseña, o elegí una nueva con \"Olvidé mi contraseña\". Si no fuiste vos, ignorá "
            "este correo: tu cuenta no cambia.\n\nTisma Cloud")
