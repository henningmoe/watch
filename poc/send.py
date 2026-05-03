"""Sending the rendered email via Microsoft Graph API."""


def send_mail(html: str, recipients: list[str], subject: str) -> None:
    """Send *html* email to *recipients* with *subject* via Microsoft Graph."""
    raise NotImplementedError


if __name__ == "__main__":
    send_mail("<h1>Test</h1>", ["test@example.com"], "Cermaq Media Watch")
