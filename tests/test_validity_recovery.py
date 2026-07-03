from __future__ import annotations

import base64
import json

from sqlmodel import Session, select

from application.tasks import _run_single_account_check
from core.account_graph import patch_account_graph
from core.base_platform import RegisterConfig
from core.db import AccountModel, AccountOverviewModel, engine
from core.lifecycle import check_accounts_validity
from core.proxy_pool import proxy_pool
from platforms.chatgpt import payment
from platforms.chatgpt.plugin import ChatGPTPlatform


def _jwt(payload: dict) -> str:
    def enc(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(payload)}."


class _AlwaysValidPlatform:
    def __init__(self, config: RegisterConfig | None = None):
        self.config = config

    def check_valid(self, account) -> bool:
        return True


class _AlwaysInvalidPlatform:
    def __init__(self, config: RegisterConfig | None = None):
        self.config = config

    def check_valid(self, account) -> bool:
        return False


def _create_account(*, platform: str = "chatgpt", lifecycle_status: str = "registered") -> int:
    with Session(engine) as session:
        model = AccountModel(platform=platform, email=f"{platform}@example.com", password="secret")
        session.add(model)
        session.commit()
        session.refresh(model)
        patch_account_graph(
            session,
            model,
            lifecycle_status=lifecycle_status,
            summary_updates={"valid": lifecycle_status != "invalid"},
        )
        session.commit()
        return int(model.id or 0)


def _overview(account_id: int):
    with Session(engine) as session:
        return session.exec(
            select(AccountOverviewModel).where(AccountOverviewModel.account_id == account_id)
        ).one()


def test_single_account_check_recovers_previously_invalid_account(monkeypatch):
    account_id = _create_account(lifecycle_status="invalid")
    monkeypatch.setattr("application.tasks.get", lambda _platform: _AlwaysValidPlatform)

    valid, result = _run_single_account_check(account_id)

    assert valid is True
    assert result["valid"] is True
    overview = _overview(account_id)
    assert overview.lifecycle_status == "registered"
    assert overview.validity_status == "valid"
    assert overview.display_status == "registered"
    assert overview.checked_at


def test_lifecycle_validity_check_does_not_overwrite_lifecycle_status(monkeypatch):
    account_id = _create_account(lifecycle_status="registered")
    monkeypatch.setattr("core.lifecycle.get", lambda _platform: _AlwaysInvalidPlatform)

    results = check_accounts_validity(platform="chatgpt", limit=10)

    assert results["invalid"] == 1
    overview = _overview(account_id)
    assert overview.lifecycle_status == "registered"
    assert overview.validity_status == "invalid"
    assert overview.display_status == "invalid"
    assert overview.checked_at


def test_chatgpt_subscription_status_falls_back_to_wham_usage(monkeypatch):
    captured_headers: dict[str, str] = {}

    class _Resp:
        def __init__(self, data=None, error: Exception | None = None):
            self._data = data
            self._error = error

        def raise_for_status(self):
            if self._error:
                raise self._error

        def json(self):
            return self._data

    def _fake_get(url, **kwargs):
        if url.endswith("/backend-api/me"):
            return _Resp(error=RuntimeError("403"))
        captured_headers.update(kwargs.get("headers") or {})
        return _Resp(data={"plan_type": "free"})

    monkeypatch.setattr(payment.cffi_requests, "get", _fake_get)
    account = type(
        "AccountStub",
        (),
        {
            "access_token": "token",
            "cookies": "",
            "id_token": json.dumps({"chatgpt_account_id": "acct-123"}),
            "extra": {},
        },
    )()

    status = payment.check_subscription_status(account)

    assert status == "free"
    assert captured_headers["Authorization"] == "Bearer token"
    assert captured_headers["Chatgpt-Account-Id"] == "acct-123"


