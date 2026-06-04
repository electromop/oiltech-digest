import json
from pathlib import Path
from jinja2 import Template

base = Path(__file__).resolve().parent
content = json.loads((base / "digest_content.json").read_text(encoding="utf-8"))
template = Template((base / "email_template.html").read_text(encoding="utf-8"))
html = template.render(**content)
(base / "email_ready.html").write_text(html, encoding="utf-8")
print("Created:", base / "email_ready.html")
