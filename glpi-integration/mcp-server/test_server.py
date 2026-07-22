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
os.environ["GLPI_ESCALATION_USER"] = "test-escalation-user"

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


def _timeline_link(document_id):
    """Shape of one entry from GET .../Timeline/Document -- confirmed live
    against the real GLPI v2.3 API: it's a wrapper around the document id,
    NOT the flat Document object (which has to be fetched separately)."""
    return {
        "type": "Document",
        "item": {"documents_id": document_id, "itemtype": "Ticket"},
    }


class GetTicketImagesTests(unittest.TestCase):
    @patch.object(server.glpi, "request")
    def test_filters_to_images_and_downloads_each(self, mock_request):
        mock_request.side_effect = [
            [_timeline_link(10), _timeline_link(11), _timeline_link(12)],
            {"id": 10, "mime": "image/png", "filename": "screenshot.png"},
            b"PNGDATA",
            {"id": 11, "mime": "application/pdf", "filename": "manual.pdf"},
            {"id": 12, "mime": "image/jpeg", "filename": "photo.jpg"},
            b"JPGDATA",
        ]

        images = server.get_ticket_images(42)

        self.assertEqual(len(images), 2)
        self.assertEqual(images[0].data, b"PNGDATA")
        self.assertEqual(images[1].data, b"JPGDATA")
        mock_request.assert_any_call(
            "GET", "/Assistance/Ticket/42/Timeline/Document"
        )
        mock_request.assert_any_call("GET", "/Management/Document/10")
        mock_request.assert_any_call(
            "GET", "/Management/Document/10/Download", raw=True
        )
        mock_request.assert_any_call("GET", "/Management/Document/11")
        mock_request.assert_any_call("GET", "/Management/Document/12")
        mock_request.assert_any_call(
            "GET", "/Management/Document/12/Download", raw=True
        )

    @patch.object(server.glpi, "request")
    def test_caps_at_five_images(self, mock_request):
        links = [_timeline_link(i) for i in range(8)]
        per_doc = []
        for i in range(8):
            per_doc.append({"id": i, "mime": "image/png", "filename": f"img{i}.png"})
            per_doc.append(b"DATA")
        mock_request.side_effect = [links] + per_doc

        images = server.get_ticket_images(42)

        self.assertEqual(len(images), 5)
        # only 5 metadata + 5 download calls should have happened, plus the
        # initial Timeline/Document call -- never the 6th-8th documents'.
        self.assertEqual(mock_request.call_count, 1 + 5 + 5)

    @patch.object(server.glpi, "request")
    def test_no_documents_returns_empty_list(self, mock_request):
        mock_request.return_value = []

        images = server.get_ticket_images(42)

        self.assertEqual(images, [])

    @patch.object(server.glpi, "request")
    def test_none_response_treated_as_no_documents(self, mock_request):
        mock_request.return_value = None

        images = server.get_ticket_images(42)

        self.assertEqual(images, [])

    @patch.object(server.glpi, "request")
    def test_document_deleted_between_link_and_metadata_lookup_is_skipped(
        self, mock_request
    ):
        # Realistic race: a document was unlinked/removed after the
        # Timeline/Document call already returned it, so the follow-up
        # metadata fetch comes back None instead of raising.
        mock_request.side_effect = [
            [_timeline_link(10), _timeline_link(11)],
            None,
            {"id": 11, "mime": "image/png", "filename": "still_here.png"},
            b"PNGDATA",
        ]

        images = server.get_ticket_images(42)

        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].data, b"PNGDATA")


