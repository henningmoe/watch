"""Rendering classified articles into HTML or email."""

from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def render_html(articles: list[dict]) -> str:
    """Render *articles* into a complete HTML page string."""
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=True)
    template = env.get_template("feed.html")
    return template.render(
        articles=articles,
        today=date.today().strftime("%d. %B %Y"),
    )


def render_email(articles: list[dict]) -> str:
    """Render *articles* into a complete HTML email string."""
    raise NotImplementedError


if __name__ == "__main__":
    print(render_html([]))
