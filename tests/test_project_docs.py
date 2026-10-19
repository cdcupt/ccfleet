"""Current local-Claude guidance never inherits retired slot-only/privacy claims."""

from pathlib import Path

from ccfleetd import bilingual
from ccfleetd.config import Config
from ccfleetd.customer_docs import guide, how_it_works, overview
from ccfleetd.usersite import cli_pairing_page, privacy_page
from tests.test_html_document_preservation import read_document, visible_html

ROOT = Path(__file__).parents[1]
SETUP_COMMAND = ("curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/"
                 "laptop/install.sh | bash -s -- --setup")


def visible(page):
    return visible_html(bilingual.project_html(page, "en"))


def test_guide_keeps_one_setup_command_and_reuses_existing_pairing():
    page = guide(Config())
    text = visible(page)
    assert 'id="setup"' in page and page.count(SETUP_COMMAND) == 1
    for phrase in (
        "keeps an existing pairing",
        "Only if asked",
        "fresh pairing code",
        "ccfleet local --check",
        "ccfleet local",
        "make no model request",
        "upload no project files",
    ):
        assert phrase in text
    assert "ccfleet-connect" not in text and "--migrate" not in text


def test_setup_installs_missing_native_claude_and_preserves_existing_configuration():
    text = visible(guide(Config()))
    for phrase in (
        "local Claude installation",
        "installs the original local CLI",
        "fixed vendor URL",
        "digest-pinned helper",
        "preserves pairing",
        "configuration and slot sign-in",
        "backup when it adjusts PATH",
    ):
        assert phrase in text
    assert "saves a separate private connection key" in text


def test_current_setup_does_not_teach_retired_cleanup_workflows():
    text = visible(guide(Config()))
    for removed in (
        "live-folder",
        "migration",
        "--yes",
        "--migrate",
        "--reset-link",
        "--disconnect",
        "ccfleet-connect",
    ):
        assert removed not in text
    assert "Setup and the readiness check make no model request" in text


def test_primary_workflow_is_native_local_without_mount_or_filesystem_product_caps():
    text = visible(guide(Config()))
    for phrase in (
        "launches the original local Claude CLI",
        "does not upload a project",
        "mount your filesystem on the slot",
        "no CC Fleet filesystem count/size caps",
        "working directory is not a sandbox",
        "including outside that directory",
    ):
        assert phrase in text
    assert "No local Claude Code installation is required" not in text
    assert "Tools execute on Linux" not in text


def test_native_session_controls_and_local_permission_defaults_are_documented():
    text = visible(guide(Config()))
    for command in (
        "ccfleet local --new --name work",
        "ccfleet local --resume",
        "ccfleet local --resume work",
        "ccfleet local --continue",
        "ccfleet local --resume work --fork-session",
        'ccfleet local --print "Summarize this project"',
        "--mode plan --model opus --effort high",
    ):
        assert command in text
    assert "default to bypassPermissions" in text
    assert "without individual approval prompts" in text
    assert "/model" in text and "/effort" in text
    assert "Resuming keeps the saved model and effort unless changed deliberately" in text


def test_native_history_is_private_and_no_old_profiles_are_taught():
    text = visible(guide(Config()))
    for phrase in (
        "Native Claude settings/history",
        "existing CLAUDE_CONFIG_DIR",
        "stay on this computer",
        "does not import or synchronize conversations",
        "temporary private settings overlay",
        "not permanently rewritten",
    ):
        assert phrase in text
    assert "--legacy-history" not in text and "Old remote history" not in text


def test_current_exit_and_failover_do_not_imply_replay_or_retired_controls():
    text = visible(guide(Config()))
    assert "Quit with /exit and resume later" in text
    assert "result is uncertain, the gateway stops" in text
    for removed in ("--disconnect", "--reset-link", "ccfleet project", "ccfleet remote"):
        assert removed not in text


def test_current_background_jobs_are_local_and_require_a_running_computer():
    text = visible(guide(Config()))
    for phrase in (
        "local Claude print-mode",
        "computer must stay running and connected",
        "job ID",
        "private local files",
        "never automatically restart",
        "not sandbox-contained",
        "archiving never stops an active job",
    ):
        assert phrase in text
    assert "bridge" not in text


def test_privacy_distinguishes_local_files_from_model_context_and_direct_network():
    for page in (guide(Config()), privacy_page(Config()), how_it_works(Config())):
        text = visible(page)
        for phrase in ("MCP", "hooks", "plugins", "gateway", "file contents"):
            assert phrase in text
        assert "directly from the laptop" in text or "directly from your computer" in text
        assert "root" in text and "Claude credential" in text
    text = visible(how_it_works(Config()))
    for phrase in (
        "selected headers",
        "structured metadata",
        "does not redact arbitrary prompts",
        "Native system prompts",
        "working directory",
        "not anonymity",
        "The gateway can read model requests and responses",
    ):
        assert phrase in text


