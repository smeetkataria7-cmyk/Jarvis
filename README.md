# Jarvis

A private assistant. The brain runs on your own computer, your phone is the
mouth and hands, and nothing is exposed to the internet.

It can hold a conversation, remember things about you, carry out tasks on your
Android phone, and propose changes to its own code — which it cannot apply
until you approve them with your fingerprint.

---

## How it fits together

```
   your phone                    your computer
  ┌───────────┐                 ┌──────────────────────────┐
  │  Jarvis   │   home WiFi     │  core/    the assistant  │
  │    app    │ ◄─────────────► │           (Jarvis may    │
  │           │   HTTP + token  │            edit this)    │
  │ voice     │                 │                          │
  │ hands     │                 │  guardian/  the gate     │
  │ fingerprint                 │           (Jarvis may    │
  └───────────┘                 │            NEVER edit)   │
                                └──────────────────────────┘
```

Nothing listens on the public internet. Jarvis binds to your LAN, and your
phone reaches it the same way it reaches your printer.

---

## The two ideas worth understanding

**1. The gate is outside the thing it guards.**

Jarvis can rewrite its own brain, its skills, and how it remembers you. It
cannot touch `guardian/`. If it could, the first change it would be able to
propose is *"remove the part that asks permission"* — and every approval after
that would be theatre. The protected list lives in
`guardian/protected.py`, and the tests that prove it holds live in
`tests/guardian/`, which is also protected.

**2. Your fingerprint is proof, not a claim.**

The naive version has the phone run a fingerprint check and then tell the
server "approved". That proves nothing — anything on your WiFi could send the
same bytes.

Instead, the phone holds a signing key generated inside the Android hardware
Keystore with `setUserAuthenticationRequired(true)`. The secure element
physically refuses to sign until a fingerprint succeeds. So a signature that
verifies is evidence a human approved, because there is no other way those
bytes could exist.

---

## Setting it up

### 1. The computer

```bash
git clone https://github.com/smeetkataria7-cmyk/Jarvis
cd Jarvis
pip3 install -r requirements.txt

cp config.example.yaml config.yaml
```

Open `config.yaml` and set two things.

**Your brain.** Pick one:

```yaml
brain:
  provider: openai        # you already have a key
  openai:
    api_key: "sk-..."
    model: gpt-4o         # use a model your account can actually access
```

Or free and fully local, if you'd rather nothing leaves your machine:

```yaml
brain:
  provider: ollama
  ollama:
    model: llama3.1:8b
```

**Your auth token.** Generate one:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Paste it into `server.auth_token`. Without it, anything on your WiFi could talk
to Jarvis.

Then:

```bash
python3 run.py
```

It prints a 6-digit enrollment code. Leave the window open.

### 2. The phone

Open `android/` in Android Studio and run it onto your phone over USB.

Then, in order:

1. **Pair** — enter your computer's local IP (`192.168.x.x`), the auth token,
   and the 6-digit code. This generates the hardware-backed key and sends only
   its public half.
2. **Enable phone control** — Settings → Accessibility → Jarvis. Android will
   warn you about what this permission allows. The warning is accurate; grant
   it only because you built this yourself.

---

## What it can and cannot do to your phone

| | |
|---|---|
| Send texts and WhatsApp messages | Yes |
| Place calls | Yes |
| Tap, type, scroll, navigate any app | Yes |
| Open apps, set alarms, play music, navigate | Yes |
| Read what's on screen | Yes |
| **Talk on a live phone call** | **No** |
| **See inside banking apps** | **No** |

The call limitation is not a gap in the code. Android closes the telephony
uplink to apps so malware cannot impersonate you to your bank, and there is no
API to reopen it. The `call_and_speak` action works around it by dialling,
enabling speakerphone, and reading the message aloud into the microphone. It
genuinely transmits, and it genuinely sounds like a robot in a tunnel. Prefer
a text.

Banking and password apps set `FLAG_SECURE`, and their windows read back blank.
That is the system protecting you from exactly the access this app has.

---

## Anything that reaches another person is confirmed

Actions are tiered by what it costs to get them wrong:

- **Safe** — opening an app, playing music. Runs immediately.
- **Moderate** — alarms, settings, taps. Confirmed if the model isn't sure.
- **Outbound** — texts, calls, WhatsApp. **Always** confirmed, read back aloud
  with the contact name and the full message, no matter how confident the model
  claims to be.

That last rule is not caution for its own sake. Model confidence is the model's
opinion of itself, and a message sent to the wrong person cannot be unsent.

---

## When Jarvis changes its own code

1. It writes a diff, based on its own failure logs rather than a hunch
2. The Guardian checks it touches nothing protected, and isn't too big to read
3. It's applied to a **scratch branch** and the tests run there
4. **Only if the tests pass** does your phone buzz
5. You read the diff, then your fingerprint approves it
6. It's merged, and the commit records that you approved it
7. If anything fails at any point, hard reset to the last good commit

Step 3 matters more than it looks. Your attention is the scarcest thing in this
system. Spending it on patches that were always going to fail is how a security
gate decays into a button you press without reading — and a button you press
without reading is not a gate.

Every change is a git commit, so nothing is ever unrecoverable.

Turn it off entirely with `selfedit.enabled: false`.

---

## What "self-learning" means here

Honestly: the model does not get smarter. Its weights are fixed. Nothing on a
personal machine changes that, and anyone claiming otherwise is selling
something.

What does happen is that Jarvis accumulates context — that your mother is "Mum"
in your contacts, that you leave at 8:40, that you always want the text rather
than the call. Same model, better material. In daily use the difference is
invisible; it just knows you.

It also logs every action and whether it worked (`/api/reliability`). When
tapping a particular app fails nine times in ten, that pattern is visible, and
it's the honest evidence a self-edit proposal should be built on.

---

## Tests

```bash
python3 -m pytest tests/ -q
```

---

## Status

Complete, with 98 tests passing:

| Computer | Phone |
|---|---|
| Brain — OpenAI, Anthropic, Ollama | Pairing and hardware keystore |
| Memory — conversation, facts, outcomes | Voice in and out |
| Risk tiering and confirmation | Accessibility phone control |
| Guardian — protected paths, signatures | Contact resolution |
| Self-edit proposer and patch pipeline | Action execution |
| | Biometric patch approval |

The Android app has never been compiled — this repo was built in a container
with no Android SDK. The Kotlin is written against real APIs, but expect to fix
a few things the first time Android Studio looks at it.

### Known limitations, stated plainly

- **No wake word.** You tap the mic. Always-on listening needs a foreground
  service and a hotword engine; it's the obvious next feature.
- **WhatsApp voice notes are refused**, not attempted. The record button needs
  a press-and-hold on a view with no accessibility node, and an unreliable
  implementation would occasionally send half a second of silence to someone.
- **WiFi and Bluetooth can't be toggled** — Android removed that from apps in
  version 10. Jarvis opens the settings panel instead.
- **The Guardian shares a process with the assistant.** File-level protection
  and the git audit trail are the real boundary, and for a personal LAN setup
  that is proportionate. Splitting them into separate processes would be
  stricter.
