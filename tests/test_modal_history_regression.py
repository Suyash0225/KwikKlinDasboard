from pathlib import Path


def test_dashboard_modal_explicit_close_clears_modal_history() -> None:
    source = Path("app/static/app.js").read_text(encoding="utf-8")
    close = source.split("function closeModal()", 1)[1].split("\n}", 1)[0]
    assert 'history.state.kk === "modal"' in close
    assert 'history.replaceState({ kk: "nav" }, "", location.href)' in close
    assert "syncOverlayScrollLock();" in close


def test_staff_modal_explicit_close_clears_modal_history() -> None:
    source = Path("app/static/staff.js").read_text(encoding="utf-8")
    close = source.split("function closeModal()", 1)[1].split("\n}", 1)[0]
    assert 'history.state.kk === "modal"' in close
    assert 'history.replaceState({ kk: "nav", nav: NAV }, "")' in close


def test_modal_history_fix_refreshes_both_script_cache_versions() -> None:
    dashboard = Path("app/static/dashboard.html").read_text(encoding="utf-8")
    staff = Path("app/static/staff.html").read_text(encoding="utf-8")
    assert "app.js?v=ui2-" in dashboard  # bumped after the modal-history fix
    assert "staff.js?v=s16" in staff  # bumped after the modal-history fix
