"""Tests for plugin-extensible approval card registry + Feishu routing."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_feishu_mocks() -> None:
    if importlib.util.find_spec("lark_oapi") is None and "lark_oapi" not in sys.modules:
        mod = MagicMock()
        for name in (
            "lark_oapi", "lark_oapi.api.im.v1",
            "lark_oapi.event", "lark_oapi.event.callback_type",
        ):
            sys.modules.setdefault(name, mod)
    if importlib.util.find_spec("aiohttp") is None and "aiohttp" not in sys.modules:
        aio = MagicMock()
        sys.modules.setdefault("aiohttp", aio)
        sys.modules.setdefault("aiohttp.web", aio.web)


_ensure_feishu_mocks()

from gateway.approval_kinds import (  # noqa: E402
    ApprovalContext,
    ApprovalResult,
    clear_approval_kinds,
    get_approval_kind,
    list_approval_kinds,
    register_approval_kind,
)
from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms.feishu import FeishuAdapter  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_registry():
    clear_approval_kinds()
    yield
    clear_approval_kinds()


def _make_adapter() -> FeishuAdapter:
    adapter = FeishuAdapter(PlatformConfig(enabled=True))
    adapter._client = MagicMock()
    return adapter


def _card_action_data(action_value: dict, *, chat_id="oc_x", open_id="ou_x", token="tok_x"):
    return SimpleNamespace(
        event=SimpleNamespace(
            token=token,
            context=SimpleNamespace(open_chat_id=chat_id),
            operator=SimpleNamespace(open_id=open_id),
            action=SimpleNamespace(tag="button", value=action_value),
        ),
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestApprovalKindRegistry:
    def test_register_and_get(self):
        async def _on_action(_ctx):
            return ApprovalResult()

        register_approval_kind("dmc.adTag.del", lambda p: {"x": 1}, _on_action)
        kind = get_approval_kind("dmc.adTag.del")
        assert kind is not None
        assert kind.kind == "dmc.adTag.del"
        assert "dmc.adTag.del" in list_approval_kinds()

    def test_unknown_kind_returns_none(self):
        assert get_approval_kind("does.not.exist") is None

    def test_empty_kind_rejected(self):
        async def _on_action(_ctx):
            return ApprovalResult()
        with pytest.raises(ValueError):
            register_approval_kind("", lambda p: {}, _on_action)


# ---------------------------------------------------------------------------
# send_plugin_approval_card — render + dispatch via registry
# ---------------------------------------------------------------------------

class TestSendPluginApprovalCard:
    @pytest.mark.asyncio
    async def test_renders_and_injects_kind_tag(self):
        adapter = _make_adapter()

        def _render(payload: dict) -> dict:
            return {
                "header": {"title": {"content": f"Delete {payload['name']}", "tag": "plain_text"}},
                "elements": [
                    {"tag": "action", "actions": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": "Confirm"},
                         "value": {"choice": "yes"}},
                        {"tag": "button", "text": {"tag": "plain_text", "content": "Cancel"},
                         "value": {"choice": "no"}},
                    ]},
                ],
            }

        async def _on_action(_ctx):
            return ApprovalResult()

        register_approval_kind("dmc.adTag.del", _render, _on_action)

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_42"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_send:
            result = await adapter.send_plugin_approval_card(
                chat_id="oc_x",
                kind="dmc.adTag.del",
                payload={"name": "hermes-tt-001", "id": 80},
            )

        assert result.success
        assert result.message_id == "msg_42"

        kwargs = mock_send.call_args[1]
        assert kwargs["msg_type"] == "interactive"
        card = json.loads(kwargs["payload"])
        buttons = card["elements"][0]["actions"]
        for btn in buttons:
            assert btn["value"]["hermes_kind"] == "dmc.adTag.del"
            assert "hermes_action_id" in btn["value"]
        assert buttons[0]["value"]["choice"] == "yes"

        action_id = buttons[0]["value"]["hermes_action_id"]
        assert action_id in adapter._plugin_approval_state
        assert adapter._plugin_approval_state[action_id]["kind"] == "dmc.adTag.del"
        assert adapter._plugin_approval_state[action_id]["payload"]["id"] == 80

    @pytest.mark.asyncio
    async def test_unknown_kind_returns_failure(self):
        adapter = _make_adapter()
        result = await adapter.send_plugin_approval_card(
            chat_id="oc_x", kind="not.registered", payload={},
        )
        assert not result.success
        assert "unknown approval kind" in (result.error or "")


# ---------------------------------------------------------------------------
# Card-action routing — kind takes priority over hermes_action
# ---------------------------------------------------------------------------

class TestCardActionRouting:
    def test_kind_button_routed_to_plugin_handler(self):
        import asyncio
        import threading

        adapter = _make_adapter()
        action_id = "act_test_1"
        adapter._plugin_approval_state[action_id] = {
            "kind": "dmc.adTag.del",
            "payload": {"id": 80, "name": "hermes-tt-001"},
            "message_id": "msg_42",
            "chat_id": "oc_x",
        }

        done = threading.Event()
        captured = {}

        async def _on_action(ctx: ApprovalContext) -> ApprovalResult:
            captured["ctx"] = ctx
            done.set()
            return ApprovalResult(
                resolved_card={"header": {"title": {"content": "Done", "tag": "plain_text"}}},
                log_message="deleted tag 80",
            )

        register_approval_kind("dmc.adTag.del", lambda p: {}, _on_action)

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        try:
            adapter._loop = loop
            data = _card_action_data({
                "hermes_kind": "dmc.adTag.del",
                "hermes_action_id": action_id,
                "choice": "yes",
            })

            with patch("gateway.platforms.feishu.P2CardActionTriggerResponse",
                       new=lambda: SimpleNamespace(card=None)):
                with patch("gateway.platforms.feishu.CallBackCard",
                           new=lambda: SimpleNamespace(type=None, data=None)):
                    adapter._on_card_action_trigger(data)

            assert done.wait(timeout=3.0), "on_action handler was never invoked"
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2.0)
            loop.close()

        ctx = captured["ctx"]
        assert ctx.kind == "dmc.adTag.del"
        assert ctx.action_id == action_id
        assert ctx.payload["id"] == 80
        assert ctx.action_value["choice"] == "yes"
        assert action_id not in adapter._plugin_approval_state
