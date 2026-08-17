#!/usr/bin/env python3
"""Brave Search에서 공개 웹앱 후보를 찾고 기존 Supabase 스캐너로 넘긴다."""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests


BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
DEFAULT_ALLOWED_DOMAINS = (
    "vercel.app",
    "netlify.app",
    "pages.dev",
    "web.app",
    "firebaseapp.com",
    "onrender.com",
    "railway.app",
    "lovable.app",
)
SKIP_PATH_PARTS = {
    "blog", "blogs", "doc", "docs", "documentation", "guide", "guides",
    "tutorial", "tutorials", "template", "templates", "example", "examples",
    "changelog", "showcase",
}
DISCOVERY_FIELDS = (
    "discovered_utc", "query", "search_offset", "url", "domain", "source"
)
REQUEST_FIELDS = (
    "requested_utc", "query", "search_offset", "request_mode", "status",
    "result_count", "error_type",
)
REPORT_FIELDS = (
    "scanned_utc", "url", "domain", "review_priority", "status",
    "detected_types", "type_counts", "confidence_counts",
    "service_role_candidate", "secret_key_candidate", "review_reason",
    "analyst_next_step",
)


class ApiBudgetExhausted(RuntimeError):
    pass


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_queries(path: Path) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        query = raw.strip()
        if not query or query.startswith("#") or query in seen:
            continue
        seen.add(query)
        queries.append(query)
    return queries


def normalize_url(value: str) -> str | None:
    """추적용 query/fragment를 제거해 동일 페이지 중복을 줄인다."""
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    host = parts.hostname.lower().rstrip(".")
    port = parts.port
    netloc = host if port is None else f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, "", ""))


def is_allowed_candidate(url: str, allowed_domains: tuple[str, ...]) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if not any(host == domain or host.endswith("." + domain) for domain in allowed_domains):
        return False
    path_parts = {part.lower() for part in parts.path.split("/") if part}
    return not bool(path_parts & SKIP_PATH_PARTS)


def relax_query(query: str) -> str:
    """0건 검색식에서 정확 구문과 제외 조건만 완화한다."""
    relaxed = re.sub(r"\s+-(?:inurl:[^\s]+|template|github)", "", query)
    relaxed = relaxed.replace('"', "").replace("(", "").replace(")", "")
    return " ".join(relaxed.split())


def has_supabase_anchor(query: str) -> bool:
    """기능 문구만 남는 광범위한 완화 검색을 막는다."""
    lowered = query.lower()
    return any(
        anchor in lowered
        for anchor in ("supabase.co", "supabase", "powered by supabase", "built with supabase")
    )


def brave_search(
    session: requests.Session,
    api_key: str,
    query: str,
    count: int,
    country: str,
    offset: int = 0,
) -> list[str]:
    response = session.get(
        BRAVE_ENDPOINT,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        },
        params={
            "q": query, "count": count, "country": country,
            "offset": offset, "result_filter": "web",
        },
        timeout=20,
    )
    if response.status_code in {401, 403}:
        raise RuntimeError("Brave API 키 또는 구독 상태를 확인하세요(HTTP 인증 실패).")
    if response.status_code == 429:
        raise RuntimeError("Brave API 요청 한도에 도달했습니다(HTTP 429).")
    response.raise_for_status()
    payload = response.json()
    results = payload.get("web", {}).get("results", [])
    return [item["url"] for item in results if isinstance(item, dict) and item.get("url")]


def load_existing_urls(path: Path) -> set[str]:
    if not path.exists():
        return set()
    values: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        if raw.strip() and not raw.lstrip().startswith("#"):
            normalized = normalize_url(raw)
            if normalized:
                values.add(normalized)
    return values


