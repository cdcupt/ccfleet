"""Current local-Claude guidance never inherits retired slot-only/privacy claims."""

import re
from html import unescape
from pathlib import Path

from ccfleetd.config import Config
from ccfleetd.customer_docs import guide, how_it_works, overview
from ccfleetd.usersite import cli_pairing_page, privacy_page

ROOT = Path(__file__).parents[1]
SETUP_COMMAND = ("curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/"
                 "laptop/install.sh | bash -s -- --setup")


def visible(page):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", page)).split())


def test_guide_keeps_one_setup_command_and_reuses_existing_pairing():
    page = guide(Config())
    migration = page.split('id="migration"', 1)[1].split('id="project-workspaces"', 1)[0]
    text = visible(migration)
    assert 'href="#migration"' in page
    assert migration.count(SETUP_COMMAND) == 1
    for phrase in ("One-command setup and migration", "reuses its existing pairing",
                   "Only if this computer is not paired", "one fresh pairing code",
                   "No SSH command or slot Claude credential is needed",
                   "without uploading project files or making a model request"):
        assert phrase in text
    assert "laptop/install.sh | bash -s -- --migrate" not in migration


def test_setup_installs_missing_native_claude_and_preserves_existing_configuration():
    text = visible(guide(Config()))
    for phrase in ("Existing native Claude is preserved", "installs the original local CLI",
                   "fixed vendor URL", "verifies its digest-pinned helper",
                   "Pairing, configuration, slot sign-in and existing remote files stay in place",
                   "PATH changes preserve existing shell settings", "keep a backup",
                   "does not automatically revoke an Anthropic credential"):
        assert phrase in text
    assert '--name "Personal Mac"' in text and "--slot SLOT" in text


def test_migration_requires_explicit_end_user_cancellation_and_preserves_other_work():
    text = visible(guide(Config()))
    for phrase in ("asks in the controlling terminal before stopping old live-folder connectors",
                   "associated remote live-folder sessions", "cancels pending work",
                   "invalidates open mount handles", "Files and history are kept",
                   "ordinary remote tmux sessions are untouched", "If you decline or cleanup fails",
                   "Adding --yes explicitly authorizes this cancellation"):
        assert phrase in text
    assert "old live-folder connector may still be running after its terminal closes" in text


def test_primary_workflow_is_native_local_without_mount_or_filesystem_product_caps():
    text = visible(guide(Config()))
    for phrase in ("Local Claude, local files and history", "launches the original local Claude CLI",
                   "does not upload a project, mount your filesystem on the slot",
                   "no CC Fleet filesystem count/size caps or Git-ignore filters",
                   "working directory is not a sandbox", "including outside that directory"):
        assert phrase in text
    assert "cd ~" in text
    assert "No local Claude Code installation is required" not in text
    assert "Tools execute on Linux" not in text


def test_native_session_controls_and_local_permission_defaults_are_documented():
    text = visible(guide(Config()))
    for command in ("ccfleet local --new --name work", "ccfleet local --resume",
                    "ccfleet local --resume work", "ccfleet local --continue",
                    "ccfleet local --resume work --fork-session",
                    'ccfleet local --print "Summarize this project"',
                    "--mode plan --model opus --effort high"):
        assert command in text
    assert "default to bypassPermissions" in text
    assert "without individual approval prompts" in text
    assert "native local conversations, not remote tmux sessions" in text
    assert "/model" in text and "/effort" in text
    assert "Resuming preserves the saved model and effort unless you request changes" in text


def test_native_history_is_default_and_legacy_history_is_selection_not_import():
    text = visible(guide(Config()))
    for phrase in ("native Claude settings/history", "existing CLAUDE_CONFIG_DIR",
                   "Nothing is imported or deleted", "ccfleet local --legacy-history --resume",
                   "does not merge it into native history", "Old remote history remains on the slot",
                   "temporary private settings overlay", "not permanently rewritten"):
        assert phrase in text


def test_cleanup_flags_are_not_presented_as_current_session_controls():
    text = visible(guide(Config()))
    assert "Quit a new local session normally with /exit" in text
    assert "Interrupted inference is not silently replayed" in text
    assert "ccfleet local --disconnect is only for explicitly cleaning up" in text
    assert "--reset-link is retired and provides migration guidance" in text
    assert "Old ccfleet project commands remain for deliberate snapshot recovery" in text


def test_foreground_bridge_does_not_claim_native_background_agent_support():
    text = visible(guide(Config()))
    assert re.search(r"Foreground interactive sessions,\s+--print\s*,\s*native resume", text)
    assert "multiple normal terminal sessions are supported" in text
    assert "Native --bg / --background is explicitly rejected" in text
    assert "Managed local background jobs" in text
    assert re.search(r"It is not a remote job, native detached\s+--bg\s*, or an", text)
    assert "computer must stay running and connected" in text


