import base64
import json
import tempfile
import unittest
from pathlib import Path

import supabase_url_scanner as scanner


def jwt(role):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'HS256', 'typ': 'JWT'})}.{encode({'role': role})}.signature1234567890"


class ScannerTests(unittest.TestCase):
    def rules(self):
        return scanner.load_rules(Path("rules"))

    def test_masks_service_role_and_context(self):
        token = jwt("service_role")
        text = "supabase authorization token=" + token
        findings = scanner.scan_text(text, "https://example.com", "html", self.rules())
        item = next(f for f in findings if f["type"] == "JWT:service_role")
        self.assertNotIn(token, item["masked_value"])
        self.assertNotIn(token, item["context"])
        self.assertEqual(item["status"], "REVIEW_REQUIRED")

    def test_context_lowers_example_confidence(self):
        text = "example Supabase project https://abcdefghijklmnopqrst.supabase.co"
        findings = scanner.scan_text(text, "https://example.com", "html", self.rules())
        item = next(f for f in findings if f["type"] == "SUPABASE_PROJECT_URL")
        self.assertEqual(item["final_confidence"], "MEDIUM")

    def test_url_input_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "urls.txt"
            path.write_text("# x\nhttps://example.com\nhttps://example.com\n", encoding="utf-8")
            self.assertEqual(scanner.load_urls(path), ["https://example.com"])

    def test_private_addresses_rejected(self):
        self.assertFalse(scanner.public_url("http://127.0.0.1"))
        self.assertFalse(scanner.public_url("http://localhost"))
        self.assertTrue(scanner.public_url("https://example.com"))

    def test_secret_and_phone_are_masked(self):
        fake_secret = "sb_" + "secret_" + "abcdefghijklmnopqrstuvwxyz123456"
        text = (
            "supabase https://abcdefghijklmnopqrst.supabase.co "
            + fake_secret + " "
            "customer phone 010-9876-5432"
        )
        findings = scanner.scan_text(text, "https://app.example.org", "html", self.rules())
        secret = next(f for f in findings if f["type"] == "SUPABASE_SECRET_KEY")
        phone = next(f for f in findings if f["type"] == "PHONE_NUMBER")
        self.assertNotIn(fake_secret, secret["masked_value"])
        self.assertEqual(phone["masked_value"], "010-****-5432")

    def test_completed_urls_skip_but_errors_retry(self):
        rows = [
            {"url": "https://clean.example", "status": "CLEAN"},
            {"url": "https://finding.example", "status": "REVIEW_REQUIRED"},
            {"url": "https://error.example", "status": "ERROR"},
        ]
        self.assertEqual(
            scanner.completed_urls(rows),
            {"https://clean.example", "https://finding.example"},
        )

    def test_merge_details_deduplicates_and_summary_keeps_latest(self):
        details = scanner.merge_detail_rows(
            [{"evidence_hash": "sha256:a", "url": "https://a"}],
            [{"evidence_hash": "sha256:a", "url": "https://a"}, {"evidence_hash": "sha256:b", "url": "https://b"}],
        )
        self.assertEqual(len(details), 2)
        summaries = scanner.merge_summary_rows(
            [{"url": "https://a", "status": "ERROR"}],
            [{"url": "https://a", "status": "CLEAN"}],
        )
        self.assertEqual(summaries[0]["status"], "CLEAN")

    def test_atomic_csv_write_and_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "summary.csv"
            row = {field: "" for field in scanner.SUMMARY_FIELDS}
            row.update({"url": "https://example.com", "status": "CLEAN"})
            scanner.write_csv(path, scanner.SUMMARY_FIELDS, [row])
            loaded = scanner.load_csv_rows(path, scanner.SUMMARY_FIELDS)
            self.assertEqual(loaded[0]["url"], "https://example.com")
            self.assertEqual(loaded[0]["status"], "CLEAN")

    def test_summary_explains_low_confidence_matches(self):
        summary = {field: "" for field in scanner.SUMMARY_FIELDS}
        findings = [
            {"type": "EMAIL", "final_confidence": "LOW"},
            {"type": "EMAIL", "final_confidence": "LOW"},
            {"type": "PHONE_NUMBER", "final_confidence": "LOW"},
            {"type": "SUPABASE_REFERENCE", "final_confidence": "LOW"},
        ]
        scanner.summarize_findings(summary, findings)
        self.assertEqual(summary["status"], "LOW_CONFIDENCE_MATCHES")
        self.assertEqual(summary["review_priority"], "LOW")
        self.assertIn("EMAIL=2", summary["type_counts"])
        self.assertIn("PHONE_NUMBER", summary["type_counts"])

    def test_summary_prioritizes_service_role(self):
        summary = {field: "" for field in scanner.SUMMARY_FIELDS}
        scanner.summarize_findings(
            summary,
            [{"type": "JWT:service_role", "final_confidence": "HIGH"}],
        )
        self.assertEqual(summary["status"], "REVIEW_REQUIRED")
        self.assertEqual(summary["review_priority"], "HIGH")


if __name__ == "__main__":
    unittest.main()
