"""Public migration guidance matches the slot-native project connector boundary."""

from pathlib import Path

from ccfleet_agent import project_files
from ccfleetd.config import Config
from ccfleetd.customer_docs import guide, how_it_works
from ccfleetd.usersite import cli_pairing_page, privacy_page

ROOT = Path(__file__).parents[1]
SETUP_COMMAND = ("curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/"
                 "laptop/install.sh | bash -s -- --setup")


def test_guide_has_one_linkable_setup_path_for_new_paired_and_legacy_computers():
    page = guide(Config())
    assert 'href="#migration"' in page
    assert 'id="migration"' in page
    migration = page.split('id="migration"', 1)[1].split('id="project-workspaces"', 1)[0]
    assert "One-command setup and migration" in migration
    assert "New computer, already paired, or still using <code>ccfleet-connect</code>" in migration
    assert "You do not need to choose a migration mode" in migration
    assert migration.count(SETUP_COMMAND) == 1
    assert "reuses its existing pairing; do not pair again" in migration
    assert "Only if this computer is not paired" in migration
    assert "paste one fresh pairing code when asked" in migration
    assert "only after readiness succeeds" in migration
    assert "If readiness fails, the legacy setup is left in place" in migration
    assert "waits for a new device key to reach the slot" in migration
    assert "without uploading project files or making a model request" in migration
    assert "Open a new terminal, then choose your project" in migration
    assert "cd ~/code/my-project\nccfleet local</code>" in migration
    assert "Setup itself does not share files or start a model session" in migration
    assert "legacy <code>--migrate</code> remain compatibility options" in migration
    assert "laptop/install.sh | bash -s -- --migrate" not in migration
    assert "laptop/install.sh | bash</code>" not in migration
    assert "Exit any running local-agent preview session before updating" in migration
    assert "does not stop or convert an already-running process" in migration
    assert "Old local conversation history stays on this computer" in migration
    assert "not imported into the slot" in migration
    assert "existing remote workspace stay in place" in migration


def test_setup_guide_preserves_configuration_and_leaves_provider_revocation_to_user():
    page = guide(Config())
    for text in ("verifies its digest-pinned helper", "pairing, configuration, slot sign-in",
                 "PATH changes preserve existing shell settings", "keep a backup",
                 'add <code>--name "Personal Mac"</code>', "<code>--slot SLOT</code>",
                 "No Claude password, token, or SSH command is needed",
                 "does not automatically revoke an Anthropic credential",
                 "Revoke an old setup-token yourself only if nothing else uses it"):
        assert text in page


def test_readme_and_workspace_guide_use_the_same_unified_setup_command():
    for relative in ("README.md", "docs/project-workspaces.md"):
        document = (ROOT / relative).read_text()
        normalized = " ".join(document.split())
        assert SETUP_COMMAND in document
        assert "--name \"Personal Mac\"" in document
        assert "--slot SLOT" in document
        assert "ccfleet local" in document
        assert "no longer used anywhere else" in normalized
        assert "--migrate" in document  # retained only as a compatibility option
        assert "| bash -s -- --migrate" not in document
        assert "without uploading" in normalized
        assert "making a model request" in normalized


def test_guide_introduces_live_folder_trust_with_a_non_inference_readiness_check():
    page = guide(Config())
    assert "ccfleet local --check" in page
    assert "ccfleet project status" in page
    assert "ccfleet local --new --name work" in page
    assert "Confirm the selected folder's live read/write trust prompt" in page
    assert "live folders require an upgraded node and operator-enabled access" in page
    assert "Newly created or reassigned slots need operator activation" in page
    assert "Installing the client or running the check does not activate access" in page
    assert "check sends no project files and makes no model request" in page
    assert "No local Claude Code installation is required" in page
    assert "runs Claude Code and all agent tools on the slot, not your laptop" in page


def test_guide_keeps_snapshot_recovery_separate_from_the_live_workflow():
    page = guide(Config())
    primary, legacy = page.split('<details><summary>Legacy snapshot recovery</summary>', 1)
    for command in ("ccfleet project diff", "ccfleet project pull", "ccfleet project push",
                    "ccfleet project list", "--remote-project ID --project PATH"):
        assert command in legacy
        assert command not in primary
    assert "They are not part of the live workflow" in legacy
    assert "Legacy snapshots retain reviewed pulls, conflict checks" in legacy
    assert "project-backups" in legacy
    assert "Each device remains separately revocable" in primary
    assert "There is no whole-project upload and no manual push/pull step" in primary


def test_guide_has_native_session_controls_and_retires_local_agent_flags():
    page = guide(Config())
    for command in ("ccfleet local --continue", "ccfleet local --resume",
                    "ccfleet local --resume work", "--mode plan --model opus --effort high"):
        assert command in page
    assert "picker of running sessions" in page
    assert "New live sessions default to <code>bypassPermissions</code>" in page
    assert "without individual permission prompts" in page
    assert "Existing sessions keep their settings on reattach" in page
    assert "<code>/model</code>" in page and "<code>/effort</code>" in page
    assert "retired local <code>--print</code> and <code>--fork-session</code>" in page
    assert "options are not supported" in page


