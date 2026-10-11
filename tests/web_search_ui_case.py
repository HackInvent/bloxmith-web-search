"""Shared UI case for F8.01 (managed package origin) and F8.02 (linked package origin).

FB6: real release UI in English and French, provider-dependent fields, responsive geometry,
drafts and persistence. Each origin runs in its own suite so that each has the runner's full
time budget on a busy host. Not a suite itself: the runner only executes F*.py files.
"""

from pathlib import Path
import time

from playwright.sync_api import sync_playwright, expect, TimeoutError as PlaywrightTimeoutError
from block_test_artifacts import artifact_path
from block_test_packages import install_test_package, surface_payload
from ui_smoke_common import isolated_server, graph_payload, create_project_api, project_editor_url
from blocs.web_search.block import WebSearchBlock

VIEWPORTS = ((1440, 900), (800, 700), (390, 740), (320, 568))
SECRET = "secret://workspace/brave_search"


class Surfaces:
    """Track the editor requests that replace a surface: block renders and graph patches.

    The modal and inspector are rendered by POST /api/blocks/<release>/modal|inspector-panel; an
    applied setting reaches the graph through POST /api/projects/<id>/graph/patch, after which the
    shell hydrates the graph and renders the inspector again. A render that lands late replaces the
    whole surface (drafts, status line, selected tab), so the test acts only once none is in flight.
    """

    ROUTES = ("modal", "inspector-panel")

    def __init__(self, page):
        self.page, self.requested, self.in_flight = page, dict.fromkeys(self.ROUTES, 0), 0
        page.on("request", self.started)
        page.on("requestfinished", self.ended)
        page.on("requestfailed", self.ended)

    def route(self, request):
        path = request.url.split("?", 1)[0]
        tail = path.rsplit("/", 1)[-1]
        if request.method == "POST" and "/api/blocks/" in path and tail in self.ROUTES:
            return tail
        if "/api/projects/" in path and (path.endswith("/graph/patch") or path.endswith("/graph/state")):
            return "graph"
        return None

    def started(self, request):
        route = self.route(request)
        if route:
            self.requested[route] = self.requested.get(route, 0) + 1
            self.in_flight += 1

    def ended(self, request):
        if self.route(request):
            self.in_flight -= 1

    def settle(self, quiet=0.5, timeout=60):
        """Wait until nothing tracked has been in flight for `quiet` seconds (mounts follow the answers)."""
        deadline, calm = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            if self.in_flight:
                calm = None
            elif calm is None:
                calm = time.monotonic()
            elif time.monotonic() - calm >= quiet:
                return
            self.page.wait_for_timeout(50)
        raise AssertionError(f"editor requests still pending: {self.in_flight}")


def show_tab(surfaces, card, inspector, tab):
    """Switch the inspector tab; the click sets the shell's tab and renders the inspector again.

    The shell ignores the click while it has no selected node (a late graph hydration on a busy
    host), so the node is selected again and the idempotent click repeated before failing.
    """
    panel = inspector.locator(f'[data-inspector-panel-tab="{tab}"]')
    for _ in range(3):
        surfaces.settle()
        inspector.locator(f'[data-inspector-tab="{tab}"]').click()
        try:
            expect(panel).to_be_visible(timeout=5000)
            return
        except AssertionError:
            print(f"[retry] inspector tab {tab} not shown; selecting the node again", flush=True)
            card.click(position={"x": 20, "y": 20})
    expect(panel).to_be_visible()


def open_modal(surfaces, card, modal):
    """Open the modal from the canvas card exactly once.

    The double-click is repeated only while the editor has not requested the modal yet (its handlers
    may not be bound): repeating it during a slow render opened the modal twice, and the late second
    render replaced the surface under the test.
    """
    page = surfaces.page
    expect(card.locator('h3')).to_be_visible(timeout=30000)
    if not modal.is_visible():
        before = surfaces.requested["modal"]
        for attempt in range(6):
            if surfaces.requested["modal"] > before:
                break
            if attempt:
                print("[retry] modal not requested yet; double-clicking the card again", flush=True)
            try:
                card.locator('h3').dblclick(timeout=5000)
            except PlaywrightTimeoutError:
                pass
            deadline = time.monotonic() + 5
            while surfaces.requested["modal"] == before and time.monotonic() < deadline:
                page.wait_for_timeout(50)
        assert surfaces.requested["modal"] > before, "the editor never requested the block modal"
    surfaces.settle()
    expect(modal).to_be_visible(timeout=60000)


def close_modal(modal, which="first"):
    """Close the modal and wait until it is gone, so a reopen binds to a fresh surface."""
    buttons = modal.locator('[data-close-block-modal]')
    (buttons.last if which == "last" else buttons.first).click()
    expect(modal).to_be_hidden()