def test_privacy_distinguishes_structured_filtering_from_native_context_and_direct_network():
    for page in (guide(Config()), privacy_page(Config()), how_it_works(Config())):
        text = visible(page)
        for phrase in ("working directory", "MCP", "hooks", "plugins",
                       "directly from the laptop", "all-traffic firewall"):
            assert phrase in text
        assert "metadata" in text
    text = visible(guide(Config()))
    for phrase in ("selected headers", "top-level structured metadata",
                   "does not redact arbitrary prompts or tool results",
                   "Native system prompts may include local OS",
                   "not a fingerprint-free or zero-metadata guarantee",
                   "Slot administrators have root and can inspect or alter",
                   "influence local tool actions", "cannot decrypt the inner SSH model stream"):
        assert phrase in text


def test_privacy_page_preserves_history_and_discloses_old_grant_cleanup():
    text = visible(privacy_page(Config()))
    for phrase in ("Local Claude and model-request privacy", "Native local history/settings",
                   "--legacy-history", "without copying or deleting",
                   "does not undo writes or erase", "associated remote live-folder sessions",
                   "ordinary remote tmux sessions", "recovery backups",
                   "connection IP and timing", "This is not anonymity", "operator console"):
        assert phrase in text


def test_overview_and_how_it_works_describe_local_execution_without_slot_only_overclaim():
    summary = visible(overview(Config()))
    assert "Local Claude Code, your own account on your slot" in summary
    assert "files, tools and history on your computer" in summary
    details = visible(how_it_works(Config()))
    assert "runs original Claude Code on your computer" in details
    assert "remote-terminal compatibility command" in details
    assert "slots may share a physical machine and its internet address" in details
    assert "only Claude Code on the slot talks to Anthropic" not in details


def test_pairing_page_shares_setup_and_native_launch_without_embedding_code_in_command():
    token = "ccf_pair_FAKE_DISPLAY_ONLY"
    page = cli_pairing_page({"id": "test-slot", "name": "Example slot"}, token)
    text = visible(page)
    assert SETUP_COMMAND in page
    assert "Enter this code only if asked" in page
    assert "Setup keeps your existing connection" in page
    assert page.count(token) == 1 and f'class="token">{token}</pre>' in page
    assert "if missing, the original local CLI is installed from the fixed vendor URL" in text
    assert "files, tools, settings and history run locally" in text
    assert "finish" in text.lower() and "approve cancellation" in text
    assert "No project is uploaded during setup" in text
    assert "--legacy-history" in text and "--mode manual" in text


def test_readme_and_current_relay_guide_share_setup_and_native_semantics():
    for relative in ("README.md", "docs/local-relay.md"):
        text = " ".join((ROOT / relative).read_text().split()).replace("**", "")
        assert SETUP_COMMAND in text
        for phrase in ("--legacy-history", "--fork-session", "--print", "--continue",
                       "--disconnect", "--reset-link", "MCP", "slot", "local"):
            assert phrase in text
        assert "no longer used anywhere else" in text
        assert "fingerprint-free" in text


def test_old_workspace_guide_is_tombstone_and_keeps_legacy_recovery_scoped():
    text = " ".join((ROOT / "docs/project-workspaces.md").read_text().split())
    assert text.startswith("# Retired remote workspaces")
    assert "[local Claude setup and migration](local-relay.md)" in text
    assert "primary workflow" in text
    assert "1,000 files, 4 MiB per file and 20 MiB total" in text
    assert "apply only to those legacy transfer commands" in text
    assert "ordinary remote tmux" in text.lower()
    assert "Do not stop a real user's old session" in text


def test_current_guide_scopes_verification_and_interrupted_migration_recovery():
    text = " ".join((ROOT / "docs/local-relay.md").read_text().split())
    for phrase in ("EOFError: filesystem connection ended", "shutdown lock",
                   "Do not delete configuration, remove keys, or re-pair",
                   "marked retired", "ordinary shell prompt", "does not automatically",
                   "mktemp -d /tmp/ccfleet-check.XXXXXX", "outside Claude",
                   "mounted/shared filesystem could also pass", "does not prove where every",
                   "Neither this test nor a readiness check proves zero metadata"):
        assert phrase in text


def test_historical_verification_and_remote_design_point_to_current_architecture():
    for relative in ("docs/live-folders-verification.md", "docs/project-workspaces-verification.md"):
        text = (ROOT / relative).read_text()
        assert "local-relay-verification.md" in text
        assert "retired" in text.lower()
    design = " ".join((ROOT / "docs/design.md").read_text().split())
    assert "remote-terminal compatibility design" in design
    assert "not local Claude" in design
    assert "default hosted-terminal path" not in design


def test_compliance_keeps_historical_provenance_and_does_not_claim_relay_authorization():
    text = " ".join((ROOT / "docs/compliance.md").read_text().split()).replace("**", "")
    assert "Last re-verified against Anthropic's published documentation: 2026-09-28" in text
    assert "https://code.claude.com/docs/en/legal-and-compliance" in text
    assert "not covered by the historical hosted terminal mapping" in text
    assert "claims no approval for the relay" in text
    assert "source record below was not re-fetched" in text
    assert "use the unmodified Claude Code binary" in text
    assert "do not collect, store or intermediate Claude.ai credentials" in text
    assert "Do not promise anonymity" in text
