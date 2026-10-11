"""FB2/FB3/FB5: real graphs in One Shot and Active Runtime for bundled, managed and linked packages, with Stop.

Every provider exchange targets the loopback doubles in search_fixture.py. The
wallet and its fixture key exist only in disposable test storage. Split from
F5.02 so that each suite has the runner's full time budget on a busy host.
"""

import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).resolve().parent)]
from blocs.web_search.block import WebSearchBlock
from block_test_packages import install_test_package, prepare_release_run, surface_payload
from search_fixture import REF, TOKEN, search_api, settings
from ui_smoke_common import (create_project_api, create_run_api, data_edge, display_node, graph_payload, http_json,
                             isolated_server, stop_run_api, text_node, wait_for_run_predicate, wait_for_run_terminal)


def calls(api, route):
    return [call for call in api.calls if call["route"] == route]


def value(data, port):
    raw = data.get("output_values", {}).get("search:" + str(port), {}).get("value")
    return json.loads(raw) if raw else {}


def wallet(server):
    http_json(server.base_url, "/api/application/secrets/init", method="POST", payload={"password": "disposable-search-fixture"})
    http_json(server.base_url, "/api/application/secrets", method="POST", payload={"name": "search_fixture", "value": TOKEN})


def graph_runs():
    """# - FB2 - # - FB3 - # - FB5 - real graphs in both runtimes for bundled, managed and linked packages, with Stop."""
    block = WebSearchBlock()
    for mode in ("centralized", "zeromq_active"):
        for origin in (None, "managed", "linked"):
            full = origin is None
            with search_api() as api, isolated_server() as server:
                node = block.build_node_payload(node_id="search", position={"x": 380, "y": 160},
                                                config_overrides=settings(api, "brave", count=3))
                wiki = block.build_node_payload(node_id="search", position={"x": 380, "y": 160},
                                                config_overrides=settings(api, "wikipedia"))
                if origin:
                    model = install_test_package(server, "web_search", origin=origin)
                    node["block_version"] = wiki["block_version"] = model["version"]
                    for surface in ("modal", "inspector_panel", "node_card"):
                        html = surface_payload(server, model, node, surface=surface)["html"]
                        assert "{{" not in html and TOKEN not in html
                        if surface == "node_card":
                            assert REF not in html and "Brave Search" in html
                        else:
                            assert 'data-block-config-field="searxng_url"' in html
                wallet(server)
                sinks = [display_node("found", "Results", 760, 80), display_node("problem", "Problem", 760, 300)]
                edges = [data_edge("rx", "search", 1, "found", 1), data_edge("err", "search", 2, "problem", 1)]
                if mode == "centralized":
                    def run(document, target=node):
                        text = document if isinstance(document, str) else json.dumps(document)
                        graph = graph_payload("Search one shot", [text_node("source", "Source", text, 60, 160), target, *sinks],
                                              [data_edge("in", "source", 1, "search", 1), *edges])
                        project = create_project_api(server, document=graph)["project"]
                        created = create_run_api(server, graph, project_id=project["graph_id"], runtime_mode=mode)
                        data = wait_for_run_terminal(server, created["run_id"], timeout_sec=40)
                        assert data["status"] == "success", (origin, data.get("logs"))
                        assert TOKEN not in json.dumps(data)
                        return data
                    found = value(run("ada lovelace"), 1)
                    assert found["count"] == 3 and found["provider"] == "brave" and found["results"][0]["rank"] == 1
                    assert calls(api, "brave")[-1]["brave_token"] == TOKEN
                    if full:
                        summary = value(run({"operation": "wikipedia_summary", "title": "Lovelace"}, wiki), 1)
                        assert summary["title"] == "Ada Lovelace" and summary["redirected_from"] == "Lovelace"
                        before = len(api.calls)
                        refused = value(run({"query": "ada", "count": 50}), 2)
                        assert refused["code"] == "invalid_input" and len(api.calls) == before
                        denied = value(run({"query": "ada", "time_range": "week"}, wiki), 2)
                        assert denied["code"] == "invalid_input" and denied["provider"] == "wikipedia"
                        # HTTP 404 from a wrong SearXNG path prefix reaches the error port as not_found.
                        wrong = block.build_node_payload(node_id="search", position={"x": 380, "y": 160}, config_overrides=settings(
                            api, "searxng", searxng_url=api.origin + "/wrong-prefix"))
                        before = len(api.calls)
                        missing = value(run({"query": "ada", "request_id": "w-404"}, wrong), 2)
                        assert missing["code"] == "not_found" and missing["http_status"] == 404 and missing["request_id"] == "w-404"
                        assert len(api.calls) == before + 1 and api.calls[-1]["route"] == "unknown"
                        # Stop while Brave holds a request: the run ends promptly and publishes no results.
                        api.script["brave"] = ["hold"]
                        api.release.clear()
                        graph = graph_payload("Search stop", [text_node("source", "Source", "held", 60, 160), node, *sinks],
                                              [data_edge("in", "source", 1, "search", 1), *edges])
                        project = create_project_api(server, document=graph)["project"]
                        held = len(calls(api, "brave"))
                        rid = create_run_api(server, graph, project_id=project["graph_id"], runtime_mode=mode)["run_id"]
                        deadline = time.monotonic() + 20
                        while len(calls(api, "brave")) == held and time.monotonic() < deadline:
                            time.sleep(0.05)
                        assert len(calls(api, "brave")) == held + 1, "held search not reached"
                        started = time.monotonic()
                        stop_run_api(server, rid)
                        stopped = wait_for_run_terminal(server, rid, timeout_sec=20)
                        assert time.monotonic() - started < 10, stopped.get("logs")
                        assert stopped["status"] == "cancelled", (stopped["status"], value(stopped, 2), stopped.get("logs"))
                        assert not value(stopped, 1), "a stopped search publishes no results"
                        assert len(calls(api, "brave")) == held + 1, "the held search is not retried"
                        assert "shutdown_timeout" not in json.dumps(stopped.get("logs"))
                        api.release.set()
                        http_json(server.base_url, "/api/application/secrets/lock", method="POST", payload={})
                        before = len(api.calls)
                        locked = value(run("ada"), 2)
                        assert locked["code"] == "secret" and len(api.calls) == before
                else:
                    graph = graph_payload("Search worker", [text_node("cmd", "Command", "", 60, 160), node, *sinks],
                                          [data_edge("in", "cmd", 1, "search", 1), *edges])
                    project = create_project_api(server, document=graph)["project"]

                    def start():
                        rid = prepare_release_run(server, project["graph_id"], graph)["run_id"]
                        http_json(server.base_url, f"/api/runs/{rid}/play", method="POST", payload={})
                        return rid

                    def send(rid, document):
                        http_json(server.base_url, f"/api/runs/{rid}/active/control", method="POST", payload={
                            "action": "publish_output", "node_id": "cmd", "port_id": 1, "value": json.dumps(document),
                            "content_type": "application/json"})

                    def wait(rid, predicate, label):
                        data = wait_for_run_predicate(server, rid, lambda item: item.get("status") == "failed" or predicate(item),
                                                      label, timeout_sec=30)
                        assert data.get("status") != "failed", data.get("logs")
                        assert TOKEN not in json.dumps(data)
                        return data

                    def stop(rid):
                        started = time.monotonic()
                        stop_run_api(server, rid)
                        stopped = wait_for_run_terminal(server, rid, timeout_sec=20)
                        assert time.monotonic() - started < 10, stopped.get("logs")
                        assert "shutdown_timeout" not in json.dumps(stopped.get("logs"))
                        return stopped

                    rid = start()
                    try:
                        assert not api.calls, "Preparation or Play contacted a provider without a query"
                        send(rid, {"query": "first", "request_id": "active-1"})
                        found = value(wait(rid, lambda item: value(item, 1).get("request_id") == "active-1", "results missing"), 1)
                        assert found["count"] == 3 and len(calls(api, "brave")) == 1
                        send(rid, {"query": "second", "page": 2, "request_id": "active-2"})
                        found = value(wait(rid, lambda item: value(item, 1).get("request_id") == "active-2", "page missing"), 1)
                        assert [item["rank"] for item in found["results"]] == [4, 5, 6] and len(calls(api, "brave")) == 2
                        if full:
                            send(rid, {"query": "x", "count": 0, "request_id": "bad"})
                            wait(rid, lambda item: value(item, 2).get("request_id") == "bad", "invalid query not reported")
                            http_json(server.base_url, "/api/application/secrets/lock", method="POST", payload={})
                            before = len(api.calls)
                            send(rid, {"query": "locked", "request_id": "locked"})
                            wait(rid, lambda item: value(item, 2).get("code") == "secret", "locked wallet not reported")
                            assert len(api.calls) == before
                        else:
                            # Stop while a provider request is pending: the run ends promptly and nothing is published late.
                            api.script["brave"] = ["hold"]
                            api.release.clear()
                            before = len(calls(api, "brave"))
                            send(rid, {"query": "held", "request_id": "held"})
                            deadline = time.monotonic() + 15
                            while len(calls(api, "brave")) == before and time.monotonic() < deadline:
                                time.sleep(0.05)
                            assert len(calls(api, "brave")) == before + 1, "the held search never reached the double"
                    finally:
                        stopped = stop(rid)
                        api.release.set()
                    assert value(stopped, 1).get("request_id") != "held"
                assert TOKEN not in "\n".join(server.logs)
            print("[ok] Web Search graph " + mode + " " + str(origin), flush=True)


def main():
    graph_runs()


if __name__ == "__main__":
    main()