def measure(modal):
    return modal.evaluate("""el => {
        const r=el.getBoundingClientRect(), body=el.querySelector('.owned-body');
        const actions=[...el.querySelectorAll('[data-close-block-modal], [data-owned-apply]')];
        const visible=[...el.querySelectorAll('input,select,textarea')].filter(c=>c.getClientRects().length);
        return {inside:r.left>=-1&&r.right<=innerWidth+1&&r.top>=-1&&r.bottom<=innerHeight+1,
          overflow:el.scrollWidth>el.clientWidth+2||body.scrollWidth>body.clientWidth+2,
          controls:actions.every(a=>{const b=a.getBoundingClientRect();return b.top>=0&&b.bottom<=innerHeight+1}),
          background:getComputedStyle(el).backgroundColor,
          unlabeled:visible.filter(c=>!c.labels?.length&&!c.getAttribute('aria-label')&&!c.getAttribute('aria-labelledby')).length};
    }""")


def geometry(modal):
    """Require an opaque panel with reachable actions, no page overflow and labelled controls."""
    deadline = time.monotonic() + 5
    while True:
        measured = measure(modal)
        if (measured["inside"] and measured["controls"] and not measured["overflow"]) or time.monotonic() > deadline:
            break
        time.sleep(0.1)
    assert measured["inside"] and measured["controls"] and not measured["overflow"], measured
    assert not measured["unlabeled"], measured
    assert measured["background"] not in {"transparent", "rgba(0, 0, 0, 0)"}, measured


def ui_action(page, click):
    """Run one Apply and return the JSON answer of its ui-action request."""
    with page.expect_response(lambda r: r.url.endswith('/ui-action') and r.request.method == 'POST') as answer:
        click()
    return answer.value.json()


def field(scope, key):
    return scope.locator(f'[data-block-config-field="{key}"]')