def append_urls(path: Path, urls: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8-sig") if path.exists() else "# Brave Search가 찾은 공개 URL\n"
    if old and not old.endswith("\n"):
        old += "\n"
    text = old + "".join(f"{url}\n" for url in urls)
    path.write_text(text, encoding="utf-8")


def read_discoveries(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_discoveries(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8-sig", newline="", delete=False,
            dir=path.parent, prefix=path.name + ".", suffix=".tmp",
        ) as handle:
            temp_name = handle.name
            writer = csv.DictWriter(handle, fieldnames=DISCOVERY_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_name, path)
        temp_name = None
    except PermissionError as exc:
        raise RuntimeError(f"{path} 파일을 Excel에서 닫고 다시 실행하세요.") from exc
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def read_request_log(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def request_key(query: str, offset: int, mode: str) -> str:
    return f"{offset}|{mode}|{query}"


def write_request_log(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8-sig", newline="", delete=False,
            dir=path.parent, prefix=path.name + ".", suffix=".tmp",
        ) as handle:
            temp_name = handle.name
            writer = csv.DictWriter(handle, fieldnames=REQUEST_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_name, path)
        temp_name = None
    except PermissionError as exc:
        raise RuntimeError(f"{path} 파일을 Excel에서 닫고 다시 실행하세요.") from exc
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def search_with_budget(
    args: argparse.Namespace,
    session: requests.Session,
    api_key: str,
    query: str,
    mode: str,
) -> dict:
    key = request_key(query, args.search_offset, mode)
    previous = args._completed_requests.get(key)
    if previous:
        return {
            "urls": [], "skipped": True,
            "result_count": int(previous.get("result_count", "0") or 0),
            "record": None,
        }
    if args.api_request_budget and args._api_requests_made >= args.api_request_budget:
        raise ApiBudgetExhausted(
            f"이번 실행의 Brave API 요청 예산 {args.api_request_budget}회에 도달했습니다."
        )
    args._api_requests_made += 1
    try:
        urls = brave_search(
            session, api_key, query, args.results_per_query,
            args.country, args.search_offset,
        )
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        record = {
            "requested_utc": now_utc(), "query": query,
            "search_offset": str(args.search_offset), "request_mode": mode,
            "status": "ERROR", "result_count": "0",
            "error_type": type(exc).__name__,
        }
        args._request_rows.append(record)
        write_request_log(args.request_log, args._request_rows)
        raise
    return {
        "urls": urls, "skipped": False, "result_count": len(urls),
        "record": {
            "requested_utc": now_utc(), "query": query,
            "search_offset": str(args.search_offset), "request_mode": mode,
            "status": "OK", "result_count": str(len(urls)), "error_type": "",
        },
    }


def commit_request_records(args: argparse.Namespace, records: list[dict[str, str]]) -> None:
    if not records:
        return
    args._request_rows.extend(records)
    for row in records:
        if row.get("status") == "OK":
            key = request_key(
                row.get("query", ""), int(row.get("search_offset", "0") or 0),
                row.get("request_mode", "base"),
            )
            args._completed_requests[key] = row
    write_request_log(args.request_log, args._request_rows)


def discover_once(args: argparse.Namespace, api_key: str) -> tuple[int, int]:
    queries = load_queries(args.queries)[: args.max_queries]
    if not queries:
        raise RuntimeError("검색어 파일에 사용할 검색어가 없습니다.")

    allowed = tuple(domain.lower().lstrip(".") for domain in args.allowed_domain)
    input_urls = load_existing_urls(args.input)
    discoveries = read_discoveries(args.discovery_csv)
    discovered_urls = {normalize_url(row.get("url", "")) for row in discoveries}
    new_urls: list[str] = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Public-Interest-Supabase-Research/0.3"})

    successful_queries = 0
    relaxed_retries = 0
    for index, query in enumerate(queries, 1):
        if len(new_urls) >= args.max_new_urls:
            break
        try:
            base_outcome = search_with_budget(args, session, api_key, query, "base")
            result_urls = base_outcome["urls"]
            request_records = [base_outcome["record"]] if base_outcome["record"] else []
            if not base_outcome["skipped"]:
                successful_queries += 1
            elif base_outcome["result_count"] > 0:
                logging.info("검색 [%d/%d] 이미 완료되어 건너뜀", index, len(queries))
                continue
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            if isinstance(exc, ApiBudgetExhausted):
                raise
            logging.error("검색 실패 [%d/%d]: %s", index, len(queries), exc)
            continue

        query_used = query
        if not result_urls and args.relax_zero_results and has_supabase_anchor(query):
            relaxed = relax_query(query)
            if relaxed != query:
                try:
                    relaxed_outcome = search_with_budget(
                        args, session, api_key, relaxed, "relaxed"
                    )
                    result_urls = relaxed_outcome["urls"]
                    if relaxed_outcome["record"]:
                        request_records.append(relaxed_outcome["record"])
                    if not relaxed_outcome["skipped"]:
                        successful_queries += 1
                        relaxed_retries += 1
                    query_used = relaxed
                    logging.info("검색 [%d/%d] 0건 → 완화 검색 1회", index, len(queries))
                except (requests.RequestException, ValueError, RuntimeError) as exc:
                    if isinstance(exc, ApiBudgetExhausted):
                        commit_request_records(args, request_records)
                        raise
                    logging.error("완화 검색 실패 [%d/%d]: %s", index, len(queries), exc)

        accepted = 0
        filtered = 0
        duplicates = 0
        query_urls: list[str] = []
        query_rows: list[dict[str, str]] = []
        for raw_url in result_urls:
            url = normalize_url(raw_url)
            if not url or not is_allowed_candidate(url, allowed):
                filtered += 1
                continue
            if url in input_urls or url in new_urls:
                duplicates += 1
                continue
            new_urls.append(url)
            query_urls.append(url)
            input_urls.add(url)
            accepted += 1
            if url not in discovered_urls:
                query_rows.append({
                    "discovered_utc": now_utc(),
                    "query": query_used,
                    "search_offset": str(args.search_offset),
                    "url": url,
                    "domain": urlsplit(url).netloc,
                    "source": "Brave Search API",
                })
                discovered_urls.add(url)
            if len(new_urls) >= args.max_new_urls:
                break
            if accepted >= args.max_new_per_query:
                break
        # 검색어마다 체크포인트를 남겨 Ctrl+C나 오류가 나도 앞선 결과를 보존한다.
        if query_rows:
            discoveries.extend(query_rows)
            write_discoveries(args.discovery_csv, discoveries)
        if query_urls:
            append_urls(args.input, query_urls)
        commit_request_records(args, request_records)
        logging.info(
            "검색 [%d/%d] 결과 %d개, 새 후보 %d개, 필터 제외 %d개, 중복 %d개",
            index, len(queries), len(result_urls), accepted, filtered, duplicates,
        )
        if index < len(queries):
            time.sleep(args.search_delay)

    logging.info(
        "Brave API 요청 %d회 성공(완화 재검색 %d회), 새 URL 총 %d개",
        successful_queries, relaxed_retries, len(new_urls),
    )
    return successful_queries, len(new_urls)


def run_scanner(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(Path(__file__).with_name("supabase_url_scanner.py")),
        "--input", str(args.input),
        "--rules", str(args.rules),
        "--output-dir", str(args.output_dir),
        "--delay", str(args.scan_delay),
    ]
    return subprocess.run(command, check=False).returncode


def build_excel_report(args: argparse.Namespace) -> int:
    """Codex 번들 artifact-tool로 필터 가능한 Excel 통합 보고서를 만든다."""
    node_path = Path(
        os.environ.get(
            "CODEX_ARTIFACT_NODE",
            str(
                Path.home()
                / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
            ),
        )
    )
    builder = Path(__file__).with_name("tools") / "build_report_workbook.mjs"
    module_link = Path(__file__).with_name("node_modules")
    if not node_path.exists() or not builder.exists() or not module_link.exists():
        logging.warning(
            "Excel 자동 갱신 환경을 찾지 못해 CSV만 갱신했습니다. README의 Excel 설정을 확인하세요."
        )
        return 0
    command = [str(node_path), str(builder), str(args.excel_output)]
    completed = subprocess.run(
        command, cwd=Path(__file__).parent, check=False, capture_output=True, text=True,
    )
    if completed.returncode != 0:
        logging.error("Excel 보고서 생성 실패: %s", completed.stderr.strip()[-1000:])
        return completed.returncode
    logging.info("필터 가능한 Excel 보고서 갱신: %s", args.excel_output)
    return 0


def build_report_queue(summary_path: Path, report_path: Path) -> int:
    """사람이 우선 확인할 행만 별도 CSV로 만든다."""
    if not summary_path.exists() or summary_path.stat().st_size == 0:
        return 0
    with summary_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [
            row for row in csv.DictReader(handle)
            if row.get("status") in {"REVIEW_REQUIRED", "LOW_CONFIDENCE_MATCHES"}
        ]
    # URL별 최신 요약만 남겨 누적 파일에서 동일 후보가 중복되지 않게 보장한다.
    rows = list({row.get("url", ""): row for row in rows if row.get("url")}.values())
    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "NONE": 3}
    rows.sort(key=lambda row: (rank.get(row.get("review_priority", "NONE"), 9), row.get("url", "")))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8-sig", newline="", delete=False,
            dir=report_path.parent, prefix=report_path.name + ".", suffix=".tmp",
        ) as handle:
            temp_name = handle.name
            writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_name, report_path)
        temp_name = None
    except PermissionError as exc:
        raise RuntimeError(f"{report_path} 파일을 Excel에서 닫고 다시 실행하세요.") from exc
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
    logging.info("보고서 검토 후보 %d개(고유 URL %d개): %s", len(rows), len(rows), report_path)
    return len(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Brave Search 후보 수집 → Supabase 공개 페이지 스캔 → CSV 누적"
    )
    parser.add_argument("--queries", type=Path, default=Path("input/search_queries.txt"))
    parser.add_argument("--input", type=Path, default=Path("input/urls.txt"))
    parser.add_argument("--discovery-csv", type=Path, default=Path("output/discovery_results.csv"))
    parser.add_argument("--report-csv", type=Path, default=Path("output/report_candidates.csv"))
    parser.add_argument(
        "--excel-output", type=Path, default=Path("output/supabase_scan_report.xlsx")
    )
    parser.add_argument("--request-log", type=Path, default=Path("output/search_request_log.csv"))
    parser.add_argument("--rules", type=Path, default=Path("rules"))
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--max-queries", type=int, default=12)
    parser.add_argument("--results-per-query", type=int, default=10)
    parser.add_argument("--max-new-urls", type=int, default=40)
    parser.add_argument("--max-new-per-query", type=int, default=5)
    parser.add_argument("--country", default="KR")
    parser.add_argument("--search-offset", type=int, default=0)
    parser.add_argument("--search-delay", type=float, default=1.0)
    parser.add_argument("--scan-delay", type=float, default=1.5)
    parser.add_argument("--interval-hours", type=float, default=0)
    parser.add_argument(
        "--api-request-budget", type=int, default=0,
        help="이번 프로세스에서 허용할 Brave API 실제 요청 수(0은 제한 없음)",
    )
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument(
        "--relax-zero-results", action="store_true",
        help="0건 검색식에 Supabase 단서가 있을 때만 따옴표·제외 조건을 완화해 한 번 재검색",
    )
    parser.add_argument(
        "--tomorrow-mode", action="store_true",
        help="보고서 마감용: 고수익 검색어 64개와 결과 20개/요청을 사용",
    )
    parser.add_argument(
        "--overnight-mode", action="store_true",
        help="offset 1~9를 순회하며 요청 예산 내에서 검색·스캔·보고서 갱신",
    )
    parser.add_argument(
        "--allowed-domain", action="append", default=None,
        help="허용할 호스팅 도메인(여러 번 지정 가능)",
    )
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args()
    if args.tomorrow_mode or args.overnight_mode:
        args.queries = Path(__file__).with_name("input") / "search_queries_tomorrow.txt"
        args.max_queries = 70
        args.results_per_query = 20
        args.max_new_urls = 128
        args.max_new_per_query = 2
        args.search_delay = 0.5
        args.scan_delay = 1.0
    if args.overnight_mode and not args.api_request_budget:
        args.api_request_budget = 700
    args.allowed_domain = args.allowed_domain or list(DEFAULT_ALLOWED_DOMAINS)
    if not 1 <= args.max_queries <= 100:
        raise SystemExit("--max-queries는 1~100이어야 합니다.")
    if not 1 <= args.results_per_query <= 20:
        raise SystemExit("--results-per-query는 1~20이어야 합니다.")
    if args.max_new_urls < 1 or args.max_new_per_query < 1 or args.search_delay < 0 or args.scan_delay < 1:
        raise SystemExit("URL 제한과 지연 시간을 확인하세요(--scan-delay는 1초 이상).")
    if not 0 <= args.search_offset <= 9:
        raise SystemExit("--search-offset은 0~9여야 합니다.")
    if args.api_request_budget < 0:
        raise SystemExit("--api-request-budget은 0 이상이어야 합니다.")
    if args.interval_hours and args.interval_hours < 1:
        raise SystemExit("--interval-hours는 0 또는 1 이상이어야 합니다.")

    api_key = os.environ.get("BRAVE_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            'BRAVE_API_KEY가 없습니다. PowerShell에서 '
            '$env:BRAVE_API_KEY="발급받은-키" 를 먼저 실행하세요.'
        )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args._api_requests_made = 0
    args._request_rows = read_request_log(args.request_log)
    args._completed_requests = {
        request_key(
            row.get("query", ""), int(row.get("search_offset", "0") or 0),
            row.get("request_mode", "base"),
        ): row
        for row in args._request_rows if row.get("status") == "OK"
    }

    if args.overnight_mode:
        for offset in range(1, 10):
            args.search_offset = offset
            logging.info(
                "야간 조사 offset=%d 시작 (이번 프로세스 요청 %d/%d)",
                offset, args._api_requests_made, args.api_request_budget,
            )
            budget_hit = False
            try:
                successful, _ = discover_once(args, api_key)
            except ApiBudgetExhausted as exc:
                logging.info("%s", exc)
                successful = 0
                budget_hit = True
            if not args.discover_only:
                if run_scanner(args) != 0:
                    logging.error("페이지 스캐너가 오류 코드로 종료되었습니다.")
                else:
                    build_report_queue(args.output_dir / "batch_summary.csv", args.report_csv)
                    build_excel_report(args)
            if budget_hit:
                break
        logging.info(
            "야간 조사 종료: 이번 프로세스 Brave API 실제 요청 %d회 (현재 단가 환산 약 $%.2f)",
            args._api_requests_made, args._api_requests_made * 0.005,
        )
        return 0

    while True:
        try:
            successful, _ = discover_once(args, api_key)
            if successful == 0:
                logging.error("성공한 Brave 검색이 없어 이번 스캔을 건너뜁니다.")
            elif not args.discover_only:
                if run_scanner(args) != 0:
                    logging.error("페이지 스캐너가 오류 코드로 종료되었습니다.")
                else:
                    build_report_queue(
                        args.output_dir / "batch_summary.csv", args.report_csv
                    )
                    build_excel_report(args)
        except RuntimeError as exc:
            logging.error("%s", exc)
        if not args.interval_hours:
            break
        seconds = args.interval_hours * 3600
        logging.info("다음 실행까지 %.1f시간 대기합니다. Ctrl+C로 종료할 수 있습니다.", args.interval_hours)
        try:
            time.sleep(seconds)
        except KeyboardInterrupt:
            logging.info("사용자 요청으로 종료합니다.")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
