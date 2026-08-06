"""
Tests for the confirm-or-act decision.

The asymmetry being defended: opening the wrong app costs a shrug, sending the
wrong message to the wrong person cannot be undone at all. So these tests care
far more about false negatives on OUTBOUND actions than about anything else.
"""

import pytest

from core import phone


def intent(action, params=None, confidence=1.0):
    return {
        "action": action,
        "params": params or {},
        "confidence": confidence,
        "speak": "",
    }


class TestOutboundAlwaysConfirms:
    @pytest.mark.parametrize("action,params", [
        ("send_sms", {"contact": "mum", "message": "hi"}),
        ("place_call", {"contact": "mum"}),
        ("call_and_speak", {"contact": "mum", "message": "hi"}),
        ("whatsapp_message", {"contact": "mum", "message": "hi"}),
        ("whatsapp_voice_note", {"contact": "mum", "message": "hi"}),
    ])
    def test_confirmed_even_at_full_confidence(self, action, params):
        # Confidence is the model's opinion of itself. An unsendable message
        # is not worth trusting it over.
        decision = phone.evaluate(intent(action, params, confidence=1.0))
        assert decision.allowed
        assert decision.needs_confirmation


class TestSafeActionsRunDirectly:
    @pytest.mark.parametrize("action,params", [
        ("open_app", {"app": "spotify"}),
        ("play_music", {"query": "radiohead"}),
        ("navigate", {"destination": "college"}),
        ("read_screen", {}),
    ])
    def test_high_confidence_safe_actions_need_no_confirmation(self, action, params):
        decision = phone.evaluate(intent(action, params, confidence=0.95))
        assert decision.allowed
        assert not decision.needs_confirmation

    def test_low_confidence_still_confirms(self, ):
        decision = phone.evaluate(intent("open_app", {"app": "spotify"}, confidence=0.4))
        assert decision.needs_confirmation


class TestModerateActions:
    def test_high_confidence_runs(self):
        decision = phone.evaluate(
            intent("set_alarm", {"time": "08:00"}, confidence=0.95)
        )
        assert not decision.needs_confirmation

    def test_middling_confidence_confirms(self):
        decision = phone.evaluate(
            intent("set_alarm", {"time": "08:00"}, confidence=0.8)
        )
        assert decision.needs_confirmation


class TestRejection:
    def test_unknown_action_is_refused(self):
        # A hallucinated action must never be fuzzy-matched onto a real one —
        # a near-miss on an OUTBOUND action is precisely what we can't afford.
        decision = phone.evaluate(intent("wire_transfer", {"amount": "1000"}))
        assert not decision.allowed

    def test_none_action_is_refused(self):
        decision = phone.evaluate(intent("none"))
        assert not decision.allowed

    def test_missing_required_params_are_refused(self):
        decision = phone.evaluate(intent("send_sms", {"contact": "mum"}))
        assert not decision.allowed
        assert "message" in decision.reason

    def test_empty_param_counts_as_missing(self):
        decision = phone.evaluate(intent("send_sms", {"contact": "mum", "message": ""}))
        assert not decision.allowed


class TestConfirmationWording:
    def test_sms_confirmation_names_contact_and_message(self):
        # This is the sentence read aloud, and the failure it catches is the
        # model hearing the wrong name. You only catch that if you hear both.
        text = phone.describe_for_confirmation(
            intent("send_sms", {"contact": "mum", "message": "reached college safely"})
        )
        assert "mum" in text
        assert "reached college safely" in text

    def test_call_and_speak_warns_about_quality(self):
        text = phone.describe_for_confirmation(
            intent("call_and_speak", {"contact": "mum", "message": "hi"})
        )
        assert "robotic" in text.lower()

    def test_every_action_has_a_confirmation_string(self):
        for name, action in phone.ACTIONS.items():
            text = phone.describe_for_confirmation(
                intent(name, {p: "x" for p in action.params})
            )
            assert text and isinstance(text, str)


class TestCatalogue:
    def test_catalogue_covers_every_action(self):
        assert len(phone.catalogue()) == len(phone.ACTIONS)

    def test_every_entry_is_described_for_the_model(self):
        for entry in phone.catalogue():
            assert entry["description"].strip()