def run(origin):
    """Exercise the current package files in a real browser host for one package origin."""
    assert origin in ("managed", "linked"), origin
    block = WebSearchBlock()
    # Managed hosts render surfaces in spawned supervised processes: allow for a busy test host.
    expect.set_options(timeout=15000)
    shots = []

    def shot(page, path):
        page.screenshot(path=path)
        shots.append(Path(path))

    with isolated_server() as server, sync_playwright() as playwright:
        model = install_test_package(server, "web_search", origin=origin)
        candidate = block.build_node_payload(node_id="ui-case", position={"x": 160, "y": 160})
        candidate["block_version"] = model["version"]
        for surface in ("modal", "inspector_panel", "node_card"):
            html = surface_payload(server, model, candidate, surface=surface)["html"]
            assert "{{" not in html
        project = create_project_api(server, document=graph_payload("Owned form", [candidate], []))["project"]
        url = project_editor_url(server.base_url, project["project_id"], workspace_project_id=project["workspace_project_id"])
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.add_init_script("window.localStorage.setItem('bloxsmith.inspectorPinned','true')")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            surfaces = Surfaces(page)
            page.goto(url)
            card = page.locator('.canvas-node[data-node-id="ui-case"]')
            expect(card.locator('h3')).to_be_visible()
            expect(card.locator('.node-data-preview')).to_have_text("Remote access off")
            modal = page.locator('.owned-modal')
            open_modal(surfaces, card, modal)
            apply = modal.locator('[data-owned-apply]')
            expect(apply).to_be_disabled()
            provider, secret, count = field(modal, "provider"), field(modal, "secret_ref"), field(modal, "count")
            expect(provider).to_have_value("wikipedia")
            expect(secret).to_be_hidden()
            expect(field(modal, "searxng_url")).to_be_hidden()
            expect(field(modal, "time_range")).to_be_hidden()
            expect(field(modal, "language")).to_be_visible()
            # A draft is discarded by Cancel.
            field(modal, "language").fill("fr")
            expect(apply).to_be_enabled()
            close_modal(modal, "last")
            open_modal(surfaces, card, modal)
            expect(field(modal, "language")).to_have_value("")
            expect(apply).to_be_disabled()
            # Choosing a keyed provider reveals the wallet field; enabling without it is refused on Apply.
            provider.select_option("brave")
            expect(secret).to_be_visible()
            expect(field(modal, "time_range")).to_be_visible()
            field(modal, "enabled").check()
            refused = ui_action(page, apply.click)
            assert "secret_ref" in refused.get("error", ""), refused
            expect(modal.locator('.owned-form-status')).to_contain_text("secret_ref")
            secret.fill(SECRET)
            field(modal, "region").fill("France")
            refused = ui_action(page, apply.click)
            assert "region" in refused.get("error", ""), refused
            field(modal, "region").fill("fr")
            count.fill("0")
            apply.click()
            expect(modal.locator('.owned-form-status')).to_have_text("Check the numeric value and its limits.")
            count.fill("5")
            field(modal, "time_range").select_option("week")
            saved = ui_action(page, apply.click)
            assert not saved.get("error"), saved
            if modal.is_visible():
                close_modal(modal)
            surfaces.settle()  # the graph patch of the Apply must land before the reload
            page.reload()
            expect(card.locator('.node-data-preview')).to_have_text("Enabled · Brave Search")
            open_modal(surfaces, card, modal)
            expect(provider).to_have_value("brave")
            expect(secret).to_have_value(SECRET)
            expect(count).to_have_value("5")
            expect(field(modal, "region")).to_have_value("FR")
            expect(field(modal, "time_range")).to_have_value("week")
            expect(field(modal, "enabled")).to_be_checked()
            for width, height in VIEWPORTS:
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_timeout(100)
                geometry(modal)
                shot(page, artifact_path("web-search-" + origin + "-" + str(width) + ".png"))
            summary = modal.locator('.owned-advanced').first.locator('summary')
            summary.scroll_into_view_if_needed()
            summary.focus()
            summary.press('Enter')
            expect(modal.locator('.owned-advanced').first).to_have_attribute('open', '')
            expect(field(modal, "timeout_sec")).to_be_visible()
            expect(field(modal, "api_base_url")).to_have_value("")
            expect(field(modal, "api_base_url")).to_be_visible()
            expect(field(modal, "max_pages")).to_be_hidden()
            geometry(modal)
            shot(page, artifact_path("web-search-" + origin + "-advanced-320.png"))
            page.set_viewport_size({"width": 1440, "height": 900})
            # SearXNG over plain HTTP needs its explicit opt-in; the refused draft is then discarded.
            provider.select_option("searxng")
            expect(secret).to_be_hidden()
            expect(field(modal, "max_pages")).to_be_visible()
            field(modal, "searxng_url").fill("http://search.example.org")
            refused = ui_action(page, apply.click)
            assert "allow_insecure_http" in refused.get("error", ""), refused
            close_modal(modal, "last")
            card.click(position={"x": 20, "y": 20})
            inspector = page.locator('[data-properties-surface="inspector"][data-node-id="ui-case"]:visible')
            expect(inspector).to_be_visible()
            surfaces.settle()
            assert inspector.evaluate('el => el.scrollWidth <= el.clientWidth+2')
            expect(field(inspector, "enabled")).to_be_checked()
            # Switching to SearXNG with its plain-HTTP opt-in; the URL is saved without its trailing slash.
            field(inspector, "provider").select_option("searxng")
            expect(field(inspector, "secret_ref")).to_be_hidden()
            expect(field(inspector, "searxng_url")).to_be_visible()
            field(inspector, "searxng_url").fill("http://search.example.org/")
            field(inspector, "allow_insecure_http").check()
            panel_apply = inspector.locator('[data-owned-apply]')
            expect(panel_apply).to_be_enabled()
            applied = ui_action(page, panel_apply.click)
            assert not applied.get("error"), applied
            expect(card.locator('.node-data-preview')).to_have_text("Enabled · SearXNG")
            show_tab(surfaces, card, inspector, "ports")
            for port in ("query", "results", "error"):
                expect(inspector.locator('[data-inspector-panel-tab="ports"]')).to_contain_text(port)
            show_tab(surfaces, card, inspector, "general")
            shot(page, artifact_path("web-search-" + origin + "-inspector.png"))
            page.goto(server.base_url + "/")
            page.locator("#homeApplicationSettingsButton").click()
            page.locator("#applicationLanguageSelect").select_option("fr")
            page.wait_for_function("window.CWMessages.getLanguage() === 'fr'")
            page.goto(url)
            # The card preview follows the interface language; it also proves the French editor is ready.
            expect(card.locator('.node-data-preview')).to_have_text("Activé · SearXNG")
            open_modal(surfaces, card, modal)
            expect(field(modal, "searxng_url")).to_have_value("http://search.example.org")
            expect(field(modal, "allow_insecure_http")).to_be_checked()
            expect(modal.locator('.owned-advanced').first.locator('summary')).to_have_text("Réglages avancés")
            expect(modal.locator('.owned-notice')).to_contain_text("Chaque requête part uniquement vers le fournisseur choisi")
            expect(modal.locator('label[for$="-enabled"]')).to_have_text("Activer l’accès distant")
            expect(modal.locator('label[for$="-allow_private_network"]')).to_have_text("Autoriser le réseau privé")
            expect(field(modal, "provider").locator('option[value="searxng"]')).to_have_text("SearXNG (auto-hébergé)")
            expect(field(modal, "time_range").locator('option[value=""]')).to_have_text("Toute période")
            expect(modal.locator('[data-owned-apply]')).to_have_text("Appliquer")
            for width, height in ((1440, 900), (390, 740)):
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_timeout(100)
                geometry(modal)
                shot(page, artifact_path("web-search-" + origin + "-fr-" + str(width) + ".png"))
            assert not errors, errors
            assert page.evaluate("!window.CWBlockUiBlocks?.web_search")
        finally:
            browser.close()
    print("[ok] web_search " + origin + " UI", flush=True)
    return shots
