# Operator utilities

Small helpers kept out of the engine tree so the runtime package stays deployable as-is.

| Tool | Purpose |
|---|---|
| `toggle_truman.py [on|off]` | Toggle the v2.1 "Truman reveal" flag on the live node (`truman_reveal_active`) for the next command. Reads `WETWARE_VM` / `WETWARE_SSH_KEY` from `.local/config.env` or `config.example.env`. |
| `dsh_goal_checker.sh` | DSH goal-gate hook: exits non-zero while the node still answers HTTP 502 so the agent cannot close the incident early. Requires `WETWARE_VM` in the environment/config. |

Both scripts are diagnostic/verification aids; they do not modify the story assets or the engine code.
