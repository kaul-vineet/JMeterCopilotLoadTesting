# GRUNTMASTER 6000 — Modes Design

## 1. Two Modes

### Concurrent
N users run simultaneously in a loop. Ramp up to N, sustain for `run_time`, ramp down naturally.

### Pipeline
N users each run utterances once and exit. No looping. No ramp-down. Test ends when last user exits.

---

## 2. Parameter Design

| Parameter | Concurrent | Pipeline |
|---|---|---|
| **Mode** | Concurrent | Pipeline |
| **Users** | concurrent target | total users to run once |
| **Spawn rate** | controls ramp 0→N | controls initial 0→N spawn speed |
| **Run time** | plateau duration only | — |
| **Think time** | ✓ | ✓ |
| **Reply timeout** | ✓ | ✓ |
| **Notes** | ✓ | ✓ |

### Removed fields
- **Max run time (safety cap)** — removed entirely. No cap in either mode. Tests end naturally.

### Users label per mode
- Concurrent: `Concurrent users`
- Pipeline: `Total users`

---

## 3. Behavioral Design

| Behavior | Concurrent | Pipeline |
|---|---|---|
| Looping | Yes — same conversation, reset idx=0 | No — StopUser after last utterance |
| New conversation on loop | No — same conversation kept | N/A |
| Ramp up | 0→N at spawn rate | 0→N at spawn rate |
| Ramp down | Users finish current cycle, exit naturally (flag-based) | — no ramp-down, users exit when done |
| Ramp up/down rate | Same rate (spawn rate) — mirror image | N/A |
| Phase tracking | RAMP_UP → AT_PEAK → RAMP_DOWN | None |
| AT_PEAK trigger | spawning_complete fires | N/A |
| RAMP_DOWN trigger | `now - _plateau_start >= run_time` | N/A |
| Test ends when | RAMP_DOWN + user_count == 0 | spawning_complete AND user_count == 0 |
| Bridge | Removed | Removed |
| Killing users | Never | Never |
| Safety cap / deadline | None | None |

### Ramp-down detail (Concurrent)
- When `now - _plateau_start >= run_time`: set phase = RAMP_DOWN
- Set `runner.start(user_count=0)` — tells Locust to stop spawning replacements
- Users complete their current utterance cycle, check ramp-down flag at loop point, exit via StopUser
- Locust does not respawn because target is already 0
- User count drops at same rate as spawn rate (natural stagger from staggered spawn times)

### Pipeline exit detail
- After `spawning_complete` fires: call `runner.stop()` — tells Locust to stop spawning
- Users in flight continue to completion, call StopUser, exit naturally
- Exit condition: `spawning_complete AND user_count == 0`

---

## 4. Adversarial Analysis

Every failure raised during design review, with resolution.

| # | Failure | Verdict | Resolution |
|---|---------|---------|------------|
| A1 | `run_time` deadline breaks — plateau gets cut short | Implementation | AT_PEAK→RAMP_DOWN uses `now - _plateau_start >= run_time`, not deadline |
| A2 | Gap when user loops (WS close/reopen) | Eliminated | Keep same conversation — no close/reopen, no gap |
| A3 | Transport hang on loop | Eliminated | No transport close/reopen in same-conversation loop |
| A4 | Killing users mid-message during ramp-down | Not a case | Bot handles disconnects normally; also kills removed entirely |
| A5 | Phase code kills Pipeline users (ramp-down kill logic) | Eliminated | No kill logic in either mode |
| A6 | `_open_conversation()` HTTP hang — Pipeline never ends | Implementation | Add connection timeout to HTTP token call |
| A7 | Spawn bar broken for Pipeline | Implementation | Mode-aware dashboard rendering |
| A8 | Ramp-down is a cliff | Not a case | Staggered spawns → staggered exits at same spawn rate |
| A9 | Safety cap semantics break | Not a case | Cap removed entirely — no cap in either mode |
| A10 | Phase labels wrong in Pipeline | Implementation | Disable phase tracking for Pipeline |
| A11 | Locust respawns exiting users — test never ends | Implementation | `runner.stop()` after spawning_complete (Pipeline); `runner.start(user_count=0)` on RAMP_DOWN (Concurrent) |
| A12 | Token expiry silent failure on long Concurrent runs | Implementation | Periodic token refresh or detect auth errors and reconnect |
| A13 | Consecutive timeout reconnect delays ramp-down exit | Implementation | Skip reconnect during RAMP_DOWN, raise StopUser instead |

