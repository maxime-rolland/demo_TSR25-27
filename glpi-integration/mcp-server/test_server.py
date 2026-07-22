import os
import unittest
from unittest.mock import patch, MagicMock

# Force-override rather than setdefault: these tests run inside the same
# hermes-glpi container that has the *real* GLPI credentials set as env
# vars, so setdefault would be a no-op there and silently assert against
# production values instead of these fixtures.
os.environ["GLPI_API_URL"] = "http://testserver/api.php/v2.3"
os.environ["GLPI_OAUTH_CLIENT_ID"] = "test-client-id"
os.environ["GLPI_OAUTH_CLIENT_SECRET"] = "test-client-secret"
os.environ["GLPI_USER"] = "test-user"
os.environ["GLPI_PASSWORD"] = "test-password"

import server


def _fake_response(json_body, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_body
    resp.content = b"1" if json_body is not None else b""
    resp.raise_for_status.return_value = None
    return resp


class GLPIClientTokenTests(unittest.TestCase):
    def setUp(self):
        self.client = server.GLPIClient()

    @patch("server.requests.post")
    def test_password_grant_stores_token(self, mock_post):
        mock_post.return_value = _fake_response(
            {"access_token": "tok-1", "refresh_token": "ref-1", "expires_in": 3600}
        )
        self.client._password_grant()

        self.assertEqual(self.client._access_token, "tok-1")
        self.assertEqual(self.client._refresh_token, "ref-1")
        sent_data = mock_post.call_args.kwargs["data"]
        self.assertEqual(sent_data["grant_type"], "password")
        self.assertEqual(sent_data["username"], "test-user")

    @patch("server.requests.post")
    def test_ensure_token_reuses_valid_token(self, mock_post):
        self.client._access_token = "tok-cached"
        self.client._expires_at = server.time.time() + 3600
        self.client._ensure_token()
        mock_post.assert_not_called()

    @patch("server.requests.post")
    def test_ensure_token_refreshes_when_expired_with_refresh_token(self, mock_post):
        self.client._access_token = "tok-old"
        self.client._refresh_token = "ref-1"
        self.client._expires_at = server.time.time() - 10
        mock_post.return_value = _fake_response(
            {"access_token": "tok-new", "refresh_token": "ref-2", "expires_in": 3600}
        )

        self.client._ensure_token()

        self.assertEqual(self.client._access_token, "tok-new")
        sent_data = mock_post.call_args.kwargs["data"]
        self.assertEqual(sent_data["grant_type"], "refresh_token")

    @patch("server.requests.post")
    def test_ensure_token_falls_back_to_password_grant_when_refresh_fails(self, mock_post):
        self.client._access_token = "tok-old"
        self.client._refresh_token = "ref-1"
        self.client._expires_at = server.time.time() - 10

        refresh_fail = MagicMock()
        refresh_fail.raise_for_status.side_effect = server.requests.HTTPError("refresh failed")
        password_ok = _fake_response(
            {"access_token": "tok-fresh", "refresh_token": "ref-3", "expires_in": 3600}
        )
        mock_post.side_effect = [refresh_fail, password_ok]

        self.client._ensure_token()

        self.assertEqual(self.client._access_token, "tok-fresh")
        self.assertEqual(mock_post.call_count, 2)


class GLPIClientRequestTests(unittest.TestCase):
    def setUp(self):
        self.client = server.GLPIClient()
        self.client._access_token = "tok-1"
        self.client._expires_at = server.time.time() + 3600

    @patch("server.requests.request")
    def test_request_adds_bearer_header(self, mock_request):
        mock_request.return_value = _fake_response({"id": 1})
        result = self.client.request("GET", "/Assistance/Ticket/1")

        self.assertEqual(result, {"id": 1})
        headers = mock_request.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer tok-1")
        self.assertEqual(
            mock_request.call_args.args[1], "http://testserver/api.php/v2.3/Assistance/Ticket/1"
        )


class ToolTests(unittest.TestCase):
    @patch.object(server.glpi, "request")
    def test_add_followup_calls_correct_endpoint(self, mock_request):
        mock_request.return_value = {"id": 5}
        result = server.add_followup(42, "hello", False)

        self.assertEqual(result, {"id": 5})
        mock_request.assert_called_once_with(
            "POST",
            "/Assistance/Ticket/42/Timeline/Followup",
            json={"content": "hello", "is_private": False},
        )

    @patch.object(server.glpi, "request")
    def test_create_kb_article_calls_correct_endpoint(self, mock_request):
        mock_request.return_value = {"id": 9}
        result = server.create_kb_article("Title", "Body", True)

        self.assertEqual(result, {"id": 9})
        mock_request.assert_called_once_with(
            "POST",
            "/Knowledgebase/Article",
            json={"name": "Title", "content": "Body", "is_faq": True},
        )

    @patch.object(server.glpi, "request")
    def test_search_kb_uses_filter_param(self, mock_request):
        mock_request.return_value = []
        server.search_kb('name=like="*wifi*"')
        mock_request.assert_called_once_with(
            "GET",
            "/Knowledgebase/Article",
            params={"filter": 'name=like="*wifi*"'},
        )


class GLPIClientRawModeTests(unittest.TestCase):
    def setUp(self):
        self.client = server.GLPIClient()
        self.client._access_token = "tok-1"
        self.client._expires_at = server.time.time() + 3600

    @patch("server.requests.request")
    def test_raw_mode_returns_bytes_without_parsing_json(self, mock_request):
        resp = MagicMock()
        resp.content = b"\x89PNG raw bytes, not json"
        resp.raise_for_status.return_value = None
        mock_request.return_value = resp

        result = self.client.request(
            "GET", "/Management/Document/1/Download", raw=True
        )

        self.assertEqual(result, b"\x89PNG raw bytes, not json")
        resp.json.assert_not_called()


class GetTicketImagesTests(unittest.TestCase):
    @patch.object(server.glpi, "request")
    def test_filters_to_images_and_downloads_each(self, mock_request):
        mock_request.side_effect = [
            [
                {"id": 10, "mime": "image/png", "filename": "screenshot.png"},
                {"id": 11, "mime": "application/pdf", "filename": "manual.pdf"},
                {"id": 12, "mime": "image/jpeg", "filename": "photo.jpg"},
            ],
            b"PNGDATA",
            b"JPGDATA",
        ]

        images = server.get_ticket_images(42)

        self.assertEqual(len(images), 2)
        self.assertEqual(images[0].data, b"PNGDATA")
        self.assertEqual(images[1].data, b"JPGDATA")
        mock_request.assert_any_call(
            "GET", "/Assistance/Ticket/42/Timeline/Document"
        )
        mock_request.assert_any_call(
            "GET", "/Management/Document/10/Download", raw=True
        )
        mock_request.assert_any_call(
            "GET", "/Management/Document/12/Download", raw=True
        )

    @patch.object(server.glpi, "request")
    def test_caps_at_five_images(self, mock_request):
        docs = [
            {"id": i, "mime": "image/png", "filename": f"img{i}.png"}
            for i in range(8)
        ]
        mock_request.side_effect = [docs] + [b"DATA"] * 8

        images = server.get_ticket_images(42)

        self.assertEqual(len(images), 5)

    @patch.object(server.glpi, "request")
    def test_no_documents_returns_empty_list(self, mock_request):
        mock_request.return_value = []

        images = server.get_ticket_images(42)

        self.assertEqual(images, [])


if __name__ == "__main__":
    unittest.main()
