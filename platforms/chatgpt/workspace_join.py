from __future__ import annotations

import time
import uuid
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from core.base_mailbox import MailboxAccount
from core.config_store import config_store
from .cpa_session import export_workspace_cpa_session_from_browser
from .constants import CHATGPT_APP


DEFAULT_WORKSPACE_IDS = "631e1603-06cf-4f0b-b79b-d09fbfcfe98d"
WORKSPACE_IDS_CONFIG_KEY = "chatgpt_default_workspace_ids"


def get_default_workspace_ids() -> str:
    return str(config_store.get(WORKSPACE_IDS_CONFIG_KEY, DEFAULT_WORKSPACE_IDS) or "").strip() or DEFAULT_WORKSPACE_IDS


def parse_workspace_ids(raw: Any) -> list[str]:
    text = str(raw or "").strip() or get_default_workspace_ids()
    normalized = text.replace(",", "\n")
    return [item.strip() for item in normalized.splitlines() if item.strip()]


def _bool_config(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _int_config(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def workspace_join_enabled(extra: dict[str, Any] | None) -> bool:
    cfg = dict((extra or {}).get("chatgpt_workspace_join") or {})
    return _bool_config((extra or {}).get("auto_chatgpt_workspace_join", cfg.get("enabled")), False)


def workspace_join_config(extra: dict[str, Any] | None) -> dict[str, Any]:
    source = dict((extra or {}).get("chatgpt_workspace_join") or {})
    source.setdefault("workspace_ids", (extra or {}).get("workspace_ids", get_default_workspace_ids()))
    source.setdefault("enabled", workspace_join_enabled(extra))
    source.setdefault("route", "request")
    source.setdefault("accept_invite", True)
    source.setdefault("export_cpa_json", True)
    source.setdefault("cpa_output_dir", "")
    source.setdefault("interval_ms", 1500)
    source.setdefault("max_retries", 3)
    source.setdefault("retry_backoff_ms", 5000)
    source.setdefault("invite_timeout", 240)
    return source


def _log(log: Callable[[str], None] | None, message: str) -> None:
    if callable(log):
        try:
            log(message)
        except Exception:
            pass


def _ensure_chatgpt_origin(page, log: Callable[[str], None] | None) -> None:
    current_url = str(getattr(page, "url", "") or "")
    if "chatgpt.com" in current_url.lower():
        return
    _log(log, "Workspace Join: 当前页不在 chatgpt.com，先打开 ChatGPT 首页")
    page.goto(f"{CHATGPT_APP}/", wait_until="domcontentloaded", timeout=30000)


def _fetch_access_token_from_page(page, log: Callable[[str], None] | None) -> str:
    _ensure_chatgpt_origin(page, log)
    data = page.evaluate(
        """
        async () => {
          const response = await fetch("/api/auth/session", {
            headers: { accept: "*/*" },
            credentials: "include",
          });
          const text = await response.text().catch(() => "");
          let json = {};
          try { json = text ? JSON.parse(text) : {}; } catch (_) {}
          return {
            ok: response.ok,
            status: response.status,
            accessToken: json.accessToken || json.access_token || "",
            text: text.slice(0, 300),
          };
        }
        """
    )
    result = dict(data or {})
    token = str(result.get("accessToken") or "").strip()
    if token:
        _log(log, "Workspace Join: got accessToken from current ChatGPT page")
        return token
    raise RuntimeError(
        f"ChatGPT session did not return accessToken: HTTP {result.get('status')} "
        f"{str(result.get('text') or '')[:160]}"
    )


def request_workspace_join_in_browser(
    page,
    *,
    access_token: str = "",
    workspace_ids: list[str],
    route: str = "request",
    interval_ms: int = 1500,
    max_retries: int = 3,
    retry_backoff_ms: int = 5000,
    log: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    _ensure_chatgpt_origin(page, log)
    fallback_token = str(access_token or "").strip()
    try:
        access_token = _fetch_access_token_from_page(page, log)
    except Exception as exc:
        _log(log, f"Workspace Join: page session fetch failed, using registration token if available: {exc}")
        access_token = fallback_token
    if not access_token:
        raise RuntimeError("缺少 ChatGPT access_token，无法发送 workspace join request")

    results: list[dict[str, Any]] = []
    device_id = str(uuid.uuid4())
    normalized_route = str(route or "request").strip() or "request"

    for index, ws_id in enumerate(workspace_ids):
        last_result: dict[str, Any] = {}
        for attempt in range(max(int(max_retries), 0) + 1):
            _log(
                log,
                f"Workspace Join: POST /accounts/{ws_id[:8]}/invites/{normalized_route} "
                f"(第 {attempt + 1} 次)",
            )
            result = page.evaluate(
                """
                async ({ wsId, route, token, deviceId }) => {
                  const response = await fetch(`/backend-api/accounts/${wsId}/invites/${route}`, {
                    method: "POST",
                    credentials: "include",
                    mode: "cors",
                    headers: {
                      accept: "*/*",
                      authorization: `Bearer ${token}`,
                      "content-type": "application/json",
                      "oai-device-id": deviceId,
                      "oai-language": navigator.language || "en-US",
                    },
                    body: "",
                  });
                  const text = await response.text().catch(() => "");
                  return {
                    ok: response.ok,
                    status: response.status,
                    url: response.url,
                    text: text.slice(0, 500),
                  };
                }
                """,
                {
                    "wsId": ws_id,
                    "route": normalized_route,
                    "token": access_token,
                    "deviceId": device_id,
                },
            )
            last_result = dict(result or {})
            last_result["workspace_id"] = ws_id
            if last_result.get("ok"):
                _log(log, f"Workspace Join: {ws_id[:8]} request 成功 HTTP {last_result.get('status')}")
                break
            if last_result.get("status") in (401, 403) and attempt < max(int(max_retries), 0):
                _log(log, "Workspace Join: accessToken rejected, refreshing session from page")
                try:
                    access_token = _fetch_access_token_from_page(page, log)
                except Exception as exc:
                    _log(log, f"Workspace Join: session refresh failed, retrying with previous token: {exc}")
            _log(
                log,
                f"Workspace Join: {ws_id[:8]} request 失败 HTTP {last_result.get('status')}: "
                f"{str(last_result.get('text') or '')[:180]}",
            )
            if attempt < max(int(max_retries), 0):
                time.sleep(max(int(retry_backoff_ms), 0) / 1000)
        results.append(last_result)
        if index < len(workspace_ids) - 1:
            time.sleep(max(int(interval_ms), 0) / 1000)
    return results


def open_workspace_invite_in_browser(
    page,
    invite_url: str,
    *,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    url = str(invite_url or "").strip()
    if not url:
        return {"ok": False, "error": "empty invite url"}

    invite_workspace_id = _workspace_id_from_invite_url(url, [])
    _log(log, f"Workspace Join: open invite link wId={invite_workspace_id or '-'}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_timeout(1000)
    except Exception:
        time.sleep(1)

    clicked = False
    clicked_text = ""
    try:
        click_result = page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== "none" && style.visibility !== "hidden" &&
                  rect.width > 0 && rect.height > 0;
              };
              const pattern = /加入工作空间|加入工作區|转至\\s*ChatGPT\\s*for\\s*Teachers|轉至\\s*ChatGPT\\s*for\\s*Teachers|Go to\\s*ChatGPT\\s*for\\s*Teachers|Join workspace|Accept invite|Accept invitation|Continue/i;
              const nodes = Array.from(document.querySelectorAll("button, a, [role='button']"));
              const target = nodes.find((el) => visible(el) && pattern.test(String(el.innerText || el.textContent || el.getAttribute("aria-label") || "")));
              if (!target) return { clicked: false, text: "", url: location.href };
              const text = String(target.innerText || target.textContent || target.getAttribute("aria-label") || "").trim();
              target.click();
              return { clicked: true, text, url: location.href };
            }
            """
        )
        clicked = bool((click_result or {}).get("clicked"))
        clicked_text = str((click_result or {}).get("text") or "")
    except Exception as exc:
        _log(log, f"Workspace Join: 邀请页按钮点击探测失败，继续观察页面: {exc}")

    if not clicked:
        last_candidates: list[str] = []
        last_error = ""
        click_script = """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style && style.display !== "none" && style.visibility !== "hidden" &&
                  rect.width > 0 && rect.height > 0;
              };
              const textOf = (el) => String(
                el.innerText || el.textContent || el.getAttribute("aria-label") || ""
              ).replace(/\\s+/g, " ").trim();
              const pattern = /加入工作空间|加入工作區|转至\\s*ChatGPT\\s*for\\s*Teachers|轉至\\s*ChatGPT\\s*for\\s*Teachers|Go to\\s*ChatGPT\\s*for\\s*Teachers|ChatGPT\\s*for\\s*Teachers|Join workspace|Accept invite|Accept invitation|Continue/i;
              const nodes = Array.from(document.querySelectorAll("button, a, [role='button'], [role='link']"));
              const candidates = nodes
                .filter((el) => visible(el))
                .map((el) => textOf(el))
                .filter(Boolean)
                .slice(0, 12);
              const target = nodes.find((el) => visible(el) && pattern.test(textOf(el)));
              if (!target) return { clicked: false, text: "", candidates, url: location.href };
              const text = textOf(target);
              try { target.scrollIntoView({ block: "center", inline: "center" }); } catch (_) {}
              target.click();
              return { clicked: true, text, candidates, url: location.href };
            }
            """
        deadline = time.monotonic() + 30
        attempts = 0
        while attempts < 60 and time.monotonic() <= deadline:
            attempts += 1
            try:
                click_result = page.evaluate(click_script)
                clicked = bool((click_result or {}).get("clicked"))
                clicked_text = str((click_result or {}).get("text") or "")
                raw_candidates = (click_result or {}).get("candidates") or []
                if isinstance(raw_candidates, list):
                    last_candidates = [
                        str(item)[:80] for item in raw_candidates if str(item).strip()
                    ]
                if clicked:
                    break
            except Exception as exc:
                last_error = str(exc)
            if time.monotonic() > deadline:
                break
            try:
                page.wait_for_timeout(500)
            except Exception:
                time.sleep(0.5)
        if last_error and not clicked:
            _log(log, f"Workspace Join: invite button click probe failed: {last_error}")
        if last_candidates and not clicked:
            _log(log, f"Workspace Join: invite page button candidates: {last_candidates}")

    try:
        page.wait_for_timeout(2500 if clicked else 1200)
    except Exception:
        time.sleep(2.5 if clicked else 1.2)

    final_url = str(getattr(page, "url", "") or "")
    if clicked:
        _log(log, f"Workspace Join: 已点击邀请页按钮 {clicked_text or '-'}")
    if not clicked:
        _log(log, "Workspace Join: invite button not clicked; acceptance not confirmed")
    return {
        "ok": clicked,
        "invite_url": url,
        "clicked": clicked,
        "clicked_text": clicked_text,
        "final_url": final_url,
        "error": "" if clicked else "invite button not clicked",
    }


def _workspace_id_from_invite_url(invite_url: str, fallback_ids: list[str]) -> str:
    try:
        values = parse_qs(urlparse(str(invite_url or "")).query)
        for key in ("wId", "wid", "workspace_id", "workspaceId"):
            value = (values.get(key) or [""])[0]
            if value:
                return str(value)
    except Exception:
        pass
    return fallback_ids[0] if fallback_ids else ""


def run_workspace_join_flow(
    page,
    session_info: dict[str, Any],
    *,
    mailbox,
    mailbox_account: MailboxAccount | None,
    config: dict[str, Any],
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    workspace_ids = parse_workspace_ids(config.get("workspace_ids"))
    if not workspace_ids:
        return {"workspace_join": {"ok": False, "error": "no workspace ids"}}

    result: dict[str, Any] = {
        "ok": False,
        "workspace_ids": workspace_ids,
        "request_results": [],
        "invite_url": "",
        "accept_result": None,
        "cpa_export": None,
    }
    top_level_updates: dict[str, Any] = {}

    before_ids = set()
    if mailbox is not None and mailbox_account is not None:
        try:
            before_ids = set(mailbox.get_current_ids(mailbox_account) or set())
            _log(log, f"Workspace Join: 邮箱邀请基线 before_ids={len(before_ids)}")
        except Exception as exc:
            _log(log, f"Workspace Join: 邮箱基线读取失败，继续等待新邮件: {exc}")
    else:
        _log(log, "Workspace Join: 缺少 mailbox 上下文，将只发送 request，不自动收邀请")

    try:
        request_results = request_workspace_join_in_browser(
            page,
            access_token=str(session_info.get("access_token") or ""),
            workspace_ids=workspace_ids,
            route=str(config.get("route") or "request"),
            interval_ms=_int_config(config.get("interval_ms"), 1500),
            max_retries=_int_config(config.get("max_retries"), 3),
            retry_backoff_ms=_int_config(config.get("retry_backoff_ms"), 5000),
            log=log,
        )
        result["request_results"] = request_results
        result["request_ok"] = all(bool(item.get("ok")) for item in request_results)
    except Exception as exc:
        result["error"] = f"workspace request failed: {exc}"
        return {"workspace_join": result}

    if not result.get("request_ok"):
        result["error"] = "workspace request failed"
        return {"workspace_join": result}

    if not _bool_config(config.get("accept_invite"), True):
        result["ok"] = True
        return {"workspace_join": result}

    if mailbox is None or mailbox_account is None:
        result["ok"] = True
        result["warning"] = "mailbox context missing; invite acceptance skipped"
        return {"workspace_join": result}

    try:
        timeout = _int_config(config.get("invite_timeout"), 240)
        _log(log, f"Workspace Join: 等待邀请邮件 k12-invite，timeout={timeout}s")
        invite_url = mailbox.wait_for_link(
            mailbox_account,
            keyword="k12-invite",
            timeout=timeout,
            before_ids=before_ids or None,
        )
        result["invite_url"] = str(invite_url or "")
    except Exception as exc:
        result["error"] = f"wait invite failed: {exc}"
        return {"workspace_join": result}

    accept_result = open_workspace_invite_in_browser(page, result["invite_url"], log=log)
    result["accept_result"] = accept_result
    result["ok"] = bool(accept_result.get("ok"))

    if result.get("ok") and _bool_config(config.get("export_cpa_json"), True):
        workspace_id = _workspace_id_from_invite_url(result["invite_url"], workspace_ids)
        try:
            _log(log, "Workspace Join: start switching workspace and exporting CPA JSON")
            export_result = export_workspace_cpa_session_from_browser(
                page,
                workspace_id=workspace_id,
                output_dir=str(config.get("cpa_output_dir") or "").strip() or None,
                log=log,
            )
            if not isinstance(export_result, dict) or not export_result.get("ok"):
                raise RuntimeError(f"unexpected export result: {export_result}")
            result["cpa_export"] = {
                key: value
                for key, value in export_result.items()
                if key not in {"access_token", "refresh_token", "id_token", "session_token"}
            }
            marker = getattr(mailbox, "mark_cpa_export_success", None)
            if callable(marker) and mailbox_account is not None:
                try:
                    marker(mailbox_account, export_path=str(export_result.get("path") or ""))
                    _log(log, "Workspace Join: CPA JSON 已导出，邮箱池状态已标记为已用")
                except Exception as exc:
                    _log(log, f"Workspace Join: 邮箱池状态标记失败（忽略）: {exc}")
            for source_key, target_key in (
                ("access_token", "access_token"),
                ("refresh_token", "refresh_token"),
                ("id_token", "id_token"),
                ("session_token", "session_token"),
                ("account_id", "account_id"),
                ("expired", "expires_at"),
            ):
                value = export_result.get(source_key)
                if value not in (None, ""):
                    top_level_updates[target_key] = value
            if workspace_id:
                top_level_updates["workspace_id"] = workspace_id
        except Exception as exc:
            error = f"CPA JSON export failed: {exc}"
            result["cpa_export"] = {"ok": False, "error": str(exc)}
            result["ok"] = False
            result["error"] = error
            _log(log, f"Workspace Join: {error}")

    return {"workspace_join": result, **top_level_updates}
