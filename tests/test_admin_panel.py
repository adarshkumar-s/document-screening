import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_administration_interfaces_are_mounted_once_in_the_compact_panel():
    html = (ROOT / "index.html").read_text()
    before_panel, panel = html.split("<!-- ADMINISTRATION PANEL", 1)
    for element_id in ("staff-tab-users", "staff-tab-account", "staff-tab-learn"):
        assert html.count(f'id="{element_id}"') == 1
        assert f'id="{element_id}"' not in before_panel
        assert f'id="{element_id}"' in panel

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
