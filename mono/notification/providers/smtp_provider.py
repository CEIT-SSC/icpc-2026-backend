import smtplib
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

    def send(self, *, to: str, subject: str, html: str, text: str | None = None) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = to

        if text:
            msg.attach(MIMEText(text, "plain", _charset="utf-8"))
        msg.attach(MIMEText(html, "html", _charset="utf-8"))

        server = smtplib.SMTP_SSL(self.host, self.port, timeout=20)
        try:
            server.login(self.user, self.password)
            server.sendmail(self.from_addr, [to], msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass
