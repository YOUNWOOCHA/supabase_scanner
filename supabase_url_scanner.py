#!/usr/bin/env python3
"""지정한 공개 URL의 HTML과 동일 출처 JS에서 Supabase/민감정보 후보를 탐지한다.

Supabase API·DB·Storage는 호출하지 않으며, 탐지값은 마스킹해서 저장한다.
자동 탐지는 실제 유출 확정이 아닌 REVIEW_REQUIRED 후보로 취급한다.
"""

from __future__ import annotations

import argparse
import base64
import csv
from collections import Counter
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

USER_AGENT = "Public-Interest-Supabase-Research/0.2"
TIMEOUT = 15
MAX_JS = 30
MAX_REDIRECTS = 5
MAX_TEXT_BYTES = 10 * 1024 * 1024
MAX_FINDINGS_PER_RULE = 200
SEVERITY_RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
CONFIDENCE_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

DETAIL_FIELDS = [
    "detected_utc", "url", "domain", "source", "type", "severity",
    "base_confidence", "final_confidence", "status", "masked_value",
    "context", "positive_keywords", "negative_keywords",
    "strong_negative_keywords", "score", "description", "evidence_hash",
]
SUMMARY_FIELDS = [
    "scanned_utc", "url", "domain", "http_status", "js_checked",
    "detection_count", "highest_severity", "supabase_detected",
    "sensitive_candidate_count", "service_role_candidate", "secret_key_candidate",
    "detected_types", "type_counts", "confidence_counts", "review_priority",
    "review_reason", "analyst_next_step", "status", "note",
]

SUPABASE_SIGNAL_TYPES = {
    "SUPABASE_REFERENCE", "SUPABASE_PROJECT_URL", "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_SECRET_KEY", "JWT:anon", "JWT:service_role",
}
NON_SENSITIVE_TYPES = {
    "SUPABASE_REFERENCE", "SUPABASE_PROJECT_URL", "SUPABASE_PUBLISHABLE_KEY", "JWT:anon",
}


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def excel_safe(value):
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


def public_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def resolves_publicly(url):
    """호스트의 모든 해석 결과가 공인 IP일 때만 허용한다."""
    parsed = urlparse(url)
    if not public_url(url) or not parsed.hostname:
        return False
    try:
        addresses = {
            item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)
        }
        return bool(addresses) and all(ipaddress.ip_address(value).is_global for value in addresses)
    except (socket.gaierror, ValueError):
        return False


def load_urls(path):
    seen, urls = set(), []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        url = raw.strip()
        if not url or url.startswith("#") or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def load_csv_rows(path, fields):
    """기존 CSV를 읽는다. 이전 스키마에 없는 열은 빈 값으로 보완한다."""
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {field: row.get(field, "") for field in fields}
            for row in csv.DictReader(handle)
        ]


def completed_urls(summary_rows):
    """완료 상태만 중복 검사에서 제외한다. ERROR는 다음 실행에서 재시도한다."""
    completed_statuses = {
        "CLEAN", "SUPABASE_ONLY", "LOW_CONFIDENCE_MATCHES", "REVIEW_REQUIRED", "SKIPPED"
    }
    return {
        row.get("url", "")
        for row in summary_rows
        if row.get("url") and row.get("status") in completed_statuses
    }


def merge_detail_rows(existing, new_rows):
    """증거 해시 기준으로 상세 결과를 병합한다."""
    merged = {}
    for row in [*existing, *new_rows]:
        key = row.get("evidence_hash") or "|".join(
            [row.get("url", ""), row.get("source", ""), row.get("type", ""), row.get("masked_value", "")]
        )
        merged[key] = row
    return list(merged.values())


def merge_summary_rows(existing, new_rows):
    """URL별 최신 요약 한 행만 유지한다."""
    merged = {row.get("url", ""): row for row in existing if row.get("url")}
    for row in new_rows:
        if row.get("url"):
            merged[row["url"]] = row
    return list(merged.values())


def summarize_findings(summary, findings):
    """탐지 유형과 검토 이유를 URL 요약 행에 사람이 읽을 수 있게 추가한다."""
    type_counts = Counter(f["type"] for f in findings)
    confidence_counts = Counter(f["final_confidence"] for f in findings)
    summary["detected_types"] = "; ".join(sorted(type_counts))
    summary["type_counts"] = "; ".join(
        f"{name}={count}" for name, count in sorted(type_counts.items())
    )
    summary["confidence_counts"] = "; ".join(
        f"{name}={confidence_counts.get(name, 0)}" for name in ("HIGH", "MEDIUM", "LOW")
        if confidence_counts.get(name, 0)
    )

    sensitive = [f for f in findings if f["type"] not in NON_SENSITIVE_TYPES]
    actionable = [f for f in sensitive if f["final_confidence"] in {"MEDIUM", "HIGH"}]
    critical_types = {
        "SUPABASE_SECRET_KEY", "JWT:service_role", "LABELED_SECRET",
        "POSTGRES_CONNECTION_URI", "PRIVATE_KEY",
    }
    critical = [f for f in sensitive if f["type"] in critical_types]

    if critical:
        names = ", ".join(sorted({f["type"] for f in critical}))
        summary["review_priority"] = "HIGH"
        summary["review_reason"] = f"공개되면 안 되는 비밀정보 유형 후보 탐지: {names}"
        summary["analyst_next_step"] = (
            "results.csv에서 해당 유형의 마스킹 문맥과 source를 확인하고, 원본 값을 사용하지 말고 "
            "사이트 운영 주체·보안 연락처 및 공개 번들 포함 여부를 확인"
        )
        summary["status"] = "REVIEW_REQUIRED"
    elif actionable:
        names = ", ".join(sorted({f["type"] for f in actionable}))
        summary["review_priority"] = "MEDIUM"
        summary["review_reason"] = f"MEDIUM/HIGH 신뢰도 민감정보 형식 후보 탐지: {names}"
        summary["analyst_next_step"] = (
            "results.csv의 마스킹 문맥에서 실제 사용자 데이터인지 예제·지원 연락처인지 확인하고 "
            "복수 항목의 일관성과 페이지 기능을 대조"
        )
        summary["status"] = "REVIEW_REQUIRED"
    elif sensitive:
        names = ", ".join(sorted({f["type"] for f in sensitive}))
        summary["review_priority"] = "LOW"
        summary["review_reason"] = f"LOW 신뢰도 형식 일치만 탐지: {names}"
        summary["analyst_next_step"] = (
            "results.csv의 마스킹 문맥을 표본 확인. 압축 JS 숫자열·템플릿·예제 데이터이면 오탐으로 기록"
        )
        summary["status"] = "LOW_CONFIDENCE_MATCHES"
    elif findings:
        summary["review_priority"] = "NONE"
        summary["review_reason"] = "Supabase 공개 지문만 탐지되고 민감정보 후보는 없음"
        summary["analyst_next_step"] = "추가 조치 없음. anon/publishable 키는 정상 공개 가능"
        summary["status"] = "SUPABASE_ONLY"
    else:
        summary["review_priority"] = "NONE"
        summary["review_reason"] = "탐지 항목 없음"
        summary["analyst_next_step"] = "추가 조치 없음"
    return summary


def backfill_summary_explanations(summary_rows, detail_rows):
    """기존 CSV에도 네트워크 재검사 없이 새 설명 필드를 채운다."""
    by_url = {}
    for row in detail_rows:
        by_url.setdefault(row.get("url", ""), []).append(row)
    changed = False
    for row in summary_rows:
        if row.get("detected_types") or not row.get("url"):
            continue
        before = dict(row)
        summarize_findings(row, by_url.get(row["url"], []))
        if row != before:
            changed = True
    return changed


def load_rules(directory):
    rules = {}
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for name, rule in data.items():
            item = dict(rule)
            item["regex"] = re.compile(item["pattern"], re.I | re.S)
            rules[name] = item
    if not rules:
        raise RuntimeError(f"탐지 규칙이 없습니다: {directory}")
    return rules


def mask_value(value, kind):
    if kind == "EMAIL" and "@" in value:
        local, domain = value.split("@", 1)
        return (local[:1] + "***@" + domain) if local else "***@" + domain
    if kind == "IP_ADDRESS":
        parts = value.split(".")
        return ".".join(parts[:2] + ["***", "***"])
    if kind == "PRIVATE_KEY":
        return "-----BEGIN *** PRIVATE KEY-----"
    if kind == "PHONE_NUMBER":
        digits = re.sub(r"\D", "", value)
        return (digits[:3] + "-****-" + digits[-4:]) if len(digits) >= 10 else "***"
    if kind == "LABELED_SECRET":
        parts = re.split(r"([:=])", value, maxsplit=1)
        if len(parts) == 3:
            return parts[0] + parts[1] + "***MASKED***"
    if kind == "POSTGRES_CONNECTION_URI":
        return "postgresql://***MASKED***"
    if kind == "BEARER_TOKEN":
        return "Bearer ***MASKED***"
    if len(value) <= 10:
        return value[:2] + "*" * max(2, len(value) - 2)
    return value[:4] + "*" * min(16, len(value) - 8) + value[-4:]


def jwt_role(value):
    try:
        part = value.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        role = payload.get("role")
        return role if isinstance(role, str) else None
    except Exception:
        return None


def confidence(base, score):
    level = CONFIDENCE_RANK.get(base, 0)
    if score >= 2:
        level += 1
    elif score <= -2:
        level -= 1
    return ["LOW", "MEDIUM", "HIGH"][max(0, min(2, level))]


def scan_text(text, url, source, rules):
    findings = []
    lowered = text.lower()
    for kind, rule in rules.items():
        for match_index, match in enumerate(rule["regex"].finditer(text)):
            if match_index >= MAX_FINDINGS_PER_RULE:
                break
            value = match.group(0)
            left, right = max(0, match.start() - 100), min(len(text), match.end() + 100)
            context = text[left:right].replace("\r", " ").replace("\n", " ")
            context_lower = context.lower()
            positives = [k for k in rule.get("positive_keywords", {}) if k.lower() in context_lower]
            negatives = [k for k in rule.get("negative_keywords", {}) if k.lower() in context_lower]
            strong = [k for k in rule.get("strong_negative_keywords", {}) if k.lower() in context_lower]
            score = sum(rule.get("positive_keywords", {}).get(k, 0) for k in positives)
            score += sum(rule.get("negative_keywords", {}).get(k, 0) for k in negatives)
            score += sum(rule.get("strong_negative_keywords", {}).get(k, 0) for k in strong)
            if strong:
                score = min(score, -2)
            role = jwt_role(value) if kind == "JWT" else None
            display_type = f"JWT:{role}" if role else kind
            masked = mask_value(value, kind)
            masked_context = context.replace(value, masked)
            digest = hashlib.sha256(f"{url}|{source}|{kind}|{value}".encode()).hexdigest()
            findings.append({
                "detected_utc": now_utc(), "url": url, "domain": urlparse(url).netloc,
                "source": source, "type": display_type, "severity": rule["severity"],
                "base_confidence": rule["confidence"],
                "final_confidence": confidence(rule["confidence"], score),
                "status": "REVIEW_REQUIRED", "masked_value": masked,
                "context": masked_context, "positive_keywords": "; ".join(positives),
                "negative_keywords": "; ".join(negatives),
                "strong_negative_keywords": "; ".join(strong), "score": score,
                "description": rule["description"], "evidence_hash": "sha256:" + digest,
            })
    return findings


