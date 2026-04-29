from negotiate_safe.cancel_flow import (
    CANCEL_COMPLETED_REFUSED_EVENT,
    CANCELED_BEFORE_DEAL_EVENT,
    RESCINDED_AFTER_SIGN_EVENT,
    cancel_preflight,
    cancel_success_event_type,
)


def test_cancel_preflight_refuses_completed_sessions():
    decision = cancel_preflight(" completed ")
    assert decision.action == "refuse"
    assert decision.return_code == 1
    assert decision.event_type == CANCEL_COMPLETED_REFUSED_EVENT
    assert decision.status == "completed"
    assert decision.should_continue is False


def test_cancel_preflight_noops_terminal_cancel_statuses():
    for status in ("canceled", "rescinded", "rescinded_after_sign", "expired"):
        decision = cancel_preflight(status)
        assert decision.action == "noop"
        assert decision.return_code == 0
        assert decision.status == status


def test_cancel_preflight_continues_for_active_statuses():
    for status in ("open", "joined", "signing", ""):
        decision = cancel_preflight(status)
        assert decision.action == "continue"
        assert decision.return_code is None
        assert decision.should_continue is True


def test_cancel_success_event_type_tracks_rescind_flag():
    assert cancel_success_event_type(rescind=False) == CANCELED_BEFORE_DEAL_EVENT
    assert cancel_success_event_type(rescind=True) == RESCINDED_AFTER_SIGN_EVENT
