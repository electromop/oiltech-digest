"""Monthly digest draft generation."""

from __future__ import annotations

import json
from pathlib import Path

from oiltech_digest.db import repository


def build_digest_content(month: str, limit: int = 20, min_score: float = 60) -> dict:
    rows = repository.digest_candidates(month=month, limit=limit, min_score=min_score)
    items = []
    for row in rows:
        tag = row.get("tag_name") or "Без тега"
        if row.get("parent_tag_name"):
            tag = f"{row['parent_tag_name']} / {tag}"
        items.append(
            {
                "title": row["title"],
                "source": row["source_name"],
                "url": row["url"],
                "published_at": row["published_at"].isoformat() if row.get("published_at") else None,
                "tag": tag,
                "score": float(row["total_score"]) if row.get("total_score") is not None else None,
                "score_label": row.get("score_label"),
                "summary": row.get("summary") or "",
            }
        )
    return {
        "month": month,
        "title": f"OilTech Digest · {month}",
        "items": items,
    }


def write_digest_content(path: str | Path, month: str, limit: int = 20,
                         min_score: float = 60) -> dict:
    content = build_digest_content(month=month, limit=limit, min_score=min_score)
    output_path = Path(path)
    output_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(output_path), "items": len(content["items"])}