def test_live_folder_docs_have_no_snapshot_caps_or_filters_and_do_not_promise_anonymity():
    page = guide(Config())
    assert f"{project_files.MAX_FILES:,} files" in page
    assert f"{project_files.MAX_FILE_BYTES // (1024 * 1024)} MiB per file" in page
    assert f"{project_files.MAX_TOTAL_BYTES // (1024 * 1024)} MiB total" in page
    for term in ("Selected filenames", "filesystem metadata", "environment variables",
                 "timezone", "host fingerprint", "incoming IP address", "client version",
                 "terminal dimensions", "Slot administrators have root",
                 "geolocation", "or an operating-system sandbox"):
        assert term in page
    assert "not an anonymity guarantee" in page
    assert "Relevant content is sent to Anthropic" in page
    assert "not a laptop shell or local process execution" in page
    assert "no Git-ignore or broad hidden-file filtering" in page
    assert "protocol messages and concurrent operations remain resource-bounded" in page
    assert "settings, <code>.ssh</code>, <code>.claude</code>" in page
    assert "CC Fleet private configuration, device keys, host-key pins" in page
    assert "active client/helper files remain protected" in page


def test_live_folder_docs_explain_background_lifetime_explicit_stop_reset_and_linux_tools():
    page = guide(Config())
    assert "Closing the terminal leaves the background folder connector" in page
    assert "ccfleet local --disconnect" in page
    assert "ccfleet local --reset-link" in page
    assert "filesystem operations wait for the connection" in page
    assert "tmux does not prove an interrupted write succeeded" in page
    assert "reset after a warning" in page
    assert "Sharing Mac files does not provide Xcode, macOS-only commands" in page
    assert "cd ~\nccfleet local" in page
    assert "no separate <code>--allow-home</code> flag or home-folder ban" in page
    assert "Live writes can change startup scripts or other files executed locally later" in page


def test_how_it_works_explains_replacement_without_location_or_dedicated_machine_overclaims():
    page = how_it_works(Config())
    assert 'href="/docs/guide#migration"' in page
    assert "retired local-agent preview" in page
    assert "BWH sees your connection IP" in page
    assert "slots may share a physical machine and its internet address" in page
    assert "says nothing about where you are" not in page
    assert "Each slot is a machine of its own" not in page


def test_retired_relay_doc_points_to_current_design_and_keeps_history_local():
    old = (ROOT / "docs/local-relay.md").read_text()
    current = (ROOT / "docs/project-workspaces.md").read_text()
    assert "# Local-agent relay retired" in old
    assert "[project workspaces](project-workspaces.md)" in old
    assert "history is retained locally" in old
    normalized = " ".join(current.split())
    assert "reassigned slots need operator activation" in normalized
    assert "code does not activate a slot" in normalized
    assert "## Legacy snapshot recovery only" in current
    assert "background folder connector active" in normalized
    assert "do not turn folder access into a laptop OS sandbox" in normalized
    assert "no deployed revision is asserted" in current


def test_readme_and_compliance_describe_the_replacement_without_credential_substitution():
    readme = (ROOT / "README.md").read_text()
    compliance = (ROOT / "docs/compliance.md").read_text()
    assert "## Live folders, slot-only execution" in readme
    assert "all agent tools **on the slot**" in readme
    assert "docs/project-workspaces.md" in readme
    assert "old local-agent credential-substitution relay is retired" in compliance
    assert "It does not launch Claude on the laptop" in compliance
    assert "Do not promise anonymity" in compliance


def test_privacy_policy_discloses_live_writes_home_risks_and_retained_information():
    page = privacy_page(Config())
    for text in ("Live folder access", "filesystem metadata", "transfer manifests",
                 "recovery backups", "connection IP and timing", "not anonymity",
                 "does not delete those files", "Giving the slot back stops folder access",
                 "relevant shared content is sent to Anthropic", "operator console"):
        assert text in page
    assert "writes and deletions affect your laptop immediately" in page
    assert "Selecting home can expose settings, SSH keys, local Claude files" in page
    assert "Closing the terminal leaves the background folder connector active" in page
    assert "does not undo writes or erase content already read" in page
    assert "device keys, host-key pins and active client/helper files are protected" in page
    assert '/docs/guide#migration' in page


def test_slot_pairing_page_uses_the_same_setup_command_without_embedding_the_code():
    token = "ccf_pair_FAKE_DISPLAY_ONLY"
    page = cli_pairing_page({"id": "slot-test", "name": "test-slot"}, token)
    assert "laptop/install.sh | bash -s -- --setup</pre>" in page
    assert "--migrate" not in page
    assert "Enter this code only if asked" in page
    assert "Setup keeps your existing connection" in page
    assert page.count(token) == 1 and f'class="token">{token}</pre>' in page
    assert "No project is uploaded during setup" in page
    assert "ccfleet login" in page and "ccfleet local" in page