---

## 5. Implementation Steps

Each step is independent. Adversarial check is run after each before moving to next.

---

### Step 1 — Params screen: mode selector + field changes

**What changes:**
- Mode row becomes selectable: `Concurrent` | `Pipeline`
- When Pipeline selected: hide `Run time` row
- `Users` label changes per mode: `Concurrent users` vs `Total users`
- Remove `Max run time (safety cap)` row entirely
- `_params` dict carries `mode`, `users`, `spawn_rate`, `run_time` (0 if Pipeline), `think_time`, `reply_timeout`, `notes`

**Adversarial check:**
- [ ] Pipeline params screen shows no Run time row
- [ ] Concurrent params screen shows Run time row
- [ ] Cap row gone from both
- [ ] last_run.json saves/restores mode correctly
- [ ] Switching mode redraws screen with correct fields

---

### Step 2 — User loop logic: Concurrent loops, Pipeline exits

**What changes:**
- In `task()`: after last utterance, check `_params["mode"]`
  - Concurrent: reset `self._idx = 0`, continue (same conversation)
  - Pipeline: raise `StopUser`
- Remove `StopUser` from the normal utterance completion path for Concurrent

**Adversarial check:**
- [ ] Concurrent user never calls StopUser during normal operation
- [ ] Concurrent user_count stays at N after all users complete first cycle
- [ ] Pipeline user calls StopUser after last utterance
- [ ] Pipeline user_count drops as users finish

---

### Step 3 — Remove bridge code

**What changes:**
- Remove bridge decrement from `on_stop`
- Remove `_users_dispatcher` manipulation entirely
- `on_stop` only: close transport, deregister from spawn registry (if still needed)

**Adversarial check:**
- [ ] `on_stop` no longer touches `_users_dispatcher`
- [ ] No import or reference to bridge logic remains
- [ ] Concurrent users still loop correctly without bridge

---

### Step 4 — Phase tracking: Concurrent only

**What changes:**
- Wrap all phase transition logic in `if _params["mode"] == "Concurrent"`
- AT_PEAK trigger: `spawning_complete` fires → phase = AT_PEAK, record `_plateau_start = now`
- RAMP_DOWN trigger: `now - _plateau_start >= _params["run_time"]` → phase = RAMP_DOWN
- On RAMP_DOWN: call `runner.start(user_count=0, spawn_rate=_ramp_rate_ps)` — stop replacements
- Pipeline: phase stays as a neutral display value (e.g. `RUNNING`)

**Adversarial check:**
- [ ] Concurrent: AT_PEAK fires when all N spawned
- [ ] Concurrent: RAMP_DOWN fires exactly `run_time` seconds after AT_PEAK
- [ ] Concurrent: `runner.start(user_count=0)` called on RAMP_DOWN entry
- [ ] Pipeline: no phase transitions fire
- [ ] Pipeline: dashboard shows `RUNNING` not `RAMP_UP`/`AT_PEAK`/`RAMP_DOWN`

---

### Step 5 — Ramp-down: flag check in loop

**What changes:**
- In `task()` Concurrent loop point: `if _run_state.phase == "RAMP_DOWN": raise StopUser`
- This replaces all kill logic
- Remove `_kill_oldest_greenlets()`
- Remove `_spawn_registry` and `_spawn_lock` (if no longer needed)
- Remove `_rampdown_peak`, `_rampdown_start` variables

**Adversarial check:**
- [ ] User exits cleanly at end of current cycle when phase == RAMP_DOWN
- [ ] User_count drops at spawn rate naturally (staggered exits)
- [ ] No user is ever killed mid-utterance
- [ ] `_kill_oldest_greenlets` is gone
- [ ] Test exits when user_count == 0 after RAMP_DOWN

---

### Step 6 — Pipeline: runner.stop() after spawning_complete

