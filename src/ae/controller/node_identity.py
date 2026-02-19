from __future__ import annotations


def scoped_node_id(site_id: str | None, node_id: str) -> str:
    site = (site_id or "").strip()
    node = node_id.strip()
    if not site:
        return node
    prefix = f"{site}--"
    if node.startswith(prefix):
        return node
    return f"{prefix}{node}"
