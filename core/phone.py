"""
The action vocabulary: everything Jarvis is allowed to do to your phone.

This module doesn't touch the phone itself — the Android app does that, via
Accessibility Service. What lives here is the contract between them: the list
of actions that exist, what each one needs, and how much damage each one can do
if the brain misheard you.

That last part is the important one. A language model turning speech into
actions will occasionally be confidently wrong, and the cost of being wrong is
wildly uneven. Opening Spotify when you wanted YouTube is a shrug. Sending "I'm
breaking up with you" to your mother because two contacts sounded alike is not
recoverable by pressing undo.

So every action carries a risk level, and risk — not the model's confidence —
decides whether you get asked first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Risk(Enum):
    """How bad is it if the brain got this wrong?"""

    SAFE = "safe"        # reversible, invisible to anyone else
    MODERATE = "moderate"  # changes device state others might notice
    OUTBOUND = "outbound"  # reaches another human. Cannot be unsent.


# Below this, we ask you to confirm even for SAFE actions — the model is
# telling us it didn't really understand.
CONFIDENCE_FLOOR = 0.7


@dataclass(frozen=True)
class Action:
    name: str
    description: str
    params: tuple[str, ...]
    risk: Risk
    required: tuple[str, ...] = field(default_factory=tuple)

    def missing_params(self, supplied: dict[str, Any]) -> list[str]:
        return [
            p for p in self.required
            if p not in supplied or supplied[p] in (None, "")
        ]


ACTIONS: dict[str, Action] = {a.name: a for a in (
    # ---- Reaching other people. Always confirmed. ----
    Action(
        name="send_sms",
        description="Send a text message to a contact",
        params=("contact", "message"),
        required=("contact", "message"),
        risk=Risk.OUTBOUND,
    ),
    Action(
        name="place_call",
        description="Dial a contact and hand the phone to the user to talk",
        params=("contact",),
        required=("contact",),
        risk=Risk.OUTBOUND,
    ),
    Action(
        name="call_and_speak",
        description=(
            "Dial a contact, enable speakerphone, and read a message aloud so "
            "the microphone picks it up. Sounds robotic and echoey — prefer "
            "send_sms unless the user specifically insists on a phone call"
        ),
        params=("contact", "message"),
        required=("contact", "message"),
        risk=Risk.OUTBOUND,
    ),
    Action(
        name="whatsapp_message",
        description="Send a WhatsApp text message to a contact",
        params=("contact", "message"),
        required=("contact", "message"),
        risk=Risk.OUTBOUND,
    ),
    Action(
        name="whatsapp_voice_note",
        description=(
            "Send a WhatsApp voice note in a synthesised voice. Use when the "
            "user wants the recipient to hear a voice rather than read text"
        ),
        params=("contact", "message"),
        required=("contact", "message"),
        risk=Risk.OUTBOUND,
    ),

    # ---- Changes device state. Noticeable but recoverable. ----
    Action(
        name="set_alarm",
        description="Set an alarm at a specific time",
        params=("time", "label"),
        required=("time",),
        risk=Risk.MODERATE,
    ),
    Action(
        name="set_timer",
        description="Start a countdown timer for a duration in seconds",
        params=("seconds", "label"),
        required=("seconds",),
        risk=Risk.MODERATE,
    ),
    Action(
        name="toggle_setting",
        description=(
            "Turn a device setting on or off: wifi, bluetooth, torch, "
            "do_not_disturb, airplane_mode"
        ),
        params=("setting", "state"),
        required=("setting", "state"),
        risk=Risk.MODERATE,
    ),
    Action(
        name="ui_tap",
        description=(
            "Tap an on-screen element described in plain words, e.g. 'the blue "
            "Continue button'. The escape hatch for apps with no better route"
        ),
        params=("target",),
        required=("target",),
        risk=Risk.MODERATE,
    ),
    Action(
        name="ui_type",
        description="Type text into the currently focused input field",
        params=("text",),
        required=("text",),
        risk=Risk.MODERATE,
    ),

    # ---- Reversible, private, cheap to get wrong. ----
    Action(
        name="open_app",
        description="Open an app by name",
        params=("app",),
        required=("app",),
        risk=Risk.SAFE,
    ),
    Action(
        name="play_music",
        description="Play music matching a search query",
        params=("query",),
        required=("query",),
        risk=Risk.SAFE,
    ),
    Action(
        name="navigate",
        description="Start turn-by-turn navigation to a destination",
        params=("destination",),
        required=("destination",),
        risk=Risk.SAFE,
    ),
    Action(
        name="read_screen",
        description="Describe what is currently on the phone screen",
        params=(),
        risk=Risk.SAFE,
    ),
    Action(
        name="answer",
        description=(
            "Just talk back to the user — no phone action needed. Use for "
            "questions, chat, and anything conversational"
        ),
        params=("text",),
        required=("text",),
        risk=Risk.SAFE,
    ),
)}


def catalogue() -> list[dict[str, Any]]:
    """The action list in the shape brain.intent() wants."""
    return [
        {"name": a.name, "description": a.description, "params": list(a.params)}
        for a in ACTIONS.values()
    ]


@dataclass
class Decision:
    """What to do with an intent, and whether to ask first."""

    intent: dict[str, Any]
    allowed: bool
    needs_confirmation: bool
    reason: str = ""

    @property
    def action_name(self) -> str:
        return self.intent.get("action", "none")


def evaluate(intent: dict[str, Any]) -> Decision:
    """Decide whether an intent can run, and whether to confirm it first."""
    name = intent.get("action", "none")

    if name == "none":
        return Decision(
            intent=intent,
            allowed=False,
            needs_confirmation=False,
            reason=intent.get("speak") or "I didn't understand that.",
        )

    action = ACTIONS.get(name)
    if action is None:
        # The model invented an action. Refuse rather than guess at a mapping —
        # a near-miss on an OUTBOUND action is exactly what we can't afford.
        return Decision(
            intent=intent,
            allowed=False,
            needs_confirmation=False,
            reason=f"I don't know how to do '{name}'.",
        )

    params = intent.get("params", {})
    missing = action.missing_params(params)
    if missing:
        return Decision(
            intent=intent,
            allowed=False,
            needs_confirmation=False,
            reason=f"I need to know the {' and '.join(missing)} first.",
        )

    confidence = intent.get("confidence", 0.0)

    # Anything that reaches another person is confirmed every time, no matter
    # how confident the model claims to be. Confidence is the model's opinion
    # of itself, and an unsendable message is not worth trusting it over.
    if action.risk is Risk.OUTBOUND:
        return Decision(
            intent=intent,
            allowed=True,
            needs_confirmation=True,
            reason="reaches someone else",
        )

    if confidence < CONFIDENCE_FLOOR:
        return Decision(
            intent=intent,
            allowed=True,
            needs_confirmation=True,
            reason=f"not confident I understood ({confidence:.0%})",
        )

    if action.risk is Risk.MODERATE and confidence < 0.85:
        return Decision(
            intent=intent,
            allowed=True,
            needs_confirmation=True,
            reason="changes a device setting",
        )

    return Decision(intent=intent, allowed=True, needs_confirmation=False)


def describe_for_confirmation(intent: dict[str, Any]) -> str:
    """A one-line, read-it-aloud summary of what is about to happen.

    Spoken back before any confirmed action, so the failure mode is you hearing
    the wrong contact's name rather than discovering it in the sent folder.
    """
    name = intent.get("action", "none")
    p = intent.get("params", {})

    match name:
        case "send_sms" | "whatsapp_message":
            channel = "WhatsApp" if name.startswith("whatsapp") else "text"
            return f"Send this {channel} to {p.get('contact')}: \"{p.get('message')}\"?"
        case "whatsapp_voice_note":
            return f"Send {p.get('contact')} a voice note saying: \"{p.get('message')}\"?"
        case "place_call":
            return f"Call {p.get('contact')}?"
        case "call_and_speak":
            return (
                f"Call {p.get('contact')} on speakerphone and read out: "
                f"\"{p.get('message')}\"? It will sound robotic."
            )
        case "set_alarm":
            return f"Set an alarm for {p.get('time')}?"
        case "set_timer":
            return f"Start a {p.get('seconds')} second timer?"
        case "toggle_setting":
            return f"Turn {p.get('state')} {str(p.get('setting', '')).replace('_', ' ')}?"
        case "ui_tap":
            return f"Tap {p.get('target')}?"
        case "ui_type":
            return f"Type \"{p.get('text')}\"?"
        case _:
            return f"Go ahead with {name}?"