**What changes:**
- In `spawning_complete` event handler: `if _params["mode"] == "Pipeline": runner.stop()`
- `runner.stop()` sets Locust target to 0 — no more replacements spawned
- Users in flight continue to their natural exit
- Exit condition: `spawning_complete AND user_count == 0`

**Adversarial check:**
- [ ] `runner.stop()` called exactly once in Pipeline mode
- [ ] Users in flight at time of `runner.stop()` continue to completion
- [ ] No new users spawned after `runner.stop()`
- [ ] Test exits when last user calls StopUser and user_count hits 0

---

### Step 7 — Connection timeout for `_open_conversation()`

**What changes:**
- Add `timeout=` to the HTTP token request in `_open_conversation()`
- If timeout fires: raise `StopUser` in Pipeline, reconnect in Concurrent
- Prevents test hanging forever if token endpoint is unreachable

**Adversarial check:**
- [ ] Hanging token request is interrupted after timeout
- [ ] Pipeline raises StopUser on connection timeout
- [ ] Concurrent reconnects (or raises StopUser after max retries)
- [ ] Timeout value is sensible (e.g. 30s)

---

### Step 8 — Token expiry handling (Concurrent long runs)

**What changes:**
- Detect auth errors from WebSocket responses (not just timeouts)
- On auth error: increment `_consecutive_timeouts` same as a timeout
- When `_consecutive_timeouts >= MAX`: call `_open_conversation()` (fresh token)
- During RAMP_DOWN: skip reconnect, raise StopUser

**Adversarial check:**
- [ ] Auth error increments consecutive counter
- [ ] Reconnect fetches fresh DirectLine token
- [ ] RAMP_DOWN skips reconnect and exits
- [ ] Normal timeouts still handled as before

---

### Step 9 — Dashboard: mode-aware display

**What changes:**
- Spawn bar: Concurrent shows users spawned vs target; Pipeline shows users active vs total
- Phase label: Pipeline shows `RUNNING` / `FINISHED`; Concurrent shows existing phases
- Footer: hide `Run time` for Pipeline; hide phase labels for Pipeline
- `_DashboardState(target_users=...)`: Concurrent = `users`; Pipeline = `users`(same key, same value — no change needed here)

**Adversarial check:**
- [ ] Pipeline dashboard never shows RAMP_UP / AT_PEAK / RAMP_DOWN
- [ ] Concurrent dashboard shows all phases correctly
- [ ] Run time shown in footer for Concurrent only
- [ ] Spawn bar makes sense in both modes

---

## 6. Issues Faced Today (Cross-check)

| Issue | Root cause | Fixed in step |
|-------|-----------|---------------|
| FINISHING never changed to FINISHED | Phase check used time-based heuristic, not `_run_state.phase` | Pre-existing fix |
| LIVE header never changed | Same root cause | Pre-existing fix |
| Post-run menu bypassed by buffered keypresses | Stdin buffer not flushed after Live display | Pre-existing fix |
| Post-run menu went to Starting test instead of staying | `os.system("cls")` ran for all paths including rerun | Pre-existing fix |
| `_reuse_params` NameError | Old variable name not updated to `_next` pattern | Pre-existing fix |
| Ramp-down never fired | `_rampdown_peak` used `_params["users"]` (total cycles) instead of actual concurrent | Pre-existing fix |
| `int()` killed users too early | Truncation vs ceil — `int(10-0.04)=9` kills on first tick | Pre-existing fix |
| Bridge ran during RAMP_DOWN | Missing early return in `on_stop` for RAMP_DOWN phase | Pre-existing fix (now bridge removed entirely) |
| Setup wizard banner lingered on exit | No `cls` before `sys.exit()` or `return` in `run_wizard()` | Pre-existing fix |
| Credential check shown every run | No silent fast-path when all credentials present | Pre-existing fix |

---

## 7. What Is NOT Changing

- Profile loading and assignment logic
- Utterance CSV loading
- DirectLine WebSocket transport
- Response measurement (`send_and_measure`)
- CSV writer and report generation
- Preflight bot check
- Credentials screen (`_screen1`)
- HTML report
