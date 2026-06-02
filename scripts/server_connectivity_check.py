#!/usr/bin/env python3
"""Server-side connectivity diagnostic for OilTech Digest sources.

Run this ON the server (e.g. Timeweb) to tell apart three failure modes:
  * controls fail too        -> no outbound internet / DNS broken (infra/firewall)
  * controls OK, many 403    -> datacenter IP blocked by sites (need residential/rotating proxy)
  * most sources return 200  -> connectivity fine; look at app logic/logs

Usage (from repo root):  python scripts/server_connectivity_check.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

# Use the exact headers the app sends, if importable; otherwise a plain browser UA.
try:
    from oiltech_digest.ingestion import http_client

    HEADERS = http_client._DEFAULT_HEADERS
except Exception:  # noqa: BLE001
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
    }

CONTROLS = [
    "https://example.com",
    "https://www.google.com",
    "https://api.openai.com/v1/models",
]

BUILTIN_SOURCES = [
    "https://www.rigzone.com/news/rss/rigzone_latest.aspx",
    "https://www.lngindustry.com/rss/lngindustry.xml",
    "https://www.worldoil.com",
    "https://www.upstreamonline.com",
    "https://www.iea.org",
    "https://www.opec.org",
    "https://www.halliburton.com",
    "https://www.bakerhughes.com",
]


def sample_sources(limit: int = 15) -> list[str]:
    try:
        from oiltech_digest.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COALESCE(rss_url, url) FROM sources "
                "WHERE enabled AND COALESCE(rss_url, url) LIKE 'http%%' ORDER BY id LIMIT %s",
                (limit,),
            )
            urls = [row[0] for row in cur.fetchall()]
            if urls:
                return urls
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] DB unavailable ({exc}); using built-in sample", flush=True)
    return BUILTIN_SOURCES


def resolve(host: str) -> str:
    try:
        return socket.gethostbyname(host)
    except Exception as exc:  # noqa: BLE001
        return f"DNS_FAIL: {exc}"


def check(url: str, proxies: dict | None = None) -> dict:
    host = urlsplit(url).netloc
    ip = resolve(host)
    row: dict = {"url": url[:60], "ip": ip}
    if str(ip).startswith("DNS_FAIL"):
        row["status"] = "DNS_FAIL"
        return row
    t0 = time.time()
    try:
        r = requests.get(
            url,
            headers=HEADERS,
            timeout=25 if proxies else 15,
            allow_redirects=True,
            proxies=proxies,
        )
        row["status"] = r.status_code
        row["bytes"] = len(r.content)
    except Exception as exc:  # noqa: BLE001
        row["status"] = "ERR"
        row["err"] = type(exc).__name__ + ": " + str(exc)[:80]
    row["secs"] = round(time.time() - t0, 1)
    return row


def proxy_from_env() -> dict | None:
    """Read PROXY_URL from the environment (same var the app uses). None if unset."""
    url = os.environ.get("PROXY_URL", "").strip()
    return {"http": url, "https": url} if url else None


def mask_proxy(url: str) -> str:
    """Hide credentials in a proxy URL so it is safe to print."""
    parts = urlsplit(url)
    cred = "***@" if (parts.username or parts.password) else ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{cred}{parts.hostname or ''}{port}"


def main() -> None:
    print("=== CONTROLS (basic egress + DNS) ===", flush=True)
    controls = [check(u) for u in CONTROLS]
    for r in controls:
        print(json.dumps(r, ensure_ascii=False), flush=True)

    srcs = sample_sources()
    print("\n=== SOURCES (direct) ===", flush=True)
    rows = [check(u) for u in srcs]
    for r in rows:
        print(json.dumps(r, ensure_ascii=False), flush=True)

    proxies = proxy_from_env()
    prows: list[dict] = []
    if proxies:
        print(f"\n=== SOURCES (via proxy {mask_proxy(proxies['http'])}) ===", flush=True)
        prows = [check(u, proxies=proxies) for u in srcs]
        for r in prows:
            print(json.dumps(r, ensure_ascii=False), flush=True)

    # Any HTTP status (even 401/403) means egress works; ERR/DNS_FAIL means it does not.
    ctrl_reach = sum(1 for r in controls if isinstance(r.get("status"), int))
    ok = sum(1 for r in rows if r.get("status") == 200)
    forbidden = sum(1 for r in rows if r.get("status") == 403)
    dns_fail = sum(1 for r in rows if r.get("status") == "DNS_FAIL")

    print(
        f"\nSUMMARY (direct): controls reachable {ctrl_reach}/{len(controls)} (any HTTP reply) | "
        f"sources {ok}/{len(rows)} OK(200), {forbidden}x403, {dns_fail}xDNS_FAIL",
        flush=True,
    )
    if proxies:
        pok = sum(1 for r in prows if r.get("status") == 200)
        pforbidden = sum(1 for r in prows if r.get("status") == 403)
        gained = sum(
            1 for d, p in zip(rows, prows) if d.get("status") != 200 and p.get("status") == 200
        )
        print(
            f"SUMMARY (proxy):  sources {pok}/{len(prows)} OK(200), {pforbidden}x403 | "
            f"unblocked by proxy: {gained}",
            flush=True,
        )
    print("Interpretation:", flush=True)
    print("  controls fail too        -> no outbound internet / DNS broken (firewall/infra)", flush=True)
    print("  controls OK, many 403    -> datacenter IP blocked by sites (residential/rotating proxy)", flush=True)
    print("  most sources 200         -> connectivity fine; inspect app logic/logs", flush=True)
    if proxies:
        print("  proxy unblocks 403s      -> proxy works; keep PROXY_URL in server .env", flush=True)


if __name__ == "__main__":
    main()
