from pathlib import Path


def test_dashboard_does_not_use_removed_preview_keyword():
    root = Path(__file__).resolve().parents[1]
    for relative in ("plugins/start.py", "plugins/menu.py"):
        source = (root / relative).read_text(encoding="utf-8")
        assert "disable_web_page_preview" not in source, relative
