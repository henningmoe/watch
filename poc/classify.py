"""AI-based article classification using the Claude API."""


def classify(article: dict) -> dict:
    """Classify a single article and return it enriched with relevance metadata."""
    raise NotImplementedError


if __name__ == "__main__":
    sample = {"title": "Test article", "url": "https://example.com", "content": ""}
    print(classify(sample))