def robots_allowed(session, url):
    if not resolves_publicly(url):
        return False
    try:
        parsed = urlparse(url)
        response = session.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt", timeout=TIMEOUT)
        if response.status_code >= 400:
            return True
        robot = RobotFileParser()
        robot.parse(response.text.splitlines())
        return robot.can_fetch(USER_AGENT, url)
    except requests.RequestException:
        return True


def safe_get(session, url):
    """각 리디렉션 목적지가 공인 주소인지 확인하면서 GET한다."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not resolves_publicly(current):
            raise requests.RequestException("비공개 주소 또는 확인 불가능한 호스트")
        response = session.get(current, timeout=TIMEOUT, allow_redirects=False)
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("location")
            if not location:
                return response
            current = urljoin(current, location)
            continue
        return response
    raise requests.TooManyRedirects("리디렉션 한도 초과")


def inspect_url(session, url, rules, delay):
    summary = {
        "scanned_utc": now_utc(), "url": url, "domain": urlparse(url).netloc,
        "http_status": "", "js_checked": 0, "detection_count": 0,
        "highest_severity": "NONE", "supabase_detected": "N",
        "sensitive_candidate_count": 0, "service_role_candidate": "N",
        "secret_key_candidate": "N", "detected_types": "", "type_counts": "",
        "confidence_counts": "", "review_priority": "NONE", "review_reason": "",
        "analyst_next_step": "", "status": "CLEAN", "note": "",
    }
    if not public_url(url):
        summary.update(status="SKIPPED", note="공개 http/https URL이 아님")
        return [], summary
    if not resolves_publicly(url):
        summary.update(status="SKIPPED", note="공인 IP로 해석되지 않는 호스트")
        return [], summary
    if not robots_allowed(session, url):
        summary.update(status="SKIPPED", note="robots.txt 불허")
        return [], summary
    time.sleep(delay)
    try:
        response = safe_get(session, url)
        summary["http_status"] = response.status_code
        response.raise_for_status()
        if not public_url(response.url):
            summary.update(status="SKIPPED", note="비공개 주소로 리디렉션")
            return [], summary
    except requests.RequestException as exc:
        summary.update(status="ERROR", note=f"페이지 요청 실패: {type(exc).__name__}")
        return [], summary
    content_type = response.headers.get("content-type", "").lower()
    if not any(value in content_type for value in ("text/html", "javascript", "text/plain")):
        summary.update(status="SKIPPED", note="텍스트 콘텐츠가 아님")
        return [], summary
    if len(response.content) > MAX_TEXT_BYTES:
        summary.update(status="SKIPPED", note="텍스트 크기 한도 초과")
        return [], summary

    findings = scan_text(response.text, url, "html", rules)
    origin = urlparse(response.url).netloc.lower()
    scripts = []
    for src in re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', response.text, re.I):
        js_url = urljoin(response.url, src)
        if urlparse(js_url).netloc.lower() == origin and js_url not in scripts:
            scripts.append(js_url)
    for js_url in scripts[:MAX_JS]:
        if not resolves_publicly(js_url):
            continue
        time.sleep(delay)
        try:
            js = safe_get(session, js_url)
            js.raise_for_status()
            if len(js.content) > MAX_TEXT_BYTES:
                continue
        except requests.RequestException:
            continue
        summary["js_checked"] += 1
        findings.extend(scan_text(js.text, url, "same-origin-js", rules))

    # 동일 소스·유형·값 해시 중복 제거
    findings = list({f["evidence_hash"]: f for f in findings}.values())
    supabase_detected = any(f["type"] in SUPABASE_SIGNAL_TYPES for f in findings)
    if not supabase_detected:
        # 이 도구의 목적은 Supabase 기반 웹앱 조사이므로 일반 이메일·IP 오탐은 버린다.
        findings = []
        summary["note"] = "Supabase 지문 없음—일반 민감정보 후보는 결과에서 제외"
    summary["detection_count"] = len(findings)
    if findings:
        summary["highest_severity"] = max(
            (f["severity"] for f in findings), key=lambda value: SEVERITY_RANK[value]
        )
        summary["supabase_detected"] = "Y"
        sensitive = [f for f in findings if f["type"] not in NON_SENSITIVE_TYPES]
        summary["sensitive_candidate_count"] = len(sensitive)
        summary["service_role_candidate"] = "Y" if any(f["type"] == "JWT:service_role" for f in findings) else "N"
        summary["secret_key_candidate"] = "Y" if any(
            f["type"] in {"SUPABASE_SECRET_KEY", "LABELED_SECRET", "POSTGRES_CONNECTION_URI"}
            for f in findings
        ) else "N"
        summarize_findings(summary, findings)
    else:
        summarize_findings(summary, findings)
    return findings, summary


def write_csv(path, fields, rows):
    """같은 폴더의 임시 파일을 완성한 뒤 교체해 기존 CSV 손상을 방지한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8-sig", newline="", delete=False,
            dir=path.parent, prefix=path.name + ".", suffix=".tmp",
        ) as handle:
            temp_name = handle.name
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({field: excel_safe(row.get(field, "")) for field in fields})
        os.replace(temp_name, path)
        temp_name = None
    except PermissionError as exc:
        raise RuntimeError(f"CSV가 Excel 등에서 열려 있습니다. 파일을 닫고 다시 실행하세요: {path}") from exc
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="URL 목록 기반 Supabase 공개정보 후보 스캐너")
    parser.add_argument("--input", type=Path, default=Path("input/urls.txt"))
    parser.add_argument("--rules", type=Path, default=Path("rules"))
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--rescan", action="store_true", help="기존 완료 URL도 다시 검사")
    args = parser.parse_args()
    if args.delay < 1:
        raise SystemExit("--delay는 1초 이상이어야 합니다.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    urls, rules = load_urls(args.input), load_rules(args.rules)
    detail_path = args.output_dir / "results.csv"
    summary_path = args.output_dir / "batch_summary.csv"
    existing_details = load_csv_rows(detail_path, DETAIL_FIELDS)
    existing_summaries = load_csv_rows(summary_path, SUMMARY_FIELDS)
    summary_schema_updated = backfill_summary_explanations(existing_summaries, existing_details)
    already_completed = completed_urls(existing_summaries)
    pending_urls = urls if args.rescan else [url for url in urls if url not in already_completed]
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    details, summaries = [], []
    logging.info(
        "입력 %d개 / 기존 완료 %d개 / 이번 검사 %d개%s",
        len(urls), len(already_completed), len(pending_urls), " (강제 재검사)" if args.rescan else "",
    )
    if not pending_urls:
        if summary_schema_updated:
            try:
                write_csv(summary_path, SUMMARY_FIELDS, existing_summaries)
                logging.info("기존 요약 CSV에 탐지 유형·검토 안내 필드를 추가했습니다.")
            except RuntimeError as exc:
                logging.error("%s", exc)
                return 2
        else:
            logging.info("신규 검사 대상이 없습니다. 기존 CSV를 변경하지 않습니다.")
        return 0
    for index, url in enumerate(pending_urls, 1):
        findings, summary = inspect_url(session, url, rules, args.delay)
        details.extend(findings)
        summaries.append(summary)
        logging.info(
            "[%d/%d] %s -> %s (%d건)", index, len(pending_urls), url, summary["status"], len(findings)
        )
    merged_details = merge_detail_rows(existing_details, details)
    merged_summaries = merge_summary_rows(existing_summaries, summaries)
    try:
        write_csv(detail_path, DETAIL_FIELDS, merged_details)
        write_csv(summary_path, SUMMARY_FIELDS, merged_summaries)
    except RuntimeError as exc:
        logging.error("%s", exc)
        return 2
    logging.info("신규 상세 %d건 / 누적 %d건: %s", len(details), len(merged_details), detail_path)
    logging.info("신규 요약 %d건 / 누적 %d건: %s", len(summaries), len(merged_summaries), summary_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
