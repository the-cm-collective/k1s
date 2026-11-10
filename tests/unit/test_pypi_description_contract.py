from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_readme_uses_absolute_public_image_urls() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for image in (
        "media/style/web-logos/k1s-killa-bee-refresh-0526.png",
        "media/docs.home.arpa_8443_dashboard.png",
        "media/docs.home.arpa_8443_playground.html.png",
    ):
        assert (
            f"https://raw.githubusercontent.com/the-cm-collective/k1s/main/{image}" in readme
        )
    assert 'src="media/' not in readme
    assert "](media/" not in readme
