"""S2: the reply classifier's ChatClient and the Microsoft 365 / account-sync HTTP come from providers."""
from types import SimpleNamespace

import pytest

from app.adapters import openai_chat, providers
from app.adapters.openai_chat import ChatCompletionsView, OpenAIChatClient
from app.core.config import settings
from app.domain.inbox import LabelSource, ReplyLabel
from app.services import account_sync, ms365_oauth, reply_classifier


# ── reply classifier ───────────────────────────────────────────

class FakeChat:
    def __init__(self, content='{"label": "POSITIVE", "confidence": 0.9}'):
        self.content, self.calls = content, []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


class FakeBudget:
    def __init__(self):
        self.ledger = []

    def charge(self, kwargs):
        self.ledger.append(("charge", kwargs["max_tokens"]))
        return kwargs, 42

    def settle(self, reserved, response=None):
        self.ledger.append(("settle", reserved, response is not None))


def test_classifier_uses_the_provided_chat_client_through_the_budget():
    chat, budget = FakeChat(), FakeBudget()
    with providers.override(chat_client=chat):
        result = reply_classifier.llm_classify("Re: hi", "Sounds good, let's talk Tuesday", budget=budget)
    assert result.label is ReplyLabel.POSITIVE and result.source is LabelSource.LLM
    assert budget.ledger == [("charge", reply_classifier.LLM_MAX_TOKENS), ("settle", 42, True)]
    [(messages, kwargs)] = chat.calls
    assert messages[0]["role"] == "system" and kwargs == {"max_tokens": reply_classifier.LLM_MAX_TOKENS}


def test_classifier_without_a_key_makes_no_call_and_spends_nothing():
    budget = FakeBudget()
    with providers.override(chat_client=None):
        assert reply_classifier.llm_classify("Re: hi", "Not interested", budget=budget) is None
    assert budget.ledger == []


def test_openai_client_kwargs_override_the_defaults(monkeypatch):
    sent = []
    client = OpenAIChatClient.__new__(OpenAIChatClient)
    client._model = "m-default"
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: sent.append(kw) or "resp")))
    assert client.complete([{"role": "user", "content": "x"}], max_tokens=5) == "resp"
    assert sent[-1] == {"messages": [{"role": "user", "content": "x"}], "model": "m-default", "temperature": 0,
                        "max_tokens": 5}
    client.complete([], model="m-other", temperature=0.7)
    assert sent[-1]["model"] == "m-other" and sent[-1]["temperature"] == 0.7


def test_generation_client_keeps_the_sdk_defaults(monkeypatch):
    built = []
    monkeypatch.setattr(openai_chat, "OpenAI", lambda **kw: built.append(kw) or SimpleNamespace())
    monkeypatch.setattr(openai_chat, "_generation_client", None)
    monkeypatch.setattr(openai_chat, "_client", None)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test-not-used")
    assert openai_chat.generation_chat_client() is openai_chat.generation_chat_client()
    assert openai_chat.chat_client() is not None
    assert built == [{"api_key": "sk-test-not-used"},
                     {"api_key": "sk-test-not-used", "timeout": openai_chat.LLM_TIMEOUT_SECONDS, "max_retries": 0}]


def test_sdk_shaped_view_forwards_to_complete():
    chat = FakeChat()
    view = ChatCompletionsView(chat)
    view.chat.completions.create(model="m", messages=[{"role": "user", "content": "x"}], temperature=0.8)
    assert chat.calls == [([{"role": "user", "content": "x"}], {"model": "m", "temperature": 0.8})]


# ── Microsoft 365 / account sync HTTP ──────────────────────────

class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = {"content-type": "application/json; charset=utf-8"}

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, response):
        self.response, self.posts, self.requests = response, [], []

    def post_form(self, url, data, *, timeout=20):
        self.posts.append((url, dict(data), timeout))
        return self.response

    def bearer_request(self, method, url, token, *, params=None, json=None, timeout=30):
        self.requests.append((method, url, token, params, json, timeout))
        return self.response


@pytest.fixture
def ms365_settings(monkeypatch):
    monkeypatch.setattr(settings, "MS365_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "MS365_CLIENT_SECRET", "secret")
    monkeypatch.setattr(settings, "MS365_TENANT_ID", "")


def test_ms365_token_request_goes_through_the_adapter(ms365_settings):
    http = FakeHttp(FakeResponse(body={"access_token": "a", "expires_in": 60}))
    with providers.override(oauth_http=http):
        assert ms365_oauth._token_request({"grant_type": "refresh_token", "refresh_token": "r"})["access_token"] == "a"
    [(url, data, timeout)] = http.posts
    assert url == "https://login.microsoftonline.com/common/oauth2/v2.0/token" and timeout == 20
    assert data == {"client_id": "cid", "client_secret": "secret", "scope": ms365_oauth.SCOPES,
                    "grant_type": "refresh_token", "refresh_token": "r"}


def test_ms365_token_errors_are_reported(ms365_settings):
    http = FakeHttp(FakeResponse(400, {"error": "invalid_grant", "error_description": "Token expired"}))
    with providers.override(oauth_http=http), pytest.raises(ms365_oauth.OAuthError, match="Token expired"):
        ms365_oauth._token_request({"grant_type": "refresh_token", "refresh_token": "r"})


def test_account_sync_http_goes_through_the_adapter():
    http = FakeHttp(FakeResponse(body={"value": []}))
    with providers.override(oauth_http=http):
        account_sync.http_request("GET", f"{account_sync.GRAPH}/me", "tok", params={"$select": "mail"})
        account_sync.http_post("https://oauth2.googleapis.com/token", {"code": "c"})
    assert http.requests == [("GET", "https://graph.microsoft.com/v1.0/me", "tok", {"$select": "mail"}, None, 30)]
    assert http.posts == [("https://oauth2.googleapis.com/token", {"code": "c"}, 20)]
