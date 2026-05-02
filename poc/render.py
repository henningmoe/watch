"""Rendering classified articles into an HTML email via MJML."""


def render_email(articles: list[dict]) -> str:
    """Render *articles* into a complete HTML email string."""
    raise NotImplementedError


if __name__ == "__main__":
    print(render_email([]))
