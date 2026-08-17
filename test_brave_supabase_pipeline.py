import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock

import brave_supabase_pipeline as pipeline


class BravePipelineTests(unittest.TestCase):
    def test_normalize_removes_query_fragment_and_lowercases_host(self):
        self.assertEqual(
            pipeline.normalize_url("HTTPS://Demo.Vercel.App/path?utm_source=x#top"),
            "https://demo.vercel.app/path",
        )

    def test_candidate_filter_limits_domains_and_skips_docs(self):
        allowed = ("vercel.app", "netlify.app")
        self.assertTrue(pipeline.is_allowed_candidate("https://demo.vercel.app/app", allowed))
        self.assertFalse(pipeline.is_allowed_candidate("https://demo.vercel.app/blog/post", allowed))
        self.assertFalse(pipeline.is_allowed_candidate("https://example.com/app", allowed))

    def test_relax_query_removes_quotes_and_exclusions(self):
        query = 'site:vercel.app ("book appointment" OR "available slots") -inurl:blog -template'
        relaxed = pipeline.relax_query(query)
        self.assertEqual(
            relaxed,
            "site:vercel.app book appointment OR available slots",
        )

    def test_relaxation_requires_supabase_anchor(self):
        self.assertTrue(pipeline.has_supabase_anchor('site:vercel.app "supabase.co"'))
        self.assertTrue(pipeline.has_supabase_anchor('site:vercel.app "built with supabase"'))
        self.assertFalse(
            pipeline.has_supabase_anchor(
                'site:vercel.app ("book appointment" OR "available slots")'
            )
        )

    def test_brave_search_extracts_only_urls(self):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "web": {"results": [{"url": "https://a.vercel.app"}, {"title": "missing"}]}
        }
        session = Mock()
        session.get.return_value = response
        self.assertEqual(
            pipeline.brave_search(session, "not-a-real-key", "query", 10, "KR", 2),
            ["https://a.vercel.app"],
        )
        headers = session.get.call_args.kwargs["headers"]
        self.assertEqual(headers["X-Subscription-Token"], "not-a-real-key")
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["offset"], 2)

    def test_append_urls_preserves_existing_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "urls.txt"
            path.write_text("# keep\nhttps://old.vercel.app/\n", encoding="utf-8")
            pipeline.append_urls(path, ["https://new.netlify.app/"])
            text = path.read_text(encoding="utf-8")
            self.assertIn("# keep", text)
            self.assertIn("https://old.vercel.app/", text)
            self.assertIn("https://new.netlify.app/", text)

    def test_report_queue_keeps_only_review_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "summary.csv"
            target = Path(directory) / "report.csv"
            source.write_text(
                "status,review_priority,url\n"
                "CLEAN,NONE,https://clean.example\n"
                "REVIEW_REQUIRED,LOW,https://review.example\n"
                "REVIEW_REQUIRED,HIGH,https://review.example\n",
                encoding="utf-8",
            )
            self.assertEqual(pipeline.build_report_queue(source, target), 1)
            self.assertIn("https://review.example", target.read_text(encoding="utf-8-sig"))

    def test_api_request_budget_is_hard_limit(self):
        args = Namespace(
            search_offset=1,
            _completed_requests={},
            api_request_budget=700,
            _api_requests_made=700,
        )
        with self.assertRaises(pipeline.ApiBudgetExhausted):
            pipeline.search_with_budget(args, Mock(), "fake-key", "query", "base")

    def test_completed_api_request_is_not_repeated(self):
        query = "site:vercel.app supabase"
        key = pipeline.request_key(query, 2, "base")
        args = Namespace(
            search_offset=2,
            _completed_requests={key: {"result_count": "4"}},
            api_request_budget=700,
            _api_requests_made=0,
        )
        outcome = pipeline.search_with_budget(args, Mock(), "fake-key", query, "base")
        self.assertTrue(outcome["skipped"])
        self.assertEqual(outcome["result_count"], 4)
        self.assertEqual(args._api_requests_made, 0)


if __name__ == "__main__":
    unittest.main()
