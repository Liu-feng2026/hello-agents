"""National Healthcare Security Administration — designated medical institutions.
Verified compact replay collector (public endpoint, 2026-09-26).

Usage:
    python nhsa_medical_collector.py --area-code 110000 --output beijing.jsonl
    python nhsa_medical_collector.py --all --output institutions.jsonl
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests

URL = "https://fuwu.nhsa.gov.cn/ebus/fuwu/api/nthl/api/OutMed/queryMedInsOrg"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "channel": "web",
    "Origin": "https://fuwu.nhsa.gov.cn",
    "Referer": "https://fuwu.nhsa.gov.cn/nationalHallSt/",
    "User-Agent": "Mozilla/5.0 (compatible; public-data-collector/1.0)",
}


def fetch_page(session: requests.Session, page_no: int, page_size: int, **filters: str) -> dict:
    # The service requires only fixmedinsType, pageNo, and pageSize. Empty optional
    # search fields are deliberately omitted rather than sent as null.
    payload = {"fixmedinsType": "1", "pageNo": page_no, "pageSize": page_size}
    payload.update({key: value for key, value in filters.items() if value not in (None, "")})
    response = session.post(URL, headers=HEADERS, json={"data": payload}, timeout=30)
    response.raise_for_status()
    result = response.json()
    if result.get("code") != 0:
        raise RuntimeError(f"API error: {result}")
    return result["data"]


def collect(output: Path, page_size: int, delay: float, **filters: str) -> int:
    session = requests.Session()
    # Establishes the same first-party anti-bot cookie that a normal navigation gets.
    session.get("https://fuwu.nhsa.gov.cn/nationalHallSt/", headers=HEADERS, timeout=30)

    count = 0
    page_no = 1
    total = None
    with output.open("w", encoding="utf-8") as fp:
        while True:
            data = fetch_page(session, page_no, page_size, **filters)
            rows = data.get("organization", [])
            total = data.get("total", total)
            if not rows:
                break
            for row in rows:
                fp.write(json.dumps(row, ensure_ascii=False) + "\n")
                count += 1
            print(f"page={page_no} rows={len(rows)} collected={count}/{total}")
            if len(rows) < page_size:
                break
            page_no += 1
            time.sleep(delay)
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("nhsa_medical.jsonl"))
    parser.add_argument("--area-code", default="", help="six-digit administrative area code")
    parser.add_argument("--name", default="", help="institution-name substring")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--delay", type=float, default=0.35)
    parser.add_argument("--all", action="store_true", help="explicitly acknowledge nationwide collection")
    args = parser.parse_args()
    if not args.all and not args.area_code and not args.name:
        parser.error("supply --area-code or --name; use --all only for nationwide collection")
    if not 1 <= args.page_size <= 100:
        parser.error("--page-size must be in 1..100")
    filters = {"areaCode": args.area_code, "hisName": args.name}
    print(f"wrote {collect(args.output, args.page_size, args.delay, **filters)} rows to {args.output}")


if __name__ == "__main__":
    main()