def test_privacy_page_preserves_history_and_discloses_current_model_context():
    text = visible(privacy_page(Config()))
    for phrase in (
        "Your local work and model context",
        "Native local history/settings",
        "does not automatically erase existing files or saved history",
        "does not undo writes or erase",
        "connection IP and timing",
        "This is not anonymity",
        "operator console",
        "The gateway can read model requests and responses",
    ):
        assert phrase in text
    for removed in ("--transport bridge", "--legacy-history", "live-folder", "migration"):
        assert removed not in text


def test_overview_and_how_it_works_describe_local_execution_without_slot_only_overclaim():
    summary = visible(overview(Config()))
    assert "Local Claude Code, your own account on your slot" in summary
    assert "Your tools and saved files stay local" in summary
    assert "native history" in summary
    details = visible(how_it_works(Config()))
    assert "Original Claude Code runs on your computer" in details
    assert "dedicated VPS and dedicated exit IP" in details
    assert "Slots may share a physical machine and its internet address" not in details
    assert "only Claude Code on the slot talks to Anthropic" not in details
    assert "remote-terminal" not in details


def test_pairing_page_shares_current_setup_without_embedding_code_in_command():
    token = "ccf_pair_FAKE_DISPLAY_ONLY"
    page = cli_pairing_page({"id": "test-slot", "name": "Example slot"}, token)
    text = visible(page)
    assert SETUP_COMMAND in page
    assert "Enter this code only if asked" in page
    assert "Setup keeps your existing connection" in page
    assert page.count(token) == 1 and f'class="token">{token}</pre>' in page
    for phrase in (
        "setup installs the original local CLI",
        "fixed vendor URL",
        "No project is uploaded during setup",
        "ccfleet gateway setup",
        "model requests travel over HTTPS to Fleet",
        "Your saved history and files stay local",
        "Fleet’s gateway can read those requests and replies",
        "does not automatically erase",
    ):
        assert phrase in text
    assert "--mode manual" in text
    for removed in ("--legacy-history", "--transport bridge", "live-folder", "ccfleet-connect"):
        assert removed not in text


def test_readme_and_current_relay_guide_share_setup_and_native_semantics():
    for relative in ("README.html", "docs/local-relay.html"):
        text = read_document(ROOT / relative).text
        assert SETUP_COMMAND in text
        for phrase in ("--legacy-history", "--fork-session", "--print", "--continue",
                       "--disconnect", "--reset-link", "MCP", "slot", "local"):
            assert phrase in text
        assert "no longer used anywhere else" in text
        assert "fingerprint-free" in text


def test_old_workspace_guide_is_tombstone_and_keeps_legacy_recovery_scoped():
    page = read_document(ROOT / "docs/project-workspaces.html")
    text = page.text
    assert any(tag == "h1" and heading.startswith("Retired remote workspaces")
               for tag, heading in page.headings)
    assert "local Claude setup and migration" in text
    assert "local-relay.html" in page.links
    assert "primary workflow" in text
    assert "1,000 files, 4 MiB per file and 20 MiB total" in text
    assert "apply only to those legacy transfer commands" in text
    assert "ordinary remote tmux" in text.lower()
    assert "Do not stop a real user's old session" in text


def test_current_guide_scopes_verification_and_interrupted_migration_recovery():
    text = read_document(ROOT / "docs/local-relay.html").text
    for phrase in ("EOFError: filesystem connection ended", "shutdown lock",
                   "Do not delete configuration, remove keys, or re-pair",
                   "marked retired", "ordinary shell prompt", "does not automatically",
                   "mktemp -d /tmp/ccfleet-check.XXXXXX", "outside Claude",
                   "mounted/shared filesystem could also pass", "does not prove where every",
                   "Neither this test nor a readiness check proves zero metadata"):
        assert phrase in text


def test_historical_verification_and_remote_design_point_to_current_architecture():
    for relative in ("docs/live-folders-verification.html",
                     "docs/project-workspaces-verification.html"):
        page = read_document(ROOT / relative)
        assert "local-relay-verification.html" in page.links
        assert "retired" in page.text.lower()
    design = read_document(ROOT / "docs/design.html").text
    assert "remote-terminal compatibility design" in design
    assert "not local Claude" in design
    assert "default hosted-terminal path" not in design


def test_compliance_keeps_historical_provenance_and_does_not_claim_relay_authorization():
    page = read_document(ROOT / "docs/compliance.html")
    text = page.text
    assert "Last re-verified against Anthropic's published documentation: 2026-09-28" in text
    assert "https://code.claude.com/docs/en/legal-and-compliance" in page.links
    assert "not covered by the historical hosted terminal mapping" in text
    assert "claims no approval for the relay" in text
    assert "source record below was not re-fetched" in text
    assert "use the unmodified Claude Code binary" in text
    assert "do not collect, store or intermediate Claude.ai credentials" in text
    assert "Do not promise anonymity" in text