class ResolveUserIdTests(unittest.TestCase):
    @patch.object(server.glpi, "request")
    def test_resolves_and_caches(self, mock_request):
        mock_request.return_value = [
            {"id": 4, "username": "tech"},
            {"id": 7, "username": "hermes-bot"},
        ]
        # Exercise the patched singleton itself, not a fresh GLPIClient():
        # @patch.object(server.glpi, "request") only shadows `request` on
        # the `server.glpi` instance, so a separately-constructed client
        # would still hit the real (unpatched) HTTP request method.
        client = server.glpi

        user_id = client.resolve_user_id("hermes-bot")
        user_id_again = client.resolve_user_id("hermes-bot")

        self.assertEqual(user_id, 7)
        self.assertEqual(user_id_again, 7)
        mock_request.assert_called_once_with("GET", "/Administration/User")

    @patch.object(server.glpi, "request")
    def test_raises_for_unknown_username(self, mock_request):
        mock_request.return_value = [{"id": 4, "username": "tech"}]
        client = server.glpi

        with self.assertRaises(ValueError):
            client.resolve_user_id("does-not-exist")


def _timeline_followup(content, is_private, author_name):
    """Shape of one entry from GET .../Timeline/Followup -- the same
    timeline-wrapper pattern already confirmed live for Timeline/Document
    in Task 7 (see that fix's commit message), not a flat object."""
    return {
        "type": "Followup",
        "item": {
            "content": content,
            "is_private": is_private,
            "date": "2026-07-22T12:00:00+00:00",
            "user": {"id": 1, "name": author_name},
        },
    }


class GetTicketFollowupsTests(unittest.TestCase):
    @patch.object(server.glpi, "request")
    def test_simplifies_timeline_wrapper_shape(self, mock_request):
        mock_request.return_value = [
            _timeline_followup("Bonjour", False, "hermes-bot"),
            _timeline_followup("Merci !", False, "ivan"),
        ]

        followups = server.get_ticket_followups(9)

        self.assertEqual(len(followups), 2)
        self.assertEqual(
            followups[0],
            {
                "content": "Bonjour",
                "is_private": False,
                "date": "2026-07-22T12:00:00+00:00",
                "author_name": "hermes-bot",
            },
        )
        self.assertEqual(followups[1]["author_name"], "ivan")

    @patch.object(server.glpi, "request")
    def test_no_followups_returns_empty_list(self, mock_request):
        mock_request.return_value = None
        self.assertEqual(server.get_ticket_followups(9), [])


class AssignSelfTests(unittest.TestCase):
    @patch.object(server.glpi, "resolve_user_id")
    @patch.object(server.glpi, "request")
    def test_assigns_resolved_self_id(self, mock_request, mock_resolve):
        mock_resolve.return_value = 7
        mock_request.return_value = {"id": 1, "href": "/Assistance/Ticket/9/TeamMember/1"}

        result = server.assign_self(9)

        mock_resolve.assert_called_once_with(server.GLPI_USER)
        mock_request.assert_called_once_with(
            "POST",
            "/Assistance/Ticket/9/TeamMember",
            json={"type": "User", "id": 7, "role": "assigned"},
        )
        self.assertEqual(result, {"id": 1, "href": "/Assistance/Ticket/9/TeamMember/1"})


class EscalateTicketTests(unittest.TestCase):
    @patch.object(server.glpi, "resolve_user_id")
    @patch.object(server.glpi, "request")
    def test_assigns_escalation_user_and_posts_private_reason(
        self, mock_request, mock_resolve
    ):
        mock_resolve.return_value = 4
        mock_request.side_effect = [
            {"id": 1, "href": "/Assistance/Ticket/9/TeamMember/1"},
            {"id": 2, "href": "/Assistance/Ticket/9/Timeline/Followup/2"},
        ]

        result = server.escalate_ticket(
            9, "Le client dit que ca ne marche toujours pas."
        )

        mock_resolve.assert_called_once_with(server.ESCALATION_USER)
        mock_request.assert_any_call(
            "POST",
            "/Assistance/Ticket/9/TeamMember",
            json={"type": "User", "id": 4, "role": "assigned"},
        )
        mock_request.assert_any_call(
            "POST",
            "/Assistance/Ticket/9/Timeline/Followup",
            json={
                "content": "Le client dit que ca ne marche toujours pas.",
                "is_private": True,
            },
        )
        self.assertEqual(result, {"id": 1, "href": "/Assistance/Ticket/9/TeamMember/1"})


if __name__ == "__main__":
    unittest.main()
