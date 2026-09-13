import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_administration_interfaces_are_mounted_once_in_the_compact_panel():
    html = (ROOT / "index.html").read_text()
    before_panel, panel = html.split("<!-- ADMINISTRATION PANEL", 1)
    for element_id in ("staff-tab-users", "staff-tab-account", "staff-tab-learn", "staff-tab-approvals"):
        assert html.count(f'id="{element_id}"') == 1
        assert f'id="{element_id}"' not in before_panel
        assert f'id="{element_id}"' in panel

    assert html.count('id="aiApprovalList"') == 1
    assert html.count('id="logoutBtn"') == 1
    assert 'id="logoutBtn"' not in before_panel
    assert 'class="administration-footer"' in panel

    ids = re.findall(r'\bid="([^"]+)"', html)
    duplicates = {value for value in ids if ids.count(value) > 1}
    assert not duplicates
    assert 'id="administrationModal"' in html
    assert 'id="administrationOpenBtn"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html


def test_administration_panel_reuses_existing_loaders_and_is_role_controlled():
    app_js = (ROOT / "js" / "app.js").read_text()
    portal_js = (ROOT / "portal-ui.js").read_text()
    css = (ROOT / "css" / "style.css").read_text()

    assert "function administrationDefinitions(role)" in app_js
    assert "loadStaffUsers" in app_js
    assert "loadStaffAccount" in app_js
    assert "loadStaffLearn" in app_js
    assert "ROLE_ADMIN" in app_js and "ROLE_VERIFICATION_OFFICER" in app_js
    assert "event.key === 'Escape'" in app_js
    assert "administrationOpen" in app_js
    assert "administration-modal" in css
    assert "body.administration-open" in css
    # The map-link helper remains, but the old dynamic admin menu/hiding
    # implementation is gone; the canonical app now owns permissions and tabs.
    assert "land-map-file-link" in portal_js
    assert "globalAdministration" not in portal_js
    assert "admin-menu" not in portal_js


def test_staff_navigation_no_longer_exposes_admin_sections_as_main_tabs():
    app_js = (ROOT / "js" / "app.js").read_text()
    assert "['learn', '🧠 AI Corrections']" not in app_js
    assert "['users', '👥 Users']" not in app_js
    assert "['account', '⚙️ Settings']" not in app_js
    assert "label:'AI Correction'" in app_js
    assert "label:'Settings'" in app_js


def test_users_workspace_has_role_filters_search_pagination_and_compact_actions():
    html = (ROOT / "index.html").read_text()
    app_js = (ROOT / "js" / "app.js").read_text()
    css = (ROOT / "css" / "style.css").read_text()
    for element_id in (
        "staffAddUserToggle", "staffUserCreatePanel", "staffUserRoleTabs",
        "staffUserSearch", "staffUserSummary", "staffUserPagination", "staffUsersTable",
    ):
        assert f'id="{element_id}"' in html
    assert "STAFF_USER_PAGE_SIZE = 10" in app_js
    assert "ROLE_ADMIN" in app_js and "ROLE_VERIFICATION_OFFICER" in app_js
    assert "filteredStaffUsers" in app_js
    assert "renderStaffUserPagination" in app_js
    assert "data-user-menu-toggle" in app_js
    assert ".user-role-tab" in css
    assert ".users-action-menu" in css


def test_ai_approval_is_an_administrator_tab_and_assistant_opens_administration():
    app_js = (ROOT / "js" / "app.js").read_text()
    assistant_js = (ROOT / "js" / "admin-assistant.js").read_text()

    assert "key:'approvals'" in app_js
    assert "label:'AI Approval'" in app_js
    assert "pane:'staff-tab-approvals'" in app_js
    assert "loadAiApprovals" in app_js
    assert "'approvals'" not in app_js.split("function switchStaffTab", 1)[1].split("async function loadStaffDashboard", 1)[0]
    assert "openAdministration(\"approvals\")" in assistant_js
    assert "switchStaffTab(\"approvals\")" not in assistant_js


def test_logout_is_administration_footer_action_with_quiet_automatic_logout():
    app_js = (ROOT / "js" / "app.js").read_text()
    css = (ROOT / "css" / "style.css").read_text()

    assert "if(!quiet && !window.confirm('Are you sure you want to log out?')) return;" in app_js
    assert "if(token && !quiet)" in app_js
    assert ".administration-footer" in css
    assert ".administration-logout" in css
