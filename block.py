"""Web search through SearXNG, Brave Search, SerpAPI (Google) or Wikipedia, with normalized ranked results."""

import json
from bloxsmith_app.block_api import APPLICATION_JSON, BlockDefinition, BlockRuntimeOutput, BlockRuntimePreparation, BlockRuntimeResult
from . import logic, transport, ui


# FB1 - Keep remote access off by default; one provider per node; API keys only from the wallet; official API origins fixed (loopback only for tests); SearXNG plain HTTP and private networks only by explicit opt-in.
# FB2 - Search from a plain-text or JSON query with count 1-20, page, language, region, safe search and time range mapped to each provider's documented parameters, and publish normalized ranked results.
# FB3 - Summarize one Wikipedia article from a title, an article link or search terms through the MediaWiki Action API (intro extract, URL, redirects, disambiguation), never fetching pages.
# FB4 - Bound query, response, time, SearXNG pages and retries; honour Retry-After on HTTP 429; report coded English errors (auth, 404, 429, quota, disabled JSON format) without the key.
# FB5 - Apply one contract in both runtimes and every package origin; one fresh query triggers exactly one search and Stop cancels a pending call.
# FB6 - Own translated responsive settings with provider-dependent fields, draft Apply and release-scoped assets.
class WebSearchBlock(BlockDefinition):
    """Search results are untrusted data, never instructions for this block."""

    kind = "web_search"

    def ui_assets(self, surface="modal"):
        """Every installation uses the same static release manifest."""
        return list(self.model.get("ui_assets", {}).get(surface, []))

    def prepare_runtime(self, context):
        """Validate settings without wallet, DNS, network or storage access; no listener is ever declared."""
        logic.configuration(context.config)
        return BlockRuntimePreparation()

    def fresh_inputs(self, context):
        """Only fresh deliveries count: a retained query is never searched again."""
        names = {int(port.id): str(port.name) for port in context.input_ports}
        if context.input_events:
            return [(names.get(event.input_port_id), event.value) for event in context.input_events]
        fresh = []
        for ident, name in names.items():
            attribute = context.input_attribute(name, str(ident))
            if attribute is not None and attribute.status == "updated" and attribute.value not in (None, ""):
                fresh.append((name, attribute.value))
        return fresh

    def execute_runtime(self, context):
        """One search per activation; business failures go to the error output and the run continues."""
        request, token, config = {}, "", {}
        try:
            config = logic.configuration(context.config)
            items = self.fresh_inputs(context)
            if not items:
                return BlockRuntimeResult(status="skipped", outputs=[])
            if len(items) != 1 or items[0][0] != "query":
                logic.fail("invalid_input", "Send one query per activation; nothing was sent.")
            if not config["enabled"]:
                logic.fail("disabled")
            request = logic.parse_request(items[0][1], config)
            if config["provider"] in logic.KEYED:
                try:
                    raw = context.resolve_secret(config["secret_ref"])
                except Exception:
                    logic.fail("secret")
                token = logic.api_token(raw)
            result = transport.run(config, token, request, context.services.get("cancel_requested", lambda: False))
            operation, provider = request["operation"], config["provider"]
            text = context.redact_secrets(logic.redact(json.dumps(result, ensure_ascii=False, allow_nan=False), token))
            return BlockRuntimeResult(status="success",
                outputs=[BlockRuntimeOutput(port_id=int(port.id), port_name=port.name, value=text,
                                            content_type=APPLICATION_JSON)
                         for port in context.output_ports if port.name == "results"],
                content_type=APPLICATION_JSON, last_message=f"{result['count']} result(s)",
                metadata={self.kind: {"status": "success", "operation": operation, "provider": provider,
                                      "count": result["count"], "attempts": result["attempts"]}},
                logs=[f"[web-search] {provider} {operation} returned {result['count']} result(s) "
                      f"after {result['attempts']} attempt(s)."])
        except logic.SearchError as exc:
            error = exc
        except (ValueError, TypeError, OSError, RuntimeError, KeyError):
            # Raw exception text could carry paths, URLs or provider data: publish a fixed diagnostic.
            error = logic.SearchError("internal")
        operation = request.get("operation") or error.context.get("operation")
        provider = request.get("provider") or config.get("provider")
        envelope = error.envelope(operation=operation, provider=provider,
                                  request_id=request.get("request_id") or error.context.get("request_id"))
        text = context.redact_secrets(logic.redact(json.dumps(envelope, ensure_ascii=False, allow_nan=False), token))
        return BlockRuntimeResult(status="success",
            outputs=[BlockRuntimeOutput(port_id=int(port.id), port_name=port.name, value=text,
                                        content_type=APPLICATION_JSON)
                     for port in context.output_ports if port.name == "error"],
            content_type=APPLICATION_JSON, last_message=error.code,
            metadata={self.kind: {"status": "error", "operation": operation, "provider": provider, "code": error.code}},
            logs=[f"[web-search-error] {provider or 'settings'} {operation or 'input'}: {error.code}"])

    def handle_ui_action(self, *, node, action, values, payload=None):
        """Apply validates settings only; it never resolves a host, contacts a provider or reads the wallet.

        Accepted text settings are saved in their canonical form (region FR, language pt-br, URLs without
        a trailing slash), so the form shows what the runtime will use.
        """
        if action in {"save_settings", "modal_update_fields", "inspector_update_fields"}:
            patch = (values or {}).get("node_patch") or {}
            if isinstance(patch.get("config"), dict):
                try:
                    canonical = logic.configuration({**self.default_config(), **(node.get("config") or {}), **patch["config"]})
                except (ValueError, TypeError) as exc:
                    detail = str(exc) if isinstance(exc, logic.SearchError) else logic.MESSAGES["config"]
                    return {"error": self.translate("block.web_search.error", {"detail": detail},
                                                    fallback="Web Search settings rejected: " + detail)}
                config = {key: canonical[key] if key in logic.DEFAULTS else item for key, item in patch["config"].items()}
                values = {**values, "node_patch": {**patch, "config": config}}
        result = super().handle_ui_action(node=node, action="modal_update_fields" if action == "save_settings" else action,
                                          values=values, payload=payload)
        return ui.replace_config(result, node)

    def render_modal(self, *, node, payload=None):
        return ui.modal(self, node, payload)

    def render_inspector_panel(self, *, node, payload=None):
        return ui.inspector(self, node, payload)

    def render_node_card(self, *, node, payload=None):
        """Show the access mode and provider only, never the key reference or the instance URL."""
        config = {**self.default_config(), **(node.get("config") or {})}
        provider = config.get("provider") if config.get("provider") in logic.PROVIDERS else "wikipedia"
        # The key travels with the text so the editor re-renders it in the interface language,
        # even when a package process rendered it with the English pivot.
        if config.get("enabled") is not True:
            key, fallback = "block.web_search.card_disabled", "Remote access off"
        else:
            key, fallback = f"block.web_search.card_{provider}", ui.CARD_FALLBACKS[provider]
        return ui.card(self, node, key, self.translate(key, fallback=fallback))