def test_chatgpt_subscription_status_detects_k12_workspace_from_me(monkeypatch):
    captured_headers: dict[str, str] = {}

    class _Resp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    def _fake_get(url, **kwargs):
        captured_headers[url] = dict(kwargs.get("headers") or {})
        if url.endswith("/backend-api/me"):
            return _Resp(
                {
                    "plan_type": "free",
                    "orgs": {
                        "data": [
                            {
                                "id": "631e1603-06cf-4f0b-b79b-d09fbfcfe98d",
                                "name": "schools.nyc.gov Workspace #82254",
                                "settings": {"workspace_plan_type": "chatgpt_for_teachers"},
                            }
                        ]
                    },
                }
            )
        return _Resp({"plan_type": "free"})

    monkeypatch.setattr(payment.cffi_requests, "get", _fake_get)
    account = type(
        "AccountStub",
        (),
        {
            "access_token": "token",
            "cookies": "",
            "id_token": _jwt(
                {
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "acct-workspace",
                        "chatgpt_plan_type": "workspace",
                    }
                }
            ),
            "extra": {},
        },
    )()

    details = payment.fetch_subscription_status_details(account)

    assert details["status"] == "workspace"
    assert details["plan_name"] == "ChatGPT for Teachers"
    assert details["workspace_id"] == "631e1603-06cf-4f0b-b79b-d09fbfcfe98d"
    assert "K12" in details["chips"]
    assert "Teachers" in details["chips"]
    assert details["chatgpt_account_id"] == "acct-workspace"
    assert captured_headers["https://chatgpt.com/backend-api/me"]["Chatgpt-Account-Id"] == "acct-workspace"


def test_chatgpt_check_valid_marks_k12_workspace_subscribed(monkeypatch):
    def _fake_status(account, proxy=None):
        return {
            "status": "workspace",
            "source": "backend-api/me",
            "plan_name": "ChatGPT for Teachers",
            "membership_type": "workspace",
            "workspace_id": "631e1603-06cf-4f0b-b79b-d09fbfcfe98d",
            "workspace_name": "schools.nyc.gov Workspace #82254",
            "workspace_plan_type": "chatgpt_for_teachers",
            "chips": ["K12", "Workspace", "Teachers"],
            "usage": {"plan_type": "free"},
        }

    monkeypatch.setattr(payment, "fetch_subscription_status_details", _fake_status)
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": None)

    plugin = ChatGPTPlatform.__new__(ChatGPTPlatform)
    plugin.config = RegisterConfig()
    plugin.mailbox = None
    account = type(
        "AccountStub",
        (),
        {
            "token": "token",
            "region": "",
            "extra": {"access_token": "token", "id_token": "", "cookies": ""},
        },
    )()

    assert plugin.check_valid(account) is True
    overview = plugin.get_last_check_overview()
    assert overview["plan"] == "workspace"
    assert overview["plan_state"] == "subscribed"
    assert overview["plan_name"] == "ChatGPT for Teachers"
    assert overview["membership_type"] == "workspace"
    assert overview["workspace_id"] == "631e1603-06cf-4f0b-b79b-d09fbfcfe98d"
    assert overview["workspace_plan_type"] == "chatgpt_for_teachers"
    assert overview["chips"] == ["K12", "Workspace", "Teachers"]
    assert overview["chatgpt_usage"] == {"plan_type": "free"}


def test_chatgpt_check_valid_uses_proxy_pool_before_direct(monkeypatch):
    calls: list[str | None] = []
    proxy_events: list[tuple[str, str]] = []

    def _fake_status(account, proxy=None):
        calls.append(proxy)
        if proxy != "http://127.0.0.1:7890":
            raise RuntimeError("should use proxy first")
        return {
            "status": "free",
            "source": "backend-api/wham/usage",
            "usage": {"plan_type": "free"},
        }

    monkeypatch.setattr(payment, "fetch_subscription_status_details", _fake_status)
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": "http://127.0.0.1:7890")
    monkeypatch.setattr(proxy_pool, "report_success", lambda url: proxy_events.append(("success", url)))
    monkeypatch.setattr(proxy_pool, "report_fail", lambda url: proxy_events.append(("fail", url)))

    plugin = ChatGPTPlatform.__new__(ChatGPTPlatform)
    plugin.config = RegisterConfig()
    plugin.mailbox = None
    account = type(
        "AccountStub",
        (),
        {
            "token": "token",
            "region": "",
            "extra": {
                "access_token": "token",
                "id_token": "",
                "cookies": "",
            },
        },
    )()

    assert plugin.check_valid(account) is True
    assert calls == ["http://127.0.0.1:7890"]
    assert proxy_events == [("success", "http://127.0.0.1:7890")]
    assert plugin.get_last_check_overview()["chatgpt_usage"] == {"plan_type": "free"}
