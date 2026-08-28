import smtplib
from email.utils import formataddr, parseaddr
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from django.conf import settings
from .base import EmailProvider


class SmtpEmailProvider(EmailProvider):
    def __init__(self):
        self.host = settings.EMAIL_HOST
        self.port = settings.EMAIL_PORT
        self.user = settings.EMAIL_HOST_USER
        self.password = settings.EMAIL_HOST_PASSWORD
        self.use_ssl = settings.EMAIL_USE_SSL
        self.from_addr = settings.EMAIL_DEFAULT_FROM
        self.from_name, self.envelope_from = parseaddr(self.from_addr)
        if "@" not in self.envelope_from:
            raise ValueError(
                "EMAIL_DEFAULT_FROM must be an email address, optionally formatted as 'Name <email>'"
            )

    def send(self, *, to: str, subject: str, html: str, text: str | None = None) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = formataddr((self.from_name, self.envelope_from))
        msg["To"] = to

        if text:
            msg.attach(MIMEText(text, "plain", _charset="utf-8"))
        msg.attach(MIMEText(html, "html", _charset="utf-8"))

        server = smtplib.SMTP_SSL(self.host, self.port, timeout=20)
        try:
            server.login(self.user, self.password)
            server.sendmail(self.envelope_from, [to], msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass
