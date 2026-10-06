from __future__ import annotations

import smtplib
from email.message import EmailMessage

from flask import current_app

from app.models import User


def send_delivery_email(user: User, subject: str, body: str) -> bool:
    if not user.email_delivery_notifications:
        return False
    host = str(current_app.config.get("SMTP_HOST") or "").strip()
    sender = str(current_app.config.get("SMTP_FROM") or "").strip()
    if not host or not sender:
        return False
    message = EmailMessage()
    message["From"] = sender
    message["To"] = user.email
    message["Subject"] = subject[:300]
    message.set_content(body)
    try:
        with smtplib.SMTP(host, int(current_app.config["SMTP_PORT"]), timeout=10) as smtp:
            if current_app.config.get("SMTP_USE_TLS"):
                smtp.starttls()
            username = str(current_app.config.get("SMTP_USERNAME") or "")
            password = str(current_app.config.get("SMTP_PASSWORD") or "")
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException):
        current_app.logger.warning("delivery_email_failed user_id=%s", user.id)
        return False
    return True
