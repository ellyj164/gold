# Project records

Permanent record of this project's requirements and how it was built. Kept in
the project folder so the history travels with the code.

| File | Contents |
|---|---|
| [REQUIREMENTS.md](REQUIREMENTS.md) | Every specification given, in the order received: the original 37-section PDF, the Signal Suggestion Module, the 50-pip day-trading rule, and the interface/delivery requirements |
| [DEVELOPMENT-LOG.md](DEVELOPMENT-LOG.md) | Session-by-session record: decisions and their reasoning, every bug found and fixed, calibration findings, and the standing principles for the project |
| `../AI-Powered Gold Trading Decision Assistant.pdf` | The original specification document |

## How to keep this current

When new requirements arrive, add them to `REQUIREMENTS.md` under a new numbered
heading. When work is done on them, add a session entry to `DEVELOPMENT-LOG.md`
covering what changed, what broke, and why each decision was made.

The value of these files is the *reasoning* — the code already shows what was
built. What it cannot show is why the location pillar is one factor instead of
three, or why the packaged windowed build is tested separately from the console
one. That is what belongs here.
