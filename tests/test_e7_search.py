import json
from datetime import date
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class E7EvidenceIdentityTests(unittest.TestCase):
    def test_rest_client_surfaces_azure_error_body(self):
        fake_httpx = types.SimpleNamespace(Client=lambda **kwargs: None)
        original_clients = sys.modules.pop("search.clients", None)
        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            sys.modules.pop("search.clients", None)
            from search.clients import BearerRestClient
        if original_clients is not None:
            self.addCleanup(sys.modules.__setitem__, "search.clients", original_clients)
        self.addCleanup(sys.modules.pop, "search.clients", None)

        class FakeCredential:
            def get_token(self, scope):
                return type("Token", (), {"token": "test-token"})()

        class FakeResponse:
            is_error = True
            status_code = 400
            text = '{"error":{"message":"invalid schema detail"}}'

        class FakeHttpClient:
            def request(self, *args, **kwargs):
                return FakeResponse()

        client = BearerRestClient("https://example.test", "scope", FakeCredential())
        client._client = FakeHttpClient()
        with self.assertRaisesRegex(RuntimeError, "invalid schema detail"):
            client.request("PUT", "indexes/example", payload={})

    def test_rest_client_retries_transient_throttling(self):
        fake_httpx = types.SimpleNamespace(Client=lambda **kwargs: None)
        original_clients = sys.modules.pop("search.clients", None)
        with patch.dict(sys.modules, {"httpx": fake_httpx}):
            from search.clients import BearerRestClient
            clients_module = sys.modules["search.clients"]
        if original_clients is not None:
            self.addCleanup(sys.modules.__setitem__, "search.clients", original_clients)
        self.addCleanup(sys.modules.pop, "search.clients", None)

        class FakeCredential:
            def get_token(self, scope):
                return type("Token", (), {"token": "test-token"})()

        class FakeResponse:
            def __init__(self, status_code, payload):
                self.status_code = status_code
                self.is_error = status_code >= 400
                self.headers = {"retry-after-ms": "1"}
                self.text = json.dumps(payload)
                self.content = self.text.encode()
                self._payload = payload

            def json(self):
                return self._payload

        class FakeHttpClient:
            def __init__(self):
                self.responses = [
                    FakeResponse(429, {"error": "throttled"}),
                    FakeResponse(200, {"value": "ok"}),
                ]

            def request(self, *args, **kwargs):
                return self.responses.pop(0)

        client = BearerRestClient("https://example.test", "scope", FakeCredential())
        client._client = FakeHttpClient()
        with patch.object(clients_module.time, "sleep") as sleep:
            self.assertEqual(client.request("POST", "documents", payload={}), {"value": "ok"})
        sleep.assert_called_once_with(0.001)

    def test_document_id_is_replay_stable_and_revision_specific(self):
        from search.evidence import evidence_document_id

        first = evidence_document_id("news", "news:42", "a" * 64, 0)
        replay = evidence_document_id("news", "news:42", "a" * 64, 0)
        revised = evidence_document_id("news", "news:42", "b" * 64, 0)
        next_chunk = evidence_document_id("news", "news:42", "a" * 64, 1)

        self.assertEqual(first, replay)
        self.assertNotEqual(first, revised)
        self.assertNotEqual(first, next_chunk)
        self.assertRegex(first, r"^d[A-Za-z0-9_-]{43}$")

    def test_retrieval_filter_is_point_in_time_safe(self):
        from search.retrieval import build_evidence_filter

        result = build_evidence_filter(
            as_of=date(2026, 3, 15),
            security_sks=[42, 84],
            source_types=["news", "sec_filing"],
        )

        self.assertIn("event_date le 2026-03-15T23:59:59Z", result)
        self.assertIn("knowledge_date le 2026-03-15T23:59:59Z", result)
        self.assertIn("security_sk eq 42", result)
        self.assertIn("security_sk eq 84", result)
        self.assertIn("source_type eq 'news'", result)
        self.assertIn("source_type eq 'sec_filing'", result)

    def test_index_schema_matches_embedding_and_pit_contract(self):
        schema = json.loads((ROOT / "search" / "index_schema.json").read_text(encoding="utf-8"))
        fields = {field["name"]: field for field in schema["fields"]}

        self.assertEqual(schema["name"], "idx-news-filings")
        self.assertNotIn("description", schema)
        self.assertTrue(fields["id"]["key"])
        self.assertEqual(fields["content_vector"]["dimensions"], 3072)
        self.assertEqual(fields["content_vector"]["vectorSearchProfile"], "evidence-vector-profile")
        for field in [
            "security_sk",
            "source_type",
            "event_date",
            "knowledge_date",
            "generation",
        ]:
            self.assertTrue(fields[field]["filterable"])
        self.assertIn("semantic", schema)
        self.assertIn("vectorSearch", schema)
        self.assertEqual(
            schema["vectorSearch"]["profiles"][0]["vectorizer"],
            "evidence-openai-vectorizer",
        )

    def test_index_sync_uploads_before_stale_generation_cleanup(self):
        from search.evidence import evidence_document_id
        from search.indexing import EvidenceIndexer

        class FakeSearch:
            def __init__(self):
                self.calls = []

            def ensure_index(self, schema):
                self.calls.append(("ensure", schema["name"]))

            def upload_documents(self, documents):
                documents = list(documents)
                self.calls.append(("upload", documents))
                return len(documents)

            def list_document_generations(self):
                self.calls.append(("existing",))
                return {"d-stale": "generation-0"}

            def delete_documents(self, document_ids):
                document_ids = list(document_ids)
                self.calls.append(("cleanup", document_ids))
                return len(document_ids)

        class FakeEmbeddings:
            def embed(self, texts):
                return [[float(index), 1.0] for index, _ in enumerate(texts)]

        revision_hash = "c" * 64
        documents = []
        for chunk_index in range(2):
            documents.append({
                "id": evidence_document_id("news", "news:42", revision_hash, chunk_index),
                "security_sk": 42,
                "symbol": "MSFT",
                "source_type": "news",
                "source_id": "news:42",
                "source_name": "Example",
                "source_url": "https://example.test/article",
                "title": "Title",
                "content": f"Evidence {chunk_index}",
                "event_date": "2026-03-14",
                "knowledge_date": "2026-03-15",
                "published_at": "2026-03-15T10:00:00Z",
                "revision_hash": revision_hash,
                "chunk_index": chunk_index,
                "generation": "generation-1",
                "content_status": "full_text",
            })

        search = FakeSearch()
        result = EvidenceIndexer(
            search,
            FakeEmbeddings(),
            {"name": "idx-news-filings"},
        ).sync(documents, batch_size=1)

        self.assertEqual(
            [call[0] for call in search.calls],
            ["ensure", "existing", "upload", "upload", "cleanup"],
        )
        self.assertEqual(search.calls[2][1][0]["content_vector"], [0.0, 1.0])
        self.assertEqual(search.calls[2][1][0]["event_date"], "2026-03-14T00:00:00Z")
        self.assertEqual(search.calls[-1], ("cleanup", ["d-stale"]))
        self.assertEqual(result["deleted_stale"], 1)

    def test_index_sync_skips_existing_generation_documents(self):
        from search.evidence import evidence_document_id
        from search.indexing import EvidenceIndexer

        revision_hash = "f" * 64
        documents = [{
            "id": evidence_document_id("news", "news:42", revision_hash, index),
            "source_type": "news",
            "source_id": "news:42",
            "content": f"Evidence {index}",
            "event_date": "2026-03-14",
            "knowledge_date": "2026-03-15",
            "revision_hash": revision_hash,
            "chunk_index": index,
            "generation": "generation-1",
            "content_status": "full_text",
        } for index in range(3)]

        class FakeSearch:
            def ensure_index(self, schema):
                pass

            def list_document_generations(self):
                return {documents[0]["id"]: "generation-1"}

            def merge_documents(self, rows):
                self.merged = list(rows)
                return len(self.merged)

            def upload_documents(self, rows):
                self.uploaded = list(rows)
                return len(self.uploaded)

            def delete_documents(self, document_ids):
                return 0

        class FakeEmbeddings:
            def __init__(self):
                self.texts = []

            def embed(self, texts):
                self.texts.extend(texts)
                return [[1.0] for _ in texts]

        search = FakeSearch()
        embeddings = FakeEmbeddings()
        result = EvidenceIndexer(
            search, embeddings, {"name": "idx-news-filings"}
        ).sync(documents, batch_size=2, embedding_workers=2)

        self.assertEqual(embeddings.texts, ["Evidence 1", "Evidence 2"])
        self.assertEqual(result["existing"], 1)
        self.assertEqual(result["metadata_refreshed"], 0)
        self.assertEqual(result["uploaded"], 2)

    def test_index_sync_reuses_vectors_across_generations(self):
        from search.evidence import evidence_document_id
        from search.indexing import EvidenceIndexer

        revision_hash = "a" * 64
        document = {
            "id": evidence_document_id("news", "news:42", revision_hash, 0),
            "source_type": "news",
            "source_id": "news:42",
            "content": "Immutable evidence",
            "event_date": "2026-03-14",
            "knowledge_date": "2026-03-15",
            "revision_hash": revision_hash,
            "chunk_index": 0,
            "generation": "generation-2",
            "content_status": "full_text",
        }

        class FakeSearch:
            def ensure_index(self, schema):
                pass

            def list_document_generations(self):
                return {document["id"]: "generation-1"}

            def merge_documents(self, rows):
                self.merged = list(rows)
                return len(self.merged)

            def upload_documents(self, rows):
                raise AssertionError("unchanged evidence must not be uploaded")

            def delete_documents(self, document_ids):
                return 0

        class FailIfEmbedded:
            def embed(self, texts):
                raise AssertionError("unchanged evidence must reuse its vector")

        search = FakeSearch()
        result = EvidenceIndexer(
            search, FailIfEmbedded(), {"name": "idx-news-filings"}
        ).sync([document], batch_size=1)

        self.assertEqual(result["existing"], 1)
        self.assertEqual(result["metadata_refreshed"], 1)
        self.assertEqual(result["uploaded"], 0)
        self.assertEqual(search.merged[0]["generation"], "generation-2")
        self.assertNotIn("content", search.merged[0])
        self.assertNotIn("content_vector", search.merged[0])

    def test_sentiment_enrichment_bulk_loads_cache_once(self):
        from search.sentiment import enrich_with_cached_sentiment

        class FakeService:
            def __init__(self):
                self.calls = 0

            def cached_documents(self):
                self.calls += 1
                return [{
                    "id": "cache-1",
                    "document_revision_hash": "a" * 64,
                    "model_version": "model-1",
                    "prompt_version": "prompt-1",
                    "sentiment": "0.4",
                    "relevance": "0.9",
                }]

        documents = [
            {"source_type": "news", "revision_hash": "a" * 64},
            {"source_type": "news", "revision_hash": "b" * 64},
            {"source_type": "sec_filing", "revision_hash": "a" * 64},
        ]
        service = FakeService()

        enriched = enrich_with_cached_sentiment(documents, service)

        self.assertEqual(enriched, 1)
        self.assertEqual(service.calls, 1)
        self.assertEqual(documents[0]["sentiment_cache_key"], "cache-1")
        self.assertNotIn("sentiment", documents[1])

    def test_ingestion_search_sync_uses_configurable_large_batches(self):
        ingestion_app = (ROOT / "connectors" / "function_app.py").read_text(
            encoding="utf-8"
        )
        function_bicep = (ROOT / "infra" / "modules" / "functionapp.bicep").read_text(
            encoding="utf-8"
        )

        self.assertIn('os.environ.get("AI_SEARCH_BATCH_SIZE", "128")', ingestion_app)
        self.assertIn('os.environ.get("AI_SEARCH_EMBEDDING_WORKERS", "2")', ingestion_app)
        self.assertIn("name: 'AI_SEARCH_BATCH_SIZE'", function_bicep)
        self.assertIn("name: 'AI_SEARCH_EMBEDDING_WORKERS'", function_bicep)
        self.assertIn("value: '128'", function_bicep)

    def test_search_datetime_normalizes_negative_offsets(self):
        from search.indexing import _search_datetime

        self.assertEqual(
            _search_datetime("2026-03-15T10:30:00-05:00"),
            "2026-03-15T10:30:00-05:00",
        )
        self.assertEqual(_search_datetime("2026-03-15T10:30:00"), "2026-03-15T10:30:00Z")

    def test_search_generation_ids_page_within_key_prefix(self):
        from search.clients import AzureSearchRestClient

        client = AzureSearchRestClient.__new__(AzureSearchRestClient)
        payloads = []

        def search(payload):
            payloads.append(payload)
            if "id ge 'd-'" in payload["filter"]:
                if payload["skip"] == 0:
                    return {"value": [{"id": "d-a"}, {"id": "d-b"}]}
                return {"value": [{"id": "d-c"}]}
            return {"value": []}

        client.search = search

        ids = client.list_generation_ids("generation-1", batch_size=2)

        self.assertEqual(ids, {"d-a", "d-b", "d-c"})
        self.assertEqual(payloads[0]["skip"], 0)
        self.assertEqual(payloads[1]["skip"], 2)
        self.assertIn("id ge 'd-'", payloads[0]["filter"])

    def test_search_document_generations_list_without_generation_filter(self):
        from search.clients import AzureSearchRestClient

        client = AzureSearchRestClient.__new__(AzureSearchRestClient)
        payloads = []

        def search(payload):
            payloads.append(payload)
            if "id ge 'd-'" in payload["filter"]:
                return {"value": [{"id": "d-a", "generation": "generation-1"}]}
            return {"value": []}

        client.search = search

        documents = client.list_document_generations()

        self.assertEqual(documents, {"d-a": "generation-1"})
        self.assertNotIn("generation eq", payloads[0]["filter"])

    def test_search_deletes_explicit_document_ids_in_batches(self):
        from search.clients import AzureSearchRestClient

        client = AzureSearchRestClient.__new__(AzureSearchRestClient)
        client.index_name = "idx-news-filings"

        class FakeRest:
            def __init__(self):
                self.payloads = []

            def request(self, method, path, *, params, payload):
                self.payloads.append(payload)
                return {
                    "value": [
                        {"key": item["id"], "status": True}
                        for item in payload["value"]
                    ]
                }

        client._rest = FakeRest()

        deleted = client.delete_documents(["d-a", "d-b", "d-c"], batch_size=2)

        self.assertEqual(deleted, 3)
        self.assertEqual(len(client._rest.payloads), 2)
        self.assertEqual(
            client._rest.payloads[0]["value"],
            [
                {"@search.action": "delete", "id": "d-a"},
                {"@search.action": "delete", "id": "d-b"},
            ],
        )

    def test_search_metadata_refresh_uses_strict_merge(self):
        from search.clients import AzureSearchRestClient

        client = AzureSearchRestClient.__new__(AzureSearchRestClient)
        client.index_name = "idx-news-filings"

        class FakeRest:
            def request(self, method, path, *, params, payload):
                self.payload = payload
                return {"value": [{"key": "d-a", "status": True}]}

        client._rest = FakeRest()

        merged = client.merge_documents([{"id": "d-a", "generation": "generation-2"}])

        self.assertEqual(merged, 1)
        self.assertEqual(
            client._rest.payload["value"],
            [{"@search.action": "merge", "id": "d-a", "generation": "generation-2"}],
        )

    def test_projection_allows_omitted_nullable_filing_metadata(self):
        from search.evidence import evidence_document_id
        from search.indexing import validate_projection

        revision_hash = "e" * 64
        document = {
            "id": evidence_document_id("sec_filing", "filing:42:unscoped", revision_hash, 0),
            "source_type": "sec_filing",
            "source_id": "filing:42:unscoped",
            "content": "SEC form 13F. Filer: Example. Accession: 42.",
            "event_date": "2026-03-14",
            "knowledge_date": "2026-03-15",
            "revision_hash": revision_hash,
            "chunk_index": 0,
            "generation": "generation-1",
            "content_status": "metadata_only",
        }

        self.assertEqual(validate_projection([document]), "generation-1")

    def test_hybrid_retrieval_sends_pit_filter_and_returns_citations(self):
        from search.retrieval import EvidenceSearchService

        class FakeSearch:
            def __init__(self):
                self.payload = None

            def search(self, payload):
                self.payload = payload
                return {"value": [{
                    "id": "doc-1",
                    "security_sk": 42,
                    "symbol": "MSFT",
                    "source_type": "news",
                    "source_id": "news:42",
                    "source_name": "Example",
                    "source_url": "https://example.test/article",
                    "title": "Title",
                    "content": "Fallback excerpt",
                    "event_date": "2026-03-14T00:00:00Z",
                    "knowledge_date": "2026-03-15T00:00:00Z",
                    "content_status": "full_text",
                    "@search.score": 1.25,
                    "@search.rerankerScore": 3.5,
                    "@search.captions": [{"text": "Grounded excerpt"}],
                }]}

        search = FakeSearch()
        citations = EvidenceSearchService(search).retrieve(
            query="cloud demand",
            as_of=date(2026, 3, 15),
            security_sks=[42],
            limit=5,
        )

        self.assertEqual(search.payload["queryType"], "semantic")
        self.assertEqual(search.payload["vectorQueries"][0]["kind"], "text")
        self.assertIn("knowledge_date le 2026-03-15T23:59:59Z", search.payload["filter"])
        self.assertIn("security_sk eq 42", search.payload["filter"])
        self.assertEqual(citations[0]["excerpt"], "Grounded excerpt")
        self.assertEqual(citations[0]["url"], "https://example.test/article")

    def test_infrastructure_grants_keyless_e7_runtime_roles(self):
        search_bicep = (ROOT / "infra" / "modules" / "aisearch.bicep").read_text(encoding="utf-8")
        openai_bicep = (ROOT / "infra" / "modules" / "openai.bicep").read_text(encoding="utf-8")
        function_bicep = (ROOT / "infra" / "modules" / "functionapp.bicep").read_text(encoding="utf-8")

        self.assertIn("ingestFuncPrincipalId", search_bicep)
        self.assertIn("Search Index Data Contributor", search_bicep)
        self.assertIn("Search Service Contributor", search_bicep)
        self.assertIn("searchPrincipalId", openai_bicep)
        self.assertIn("Cognitive Services OpenAI User", openai_bicep)
        self.assertIn("defaultAction: 'Allow'", openai_bicep)
        self.assertIn("disableLocalAuth: true", openai_bicep)
        self.assertIn("version: '2024-11-20'", openai_bicep)
        self.assertIn("version: '1'", openai_bicep)
        self.assertEqual(openai_bicep.count("versionUpgradeOption: 'NoAutoUpgrade'"), 2)
        self.assertIn("AI_SEARCH_ENDPOINT", function_bicep)
        self.assertIn("AI_SEARCH_EVIDENCE_INDEX", function_bicep)
        self.assertIn("AZURE_OPENAI_ENDPOINT", function_bicep)
        self.assertIn("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", function_bicep)

    def test_function_apps_expose_index_sync_and_authorized_evidence_routes(self):
        ingestion_app = (ROOT / "connectors" / "function_app.py").read_text(encoding="utf-8")
        web_app = (ROOT / "api" / "function_app.py").read_text(encoding="utf-8")

        self.assertIn('route="sync_evidence_index"', ingestion_app)
        self.assertIn('read_serving_projection("evidence")', ingestion_app)
        self.assertIn('route="evidence"', web_app)
        self.assertIn("_identity_service().product_user(_principal(req))", web_app)
        self.assertIn("EvidenceSearchService", web_app)
        portfolio_service = web_app[
            web_app.index("def _portfolio_service"):web_app.index("def _evidence_service")
        ]
        self.assertIn("return PortfolioService(", portfolio_service)

    def test_fabric_notebook_materializes_evidence_and_shared_iq_projection(self):
        from tests.fabric_notebook import notebook_code

        notebook = notebook_code("nb_10_evidence_and_iq")
        for table in [
            "fact_evidence_chunk",
            "evidence_projection_manifest",
            "iq_security",
            "iq_company",
            "iq_theme",
            "iq_evidence_document",
            "iq_material_event",
            "iq_institution",
            "iq_security_in_theme",
            "iq_security_has_evidence",
            "iq_security_has_material_event",
            "iq_institution_holds_security",
        ]:
            self.assertIn(table, notebook)
        self.assertIn('"Files/serving/evidence"', notebook)
        self.assertIn("json(projection_output_path)", notebook)
        self.assertIn("knowledge_date", notebook)
        self.assertIn("metadata_only", notebook)
        self.assertIn("trs_source_count", notebook)
        self.assertIn("trs_projection_count", notebook)
        self.assertIn("if as_of_date == date.today().isoformat()", notebook)
        self.assertIn("evidence_merge.whenNotMatchedBySourceDelete()", notebook)
        self.assertIn("as_of_date cannot be in the future", notebook)
        self.assertIn('else f"Files/audit/evidence_asof/{as_of_date}"', notebook)
        self.assertIn('"projection_output_path": projection_output_path', notebook)
        self.assertNotIn("owner_user_sk", notebook)

    def test_fabric_iq_definition_is_bound_only_to_shared_projection_tables(self):
        ontology_root = ROOT / "fabric" / "auspex_iq_pilot.Ontology"
        platform = json.loads((ontology_root / ".platform").read_text(encoding="utf-8"))
        self.assertEqual(platform["metadata"]["type"], "Ontology")

        definition_files = list(ontology_root.rglob("*.json"))
        self.assertGreaterEqual(len(definition_files), 12)
        definitions = "\n".join(path.read_text(encoding="utf-8") for path in definition_files)
        for entity_name in [
            "Security", "Company", "Theme", "EvidenceDocument", "MaterialEvent", "Institution",
        ]:
            self.assertIn(f'"name": "{entity_name}"', definitions)
        for relationship_name in [
            "issuedBy", "inTheme", "hasEvidence", "hasMaterialEvent", "holdsSecurity",
        ]:
            self.assertIn(f'"name": "{relationship_name}"', definitions)
        self.assertIn('"sourceTableName": "iq_security_in_theme"', definitions)
        self.assertNotIn("owner_user_sk", definitions)
        self.assertNotIn("portfolio", definitions.lower())

        deploy_script = (ROOT / "scripts" / "deploy_fabric_items.py").read_text(encoding="utf-8")
        self.assertIn("_refresh_ontology_graph", deploy_script)
        self.assertIn('jobs/instances?jobType=Refresh', deploy_script)
        self.assertIn('part.get("path") != ".platform"', deploy_script)

    def test_sentiment_cache_key_is_versioned_and_replay_stable(self):
        from engine.sentiment import sentiment_cache_key

        first = sentiment_cache_key("a" * 64, "gpt-4o:2024-11-20", "e7_sentiment_v1")
        replay = sentiment_cache_key("a" * 64, "gpt-4o:2024-11-20", "e7_sentiment_v1")
        new_prompt = sentiment_cache_key("a" * 64, "gpt-4o:2024-11-20", "e7_sentiment_v2")
        new_model = sentiment_cache_key("a" * 64, "gpt-4o:future", "e7_sentiment_v1")

        self.assertEqual(first, replay)
        self.assertNotEqual(first, new_prompt)
        self.assertNotEqual(first, new_model)

    def test_sentiment_parser_requires_ranges_and_grounded_quote(self):
        from engine.sentiment import (
            evidence_candidates,
            parse_sentiment_response,
            parse_sentiment_sensor_response,
        )

        score = parse_sentiment_response(
            '{"sentiment": 0.75, "relevance": 0.9, "evidence_quote": "revenue accelerated"}',
            "Quarterly revenue accelerated despite higher costs.",
        )
        self.assertEqual(score.sentiment, 0.75)
        self.assertEqual(score.relevance, 0.9)

        with self.assertRaisesRegex(ValueError, "verbatim"):
            parse_sentiment_response(
                '{"sentiment": 0.75, "relevance": 0.9, "evidence_quote": "invented claim"}',
                "Quarterly revenue accelerated despite higher costs.",
            )
        with self.assertRaisesRegex(ValueError, "sentiment"):
            parse_sentiment_response(
                '{"sentiment": 1.5, "relevance": 0.9, "evidence_quote": "revenue accelerated"}',
                "Quarterly revenue accelerated despite higher costs.",
            )
        content = "Quarterly revenue accelerated despite higher costs and uneven demand."
        candidates = evidence_candidates(content, max_chars=35)
        self.assertTrue(all(candidate in content and len(candidate) <= 35 for candidate in candidates))
        sensor_score = parse_sentiment_sensor_response(
            '{"sentiment": 0.5, "relevance": 0.8, "evidence_index": 0}',
            content,
        )
        self.assertEqual(sensor_score.evidence_quote, evidence_candidates(content)[0])

    def test_sentiment_service_scores_once_then_reuses_cache(self):
        from search.sentiment import SentimentService

        class FakeChat:
            def __init__(self):
                self.calls = 0

            def complete_json(self, messages):
                self.calls += 1
                self.assert_messages = messages
                return '{"sentiment": 0.5, "relevance": 0.8, "evidence_index": 0}'

        class FakeCache:
            def __init__(self):
                self.documents = {}

            def get(self, cache_key):
                return self.documents.get(cache_key)

            def create(self, document):
                self.documents[document["id"]] = document
                return document

        document = {
            "id": "document-1",
            "source_id": "news:42",
            "source_type": "news",
            "revision_hash": "d" * 64,
            "title": "Cloud results",
            "content": "Quarterly revenue accelerated despite higher costs.",
        }
        chat = FakeChat()
        service = SentimentService(chat, FakeCache(), "gpt-4o:2024-11-20")

        first, first_cached = service.score(document)
        replay, replay_cached = service.score(document)

        self.assertFalse(first_cached)
        self.assertTrue(replay_cached)
        self.assertEqual(first, replay)
        self.assertEqual(chat.calls, 1)
        self.assertEqual(first["document_revision_hash"], "d" * 64)
        self.assertEqual(first["prompt_version"], "e7_sentiment_v2")

    def test_sentiment_batch_cursor_progresses_without_overlap(self):
        from search.sentiment import page_evidence_documents

        documents = [
            {"id": "d3", "source_type": "news"},
            {"id": "d1", "source_type": "news"},
            {"id": "d2", "source_type": "news"},
            {"id": "f1", "source_type": "sec_filing"},
        ]
        first, cursor, has_more = page_evidence_documents(documents, limit=2)
        second, final_cursor, final_has_more = page_evidence_documents(
            documents, limit=2, after_id=cursor
        )

        self.assertEqual([document["id"] for document in first], ["d1", "d2"])
        self.assertEqual(cursor, "d2")
        self.assertTrue(has_more)
        self.assertEqual([document["id"] for document in second], ["d3"])
        self.assertEqual(final_cursor, "d3")
        self.assertFalse(final_has_more)


if __name__ == "__main__":
    unittest.main()