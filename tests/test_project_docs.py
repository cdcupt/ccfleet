"""Public migration guidance matches the slot-native project connector boundary."""

from pathlib import Path

from ccfleet_agent import project_files
from ccfleetd.config import Config
from ccfleetd.customer_docs import guide, how_it_works
from ccfleetd.usersite import privacy_page

ROOT = Path(__file__).parents[1]


def test_guide_has_a_linkable_migration_section_for_each_existing_user_path():
    page = guide(Config())
    assert 'href="#migration"' in page
    assert 'id="migration"' in page
    migration = page.split('id="migration"', 1)[1].split('id="project-workspaces"', 1)[0]
    assert "You still use ccfleet-connect" in migration
    assert "laptop/install.sh | bash -s -- --migrate" in migration
    assert "pairs the new client first" in migration
    assert "only after pairing succeeds" in migration
    assert "This computer is already paired" in migration
    assert "do not pair again or repeat" in migration
    assert "laptop/install.sh | bash</code>" in migration
    assert "You used the local-agent relay preview" in migration
    assert "Old local conversation history stays on this computer" in migration
    assert "not imported into the slot" in migration
    assert "existing remote workspace stay in place" in migration


def test_guide_introduces_explicit_project_sharing_with_a_non_inference_readiness_check():
    page = guide(Config())
    assert "ccfleet local --check" in page
    assert "ccfleet project status" in page
    assert "ccfleet local --new --name work" in page
    assert "Review the file list and confirm the first share" in page
    assert "enabled for currently assigned hosted slots" in page
    assert "Newly created or reassigned slots still need operator activation" in page
    assert "Installing the client or running the check does not activate access" in page
    assert "check sends no project files and makes no model request" in page
    assert "No local Claude Code installation is required" in page
    assert "runs Claude Code and all agent tools on the slot, not your laptop" in page


def test_guide_covers_manual_transfer_recovery_and_multi_device_workflows():
    page = guide(Config())
    for command in ("ccfleet project diff", "ccfleet project pull", "ccfleet project push",
                    "ccfleet project list", "--remote-project ID --project PATH"):
        assert command in page
    assert "There is no automatic push or pull on reconnect" in page
    assert "All sessions for that project must finish before another upload" in page
    assert "<code>/exit</code>" in page
    assert "Closing a terminal only disconnects it" in page
    assert "project-backups" in page
    assert "refuses conflicting local/slot edits" in page
    assert "not an all-files transaction" in page
    assert "Each computer remains separately revocable" in page


def test_guide_has_native_session_controls_and_retires_local_agent_flags():
    page = guide(Config())
    for command in ("ccfleet local --continue", "ccfleet local --resume",
                    "ccfleet local --resume work", "--mode plan --model opus --effort high"):
        assert command in page
    assert "picker of running project sessions" in page
    assert "Project sessions default to manual permissions" in page
    assert "<code>/model</code>" in page and "<code>/effort</code>" in page
    assert "retired local <code>--print</code> and <code>--fork-session</code>" in page
    assert "options are not supported" in page


def test_project_docs_state_selection_limits_and_do_not_promise_anonymity():
    page = guide(Config())
    assert f"{project_files.MAX_FILES:,} files" in page
    assert f"{project_files.MAX_FILE_BYTES // (1024 * 1024)} MiB per file" in page
    assert f"{project_files.MAX_TOTAL_BYTES // (1024 * 1024)} MiB total" in page
    for term in ("relative filenames", "executable flags", "environment variables",
                 "timezone", "host fingerprint", "incoming IP address", "client version",
                 "terminal dimensions", "Slot administrators have root",
                 "cannot detect every secret", "not an operating-system sandbox"):
        assert term in page
    assert "not an anonymity guarantee" in page
    assert "relevant content is sent to Anthropic" in page
    assert "remote agent has no connector command for executing a laptop shell" in page


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
    assert "enabled for currently assigned hosted slots" in current
    assert "reassigned slots still need operator activation" in current
    assert "code does not activate a slot" in current
    assert "no background synchronization" in current
    assert "not an operating-system sandbox" in current
    assert "no deployed revision is asserted" in current


def test_readme_and_compliance_describe_the_replacement_without_credential_substitution():
    readme = (ROOT / "README.md").read_text()
    compliance = (ROOT / "docs/compliance.md").read_text()
    assert "## Selected projects, slot-only execution" in readme
    assert "all agent tools **on the slot**" in readme
    assert "docs/project-workspaces.md" in readme
    assert "old local-agent credential-substitution relay is retired" in compliance
    assert "It does not launch Claude on the laptop" in compliance
    assert "Do not promise anonymity" in compliance


def test_privacy_policy_discloses_explicit_project_transfers_and_retained_copies():
    page = privacy_page(Config())
    for text in ("Selected project sharing", "relative filenames", "transfer manifests",
                 "recovery backups", "connection IP and timing", "not anonymity",
                 "does not delete those files", "giving the slot back wipes its copies",
                 "relevant shared content is sent to Anthropic", "operator console"):
        assert text in page
    assert '/docs/guide#migration' in page
