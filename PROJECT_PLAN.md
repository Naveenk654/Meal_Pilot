# Agentic AI Mess & Meal Planning System — LNMIIT

**Version 3 — FROZEN. Ship, don't redesign.**

Any change beyond this document requires a demonstrated implementation problem, not a "wouldn't it be nice" impulse.

---

## 1. One-line pitch

A stateful multi-agent LangGraph system with confidence-gated HITL that ingests real LNMIIT mess menus, generates and validates daily meal plans through a deterministic Constraint Engine, continuously replans against actual meal logs, learns behavioral patterns with user approval, and falls back to campus canteen when no valid mess-only plan exists.

---

## 2. Core architectural principles (non-negotiable)

1. **Agents reason, tools calculate.** LLMs handle reasoning, planning, tool selection, and decisions under uncertainty. Deterministic Python handles all numerical calculations, macro aggregation, budget math, and hard-constraint validation. No LLM-generated number is ever ground truth.
2. **Validate before commit.** Every candidate plan passes through the Constraint Engine before it can become a real plan. Hard constraints are never violated; soft objectives are optimized.
3. **Stateful, not scripted.** The Planner is a LangGraph state machine that inspects state, chooses tools, revises, falls back, or requests HITL. It is not an if/else ladder wrapped around one LLM call.
4. **Observable by construction.** Every agent action, tool call, validation result, and decision is logged as structured data — not raw reasoning text — from day 1.
5. **Deterministic degraded mode is possible.** The system is architected so that when the LLM is unavailable, verified menu and macro data can still produce a valid plan. Full degraded-mode handler ships in late-v1; the router flag and fallback wiring ship day 1.
6. **No hallucinated data.** Unknown dishes, stale menus, and missing macros are surfaced explicitly. The system never silently guesses.
7. **Idempotent by design.** Every event-driven operation has an explicit idempotency key. Retries never create duplicate plans, duplicate consumption, or duplicate state transitions.
8. **Meal-planning, not medical.** The system is a nutrition-estimation and meal-planning tool. It does not provide medical or disease-treatment guidance and directs users to professional guidance when conditions fall outside supported scope.

---

## 3. System architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     STREAMLIT FRONTEND                       │
│  (student logging UI + admin dashboard + eval console)       │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP (auth-scoped)
┌──────────────────────────▼──────────────────────────────────┐
│                    FASTAPI BACKEND                           │
│                                                              │
│  ┌────────────────┐  ┌──────────────┐  ┌──────────────────┐│
│  │  Planner Agent │  │Menu Intel    │  │ Profile&Learning ││
│  │  (LangGraph)   │  │Agent(on-PDF) │  │ Agent (weekly)   ││
│  └───────┬────────┘  └──────┬───────┘  └─────────┬────────┘│
│          │                  │                     │         │
│  ┌───────▼──────────────────▼─────────────────────▼───────┐│
│  │                    TOOL LAYER                          ││
│  │  Deterministic:                                        ││
│  │  • Nutrition Calculator   • Constraint & Validation    ││
│  │  • Macro Aggregator       • Budget Calculator          ││
│  │  • Confidence Calculator  • Menu Reader                ││
│  │  • Canteen Search                                      ││
│  │  Stateful:                                             ││
│  │  • User Memory   • HITL Prompter   • LLM Router        ││
│  │  Observability:                                        ││
│  │  • Decision Trace Writer  • Tool Call Logger           ││
│  └────────────────────────────┬───────────────────────────┘│
└───────────────────────────────┼─────────────────────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │   SUPABASE (POSTGRES)   │
                    │   RLS-enforced per user │
                    └─────────────────────────┘
```

Deployment: Railway (FastAPI + Streamlit + cron), Supabase (DB + Auth + RLS).

---

## 4. Tech stack (locked)

| Layer | Choice |
|---|---|
| Backend | Python 3.11 + FastAPI |
| Frontend | Streamlit |
| DB | Supabase (Postgres + Auth + Row-Level Security) |
| Agent orchestration | LangGraph (stateful graph, not sequential chain) |
| LLM primary | Configurable via env — start with Gemini Flash (free) |
| LLM fallback | Configurable via env — start with Groq Llama 3.1 8B |
| LLM router | Handles rate limits, timeouts, model unavailability |
| PDF parsing | pdfplumber + LLM structured extraction |
| Scheduling | Railway cron |
| Auth | Supabase Auth (email magic link), RLS on every table |
| Observability | `agent_runs` + `tool_calls` + `decision_traces` (Postgres) |

No model ID hardcoded in application code.

---

## 5. Planner State

The Planner is a LangGraph state machine. The state is the contract.

```python
class PlannerState(TypedDict):
    # Identity
    user_id: str
    date: date
    trigger: Literal["morning_cron", "meal_log", "override", "skip", "menu_update"]
    triggering_event_id: str | None   # for idempotency

    # Context
    profile: UserProfile
    behavioral_memory: list[BehavioralFact]
    todays_menu: DailyMenu | None     # None = menu uncertain
    menu_freshness: MenuFreshness     # source, ingested_at, effective_range, version
    veg_today: bool
    budget_remaining_inr: float

    # Nutrition state (deterministic)
    target_macros: Macros
    consumed_macros: Macros           # append-only summation of meal_logs
    macro_delta: MacrosSigned         # target - consumed (SIGNED; negatives = over)
    planning_remaining_macros: Macros # max(macro_delta, 0), used for planning only
    meals_completed: list[MealSlot]
    meals_remaining: list[MealSlot]

    # Planning workspace
    candidate_plans: list[CandidatePlan]
    validation_results: list[ValidationResult]
    revision_attempts: int            # bounded by config
    candidates_generated: int         # bounded by config
    fallback_invoked: bool
    fallback_results: list[CanteenOption]
    infeasible: bool                  # true when no valid mess-only plan exists

    # Decision surface
    selected_plan: Plan | None
    confidence: float                 # system-computed, see §16
    confidence_factors: ConfidenceBreakdown
    hitl_status: Literal["not_needed", "pending", "approved", "rejected"]
    hitl_request_id: str | None

    # Output
    final_plan: Plan | None
    reasoning_trace: list[TraceEvent] # structured, not free text
```

**Invariants:**
- `consumed_macros` is derived from `meal_logs` and never mutated in place.
- `macro_delta` is SIGNED. Over-consumption remains visible for reporting and soft-scoring.
- `planning_remaining_macros = max(macro_delta, 0)` component-wise. Used only to decide what to plan next.
- `todays_menu = None` triggers menu-uncertainty handling, not silent fallback to yesterday.
- Any transition to `final_plan` requires a passing `ValidationResult`.
- `candidates_generated` and `revision_attempts` are hard-bounded by config; exceeding → fallback or HITL, never infinite loops.

---

## 6. The 3 agents

### 6.1 Menu Intelligence Agent

**Trigger:** New PDF uploaded OR admin forwards a menu-change email.

**Idempotency key:** `(pdf_content_hash, declared_effective_from, source)`. The same PDF cannot be ingested repeatedly as separate cycles.

**Steps:**
1. Extract text + tables from PDF (pdfplumber, deterministic).
2. LLM structured extraction → JSON of `{day, meal, dishes[]}` with per-dish confidence.
3. For each new dish not in `macro_db` → LLM estimates macros with confidence. Stored `verified=false`.
4. Confidence < 0.7 → HITL: flagged for admin review before going live.
5. Diff against current cycle → write only changes, preserve verified macros.
6. Every cycle stamped with `source`, `ingested_at`, `effective_from`, `effective_to`, `version`, `content_hash`.
7. Emit event: `menu_updated`.

**Tools:** `pdf_parse`, `llm_extract_structured`, `macro_estimate`, `menu_diff`, `admin_notify`, `trace_write`.

### 6.2 Planner Agent (LangGraph)

**Trigger:** Morning cron per user + any state-changing user event (log, override, skip) + menu update.

**Idempotency key:** `(user_id, date, trigger, triggering_event_id)`. Retries return the existing result.

**Nodes (LangGraph):**
```
  load_state → check_menu_freshness → compute_macro_state
       │
       ▼
  generate_candidates ──► validate_candidates ──► classify_outcome
       ▲                                                │
       │                                                ▼
  (revise, if within limit) ◄──── any_valid? ────────┬──► score_and_select
                                                     │           │
                                              no + limit hit     ▼
                                                     │      check_confidence
                                                     ▼           │
                                            invoke_fallback      ▼
                                                  │        confidence_ok? ──yes──► commit_plan
                                                  ▼              │
                                          validate_fallback     no
                                                  │              ▼
                                                  └──────► request_hitl ──► await_response ──► commit_plan
```

**Behavior:**
- **Morning:** all meals in `meals_remaining`. Generate 2-4 candidates. Validate each. Select the highest-scoring valid candidate.
- **Mid-day (on meal_log):** the log is committed transactionally first; then a new Planner run reads updated `consumed_macros`. Only `meals_remaining` is replanned. `meal_logs` history is never touched.
- **Fallback:** invoked when the mess-only problem is infeasible OR when the best valid mess-only plan fails the confidence check for reasons that a canteen option could improve.
- **Practicality:** Constraint Engine rejects plans with unrealistic serving quantities (per-dish cap in `macro_db`). Practicality is a hard bound.
- **Bounded loops:** `MAX_CANDIDATES` and `MAX_REVISIONS` are env-configurable. On exhaustion → fallback or HITL. Never infinite.

**Tools:** `read_menu`, `read_memory`, `nutrition_calc`, `constraint_validate`, `canteen_search`, `hitl_prompt`, `llm_plan`, `confidence_calc`, `trace_write`.

### 6.3 Profile & Learning Agent

**Trigger:** Weekly cron (Sunday 10 PM per user).

**Idempotency key:** `(user_id, iso_week)`. Only one learning run per user per week.

**Steps:**
1. Pull last 7 days of `meal_logs`, `overrides`, `skips`.
2. LLM detects patterns → returns structured `ProposedFact` objects with evidence references.
3. Every proposed fact tagged `inferred` and stored `status='proposed'`.
4. HITL: weekly review card → user approves/rejects/edits each proposed fact.
5. Approved facts written with `source='learning'`, `status='active'`.
6. **Retirement rules (see §7 for full policy):** only observed and inferred facts may auto-retire. Explicit facts never auto-retire.
7. **Behavioral memory is advisory only.** It influences candidate scoring but cannot override explicit profile settings, allergies, hard restrictions, or the current day's veg toggle.

**Tools:** `read_logs`, `pattern_detect`, `memory_propose`, `hitl_weekly_review`, `memory_write`, `trace_write`.

---

## 7. Preference model & retirement policy

Three preference kinds, distinct authority, distinct lifecycle.

| Kind | Source | Authority | Auto-retirement |
|---|---|---|---|
| **Explicit** | Onboarding, direct user statement in UI | Hard (allergies, restrictions) OR strong soft (likes/dislikes) | **Never** |
| **Observed** | Meal log patterns | Advisory ranking signal | Retired after 2 contradictions OR `last_confirmed_at > 30 days` |
| **Inferred** | LLM pattern detection | None until user approves via HITL | Same as observed, once active |

**Supersession (explicit → explicit):**
- When the user makes a new explicit statement that contradicts an older explicit one, the older row is set `status='retired'`, `retired_at=now()`, `superseded_by=<new_row_id>`. The new row is inserted `status='active'`.
- History is preserved. Nothing is deleted.

**Hard rules that never auto-retire and never yield to inference:**
- Allergies (`kind='allergy'`, `authority='hard'`)
- Dietary restrictions (`kind='restriction'`, `authority='hard'`)
- Must be changed only by explicit user action in the UI. An LLM interpretation of a chat message can never retire or modify them.

**Advisory-only rule:**
- Observed and inferred facts influence candidate scoring only. They cannot override any explicit preference or hard constraint at any time.

---

## 8. Constraint & Validation Engine

**Deterministic Python. No LLM. Sole authority on plan validity.**

**Hard constraints (violation → plan is INVALID and rejected):**
- Allergy match against any dish ingredient
- Dietary restriction (veg on veg-day, no eggs if declared, etc.)
- Dish not available in today's menu / canteen hours
- Absolute budget ceiling if user set one
- Impossible meal selection (e.g. dish scheduled for lunch used at breakfast)
- Practical serving limit exceeded (per-dish `practical_max_servings_per_day`)

**Soft objectives (scored, do NOT invalidate a plan):**
- Calorie deviation from target (uses signed `macro_delta`, penalizes over- and under-consumption)
- Protein deviation from target (same)
- Carb / fat deviation (same)
- Preference match (observed + explicit likes/dislikes)
- Variety vs recent days
- Convenience (fewer supplements = higher score)
- Cost under soft budget
- Macro-source-quality penalty when a candidate relies heavily on `verified=false` macros

**Explicit distinction:**
- A plan that violates a hard constraint is **INVALID**. Rejected. Not scored.
- A plan that satisfies all hard constraints but cannot perfectly hit macros is **VALID BUT SUBOPTIMAL**. It gets a lower `total_soft_score` but remains a legitimate candidate.
- If, after `MAX_CANDIDATES` generations and `MAX_REVISIONS` revisions, no valid plan exists under hard constraints → Planner sets `infeasible=true` and moves to fallback or HITL. It does not keep trying to optimize an impossible problem.

**Output:**
```python
class ValidationResult:
    is_valid: bool
    hard_violations: list[HardViolation]   # empty if valid
    macro_deviation: MacroDeviationSigned  # signed, from macro_delta
    budget_deviation: float
    preference_score: float
    variety_score: float
    practicality_score: float
    macro_confidence_penalty: float        # penalizes reliance on unverified macros
    total_soft_score: float                # weighted sum
```

The Planner selects the highest `total_soft_score` among valid candidates.

---

## 9. Macro-verification rule (strict)

- LLM-estimated macros are stored `verified=false`.
- Unverified macros MAY be used in planning, but they carry uncertainty.
- Uncertainty propagates:
  - `ValidationResult.macro_confidence_penalty` lowers the soft score of candidates that lean on unverified macros.
  - `confidence_calc` (§16) reduces overall Planner confidence when unverified macros materially affect the final plan.
  - If Planner confidence falls below threshold because of unverified reliance → HITL is triggered.
- Verification paths: admin review in UI, IFCT match, or two consistent independent estimates. Only then does `verified` flip to `true`.
- Unverified macros are never silently treated as trusted ground truth.

---

## 10. Nutrition math (deterministic, no LLM, ever)

- **BMR:** Mifflin-St Jeor
- **TDEE:** BMR × {1.2 sedentary, 1.375 light, 1.55 gym 3-4x, 1.725 gym 5-6x, 1.9 athlete}
- **Protein target:** general = 0.8 g/kg; fitness/maintain = 1.6 g/kg; fitness/bulk = 2.0 g/kg; fitness/cut = 2.2 g/kg
- **Fats:** 25% of kcal
- **Carbs:** remainder
- **Consumed macros:** sum of `meal_logs.actual_macros_json` for today
- **Macro delta (signed):** `target - consumed`, per macro
- **Planning remaining macros:** `max(macro_delta, 0)` per macro, used only to decide what to plan next
- **Meal-level totals:** sum of dish macros × serving multiplier
- Recompute targets weekly if weight changes > 1 kg

Every one of these is a pure function with unit tests.

---

## 11. Macro DB (normalized servings + verification)

```sql
macro_db (
  id,
  dish_name_normalized,             -- e.g. "aloo_paratha"
  serving_unit,                     -- "1 piece", "1 bowl (150g)", "100g"
  serving_grams,                    -- numeric, deterministic scaling
  kcal, protein_g, carbs_g, fats_g,
  is_veg,
  practical_max_servings_per_day,   -- enforced by Constraint Engine
  source ENUM('manual','ifct','nutritionix','llm_estimated'),
  confidence FLOAT,
  verified BOOL,                    -- LLM estimates start false
  estimated_at,
  verified_at,
  verified_by
)
```

**Rules:**
- Every dish has a normalized serving unit with grams.
- LLM estimates land as `verified=false`.
- Verification: admin review, IFCT match, or two consistent independent estimates.
- Seed ~150 mess dishes + ~50 canteen dishes manually before launch.

---

## 12. Menu freshness

Every `menu_cycles` row carries:
- `source` (pdf, email, manual)
- `content_hash`
- `ingested_at`
- `effective_from`, `effective_to`
- `version`

**Planner checks:**
- Is today within `effective_from..effective_to` of an active cycle? If no → menu uncertain.
- Is there a newer partial update for this week? Apply it.
- If menu is uncertain: Planner does **not** silently reuse yesterday. It uses a safe fallback (canteen + verified staples) or requests HITL confirmation.

---

## 13. Plan lifecycle

Every plan progresses through explicit states, and the transitions are auditable.

```
draft ──► validated ──► sent ──► completed
                          │
                          ▼
                      superseded  (when replanned mid-day)
```

- `draft`: freshly generated candidate, not yet validated.
- `validated`: passed Constraint Engine. Not yet shown to user.
- `sent`: shown to user, active for the remainder of the day (or until superseded).
- `completed`: end of day reached without further changes.
- `superseded`: replaced by a newer plan mid-day. Snapshot preserved. `supersedes_id` links the successor.

**Rules:**
- Only `validated` → `sent` transitions are user-visible.
- On replan, previous `sent` plan → `superseded`; new plan → `draft` → `validated` → `sent`.
- `meal_logs` is the authoritative record of what actually happened. Never rewritten by a replan.
- `daily_plans` is the authoritative record of what was planned. Every version is retained.
- A user who consumed a meal from an old plan keeps that log unchanged; the replan only affects future meals.
- The complete evolution of a day is inspectable by walking `supersedes_id` links.

---

## 14. Idempotency & transactional writes

**Idempotency keys (enforced as Postgres unique constraints):**

| Operation | Key | Behavior on retry |
|---|---|---|
| Morning cron run | `(user_id, date, trigger='morning_cron')` | Return existing `agent_run`; no new plan |
| Meal log submission | client-generated UUID `event_id` | Return existing log; no duplicate consumption |
| Planner run | `(user_id, date, trigger, triggering_event_id)` | Return existing `agent_run`; no new plan |
| Menu ingestion | `(content_hash, declared_effective_from, source)` | Return existing cycle; no duplicate cycle |
| Weekly learning | `(user_id, iso_week)` | Return existing run; no duplicate proposals |
| HITL response | `(hitl_request_id, response_hash)` | Return existing response |

**Transactional writes:**

- **Meal log + Planner trigger:** the `meal_logs` insert commits transactionally BEFORE the Planner run is scheduled. A failed Planner run never corrupts `meal_logs`.
- **Plan lifecycle transitions:** state transitions on `daily_plans` are single atomic updates with `if_version` guards. Concurrent replans do not stomp each other.
- **Distinction:** an event may be *received* (webhook returned 200), *successfully processed* (business logic ran), and *successfully committed* (DB write flushed). Only the third counts as "done."

---

## 15. Observability (core v1 — dashboards come later)

```sql
agent_runs (
  id, idempotency_key UNIQUE,
  user_id, agent, trigger, triggering_event_id,
  planner_state_snapshot JSONB,
  final_outcome JSONB,
  confidence, confidence_factors JSONB,
  hitl_status,
  degraded_mode BOOL,
  started_at, ended_at, latency_ms,
  total_tokens, total_cost_inr
)

tool_calls (
  id, agent_run_id, tool_name,
  input_snapshot JSONB, output_snapshot JSONB,
  status ENUM('ok','error','timeout'),
  latency_ms, tokens_used, cost_inr,
  called_at
)

decision_traces (
  id, agent_run_id, step_name,
  candidates_considered JSONB,
  validation_results JSONB,
  chosen_option JSONB,
  reason_code TEXT,               -- structured, authoritative
  llm_reasoning TEXT NULL,        -- optional, secondary
  created_at
)
```

Structured decision data is the authoritative audit trail. LLM reasoning is secondary and optional.

---

## 16. Confidence calculation (system-computed)

Confidence is deterministic Python. The LLM may report an uncertainty signal, but the final number is calculated by the system.

Inputs to `confidence_calc`:
- **Menu freshness:** current cycle, current-week update, or uncertain
- **Macro verification coverage:** fraction of the plan's macros that are `verified=true`
- **Hard-constraint validation:** valid plan vs infeasible (infeasibility zeros mess-only confidence)
- **Candidate agreement:** did multiple candidates converge on similar choices?
- **Fallback uncertainty:** did we fall back? How far outside soft-budget?
- **Tool failures during the run:** any degraded reads, retries, timeouts?
- **LLM uncertainty signal:** advisory input, not authoritative

Output:
```python
class ConfidenceBreakdown:
    overall: float                      # 0..1
    menu_freshness_factor: float
    macro_verification_factor: float
    validation_factor: float
    candidate_agreement_factor: float
    fallback_factor: float
    tool_health_factor: float
    llm_uncertainty_signal: float | None
```

`overall` below a configurable threshold → HITL is triggered. Threshold and per-factor weights are configurable via env.

---

## 17. Data model (Postgres, RLS-enforced)

```sql
users (id, email, created_at, timezone, mode ENUM('fitness','general'), role ENUM('student','admin'))

user_profile (
  user_id PK, age, gender, height_cm, weight_kg,
  activity_level, goal ENUM('cut','maintain','bulk'),
  veg_default BOOL,
  budget_soft_inr, budget_hard_inr NULL,
  bmr, tdee,
  target_kcal, target_protein_g, target_carbs_g, target_fats_g,
  updated_at
)

user_preferences (
  id, user_id,
  kind ENUM('allergy','dislike','like','supplement','restriction'),
  value TEXT,
  source ENUM('onboarding','learning','manual'),
  authority ENUM('hard','soft'),
  confidence FLOAT,
  status ENUM('active','retired'),
  superseded_by BIGINT REFERENCES user_preferences(id) NULL,
  first_seen, last_confirmed_at, retired_at NULL
)

user_memory_behavioral (
  id, user_id, fact TEXT,
  evidence JSONB,                       -- log ids, counts
  status ENUM('proposed','active','retired'),
  first_seen, last_confirmed_at, retired_at NULL,
  contradiction_count INT DEFAULT 0
)

menu_cycles (
  id, effective_from, effective_to,
  source, version, content_hash,
  ingested_at, pdf_url,
  UNIQUE (content_hash, effective_from, source)
)

menu_items (
  id, cycle_id, day_of_week,
  meal ENUM('breakfast','lunch','snack','dinner'),
  dish_name, is_veg, macro_id, confidence FLOAT
)

macro_db ( … see §11 … )

canteen_items (
  id, shop_name, dish, price_inr, is_veg,
  macro_id, available_hours,
  practical_max_servings_per_day
)

daily_plans (
  id, user_id, date,
  plan_json, target_macros_json,
  validation_result_json,
  confidence, confidence_factors JSONB,
  degraded_mode BOOL,
  status ENUM('draft','validated','sent','completed','superseded'),
  supersedes_id BIGINT REFERENCES daily_plans(id) NULL,
  created_at, sent_at, completed_at NULL, superseded_at NULL
)

meal_logs (
  id, user_id, date, meal,
  event_id UUID UNIQUE,                 -- client-generated idempotency key
  action ENUM('ate_planned','ate_different','skipped'),
  actual_dishes_json, actual_macros_json,
  note TEXT, logged_at
)
-- Append-only. Never updated in place.

hitl_requests (
  id, user_id, agent, question,
  options_json, context_json,
  status ENUM('pending','approved','rejected','expired','edited'),
  response_json, responded_at
)

agent_runs, tool_calls, decision_traces ( … see §15 … )
```

**RLS:** every user-scoped table filters by `user_id = auth.uid()`. Admin routes go through a separate role check. Menu, macro, canteen tables are read-only for students, write for admins.

---

## 18. HITL surfaces

| # | When | Where | Response |
|---|---|---|---|
| 1 | Morning plan generated | App | Accept / modify / regenerate |
| 2 | Fallback proposed | Inline in plan | Confirm / reject |
| 3 | Menu uncertainty (stale/missing) | Inline banner | Confirm today's menu / skip planning |
| 4 | System confidence below threshold | Inline | User picks between options |
| 5 | Weekly learning review | Sunday 10 PM | Approve / reject / edit per proposed fact |
| 6 | New menu parsed (admin) | Admin dashboard | Approve / edit before publish |
| 7 | New macro estimate (admin) | Admin dashboard | Verify / adjust / reject |

---

## 19. LLM router (v1)

- Env-configured primary + fallback model IDs
- Retries with exponential backoff on transient errors
- Model-unavailable → try fallback
- Both unavailable → mark run `degraded_mode=true`, use verified-data path
- Every LLM call goes through the router. No direct SDK calls elsewhere.
- Only sends the minimum context needed for each operation (privacy hygiene)

Full degraded-mode planner (verified staples + canteen only) ships in late-v1. The router flag and fallback wiring ship day 1.

---

## 20. Auth, privacy, and safety boundary

**Auth & privacy (core v1):**
- Supabase Auth, email magic link
- Row-Level Security on every user-scoped table
- Admin role is a separate `users.role` value with its own RLS policies
- All secrets in env vars. Nothing checked into repo
- Every LLM call sends only the fields it needs (planner sends menu + targets + preferences, not raw profile PII)
- Access log for admin actions (menu upload, macro verification)

**Safety boundary (core v1, no new subsystem):**
- The system is a meal-planning and nutrition-estimation tool. It does not provide medical or disease-treatment guidance.
- Onboarding supports a fixed list of dietary modes (veg/non-veg/eggetarian) and common allergies (nuts, dairy, gluten, soy, shellfish).
- A free-text "other conditions" field is stored but explicitly flagged as unsupported. Planner sees it and stays conservative.
- Any user input naming a medical condition or restriction outside supported scope → the UI shows: "This tool is for meal planning, not medical guidance. Please consult a doctor or dietitian for [condition]." Planner does not invent specialized recommendations.
- Deterministic guards reject obviously unsafe planning inputs (e.g. extreme calorie targets below a hard floor, extreme weight-loss rates). These are rules in the nutrition module, not a separate agent.
- No medical-safety agent, no disease-treatment logic, no diagnostic reasoning.

---

## 21. Onboarding flow

1. Sign up (Supabase magic link)
2. Profile: age, gender, height, weight, activity level, goal
3. Mode: fitness / general
4. Diet: veg default, allergies (fixed list), dislikes, dietary restrictions
5. Supplements owned
6. Budget: soft cap ₹/day, optional hard cap
7. Calibration screen: computed BMR/TDEE/targets → user can tweak
8. First plan generated as "practice" for day 1

---

## 22. Milestones (implementation order — FROZEN)

**M1 — Foundations & deployment (wk 1-2)**
Repo, FastAPI, Supabase with RLS, auth, onboarding form, deterministic nutrition module with unit tests, empty Streamlit shell, Railway deploy, env-configured LLM router (fallback wiring, no degraded-mode planner yet), `agent_runs` + `tool_calls` + `decision_traces` schema and writers, idempotency-key infrastructure.

**M2 — Menu intelligence & macro DB (wk 2-3)**
Admin PDF upload with content-hash idempotency, Menu Intelligence Agent, macro DB with normalized servings and `practical_max_servings_per_day`, manual seed of ~150 mess + ~50 canteen dishes, verification workflow, admin approval flow, menu freshness tracking.

**M3 — Planner Agent + Constraint Engine (wk 3-5)**
LangGraph Planner with full state, bounded candidate/revision loops, Constraint & Validation Engine (deterministic), invalid-vs-suboptimal distinction, infeasibility handling, system-computed confidence, plan lifecycle states, transactional plan writes. Mess-only, no fallback yet, no memory yet. Plan card in Streamlit.

**M4 — Meal logging & continuous replanning (wk 5-6)**
Three-button meal logging with client-generated `event_id`, append-only `meal_logs`, transactional log-then-plan sequence, mid-day replan with `superseded` lifecycle, signed macro_delta reporting, macro tracker UI.

**End of M4 = end-to-end core loop works:**
onboarding → today's menu → deterministic macro calc → stateful Planner → candidate generation → Constraint Engine → validated plan → meal logging → remaining-macro recalc → mid-day replan.

**M5 — Canteen fallback + HITL refinement (wk 6-7)**
Canteen DB, fallback tool, feasibility-based fallback trigger, Constraint Engine validates canteen options, all HITL surfaces 1-4 & 6-7 wired.

**M6 — Behavioral learning & memory (wk 7-8)**
Learning Agent weekly job, proposed/active/retired lifecycle, supersession on explicit contradictions, HITL surface 5, memory-as-advisory in candidate scoring.

**M7 — Real users (wk 8-10)**
Ship to hostel friends first, iterate weekly on feedback, harden failure cases as they surface.

**M8 (late-v1) — Evaluation, baseline, degraded mode, dashboards (wk 10-12+)**
Full eval dashboard reading from the traces logged all along. Baseline comparison (rule-based vs LLM-only vs full agentic). Degraded-mode planner. Cost analytics. Qualitative rubric for weekly hand-review.

Each milestone produces a working, deployed increment.

---

## 23. Repo structure

```
mess-agent/
├── backend/
│   ├── main.py
│   ├── agents/
│   │   ├── planner_graph.py       # LangGraph definition
│   │   ├── planner_nodes.py       # Node implementations
│   │   ├── menu_intel.py
│   │   └── learning.py
│   ├── tools/
│   │   ├── nutrition.py           # Deterministic, unit-tested
│   │   ├── constraint_engine.py   # Deterministic, unit-tested
│   │   ├── confidence.py          # Deterministic, unit-tested
│   │   ├── menu.py
│   │   ├── memory.py
│   │   ├── canteen.py
│   │   ├── hitl.py
│   │   ├── llm_router.py
│   │   └── trace.py               # decision_traces / tool_calls writers
│   ├── models/                    # Pydantic schemas incl. PlannerState
│   ├── db/                        # Supabase client, migrations, RLS policies
│   ├── auth/
│   ├── idempotency/               # Key generation + retry-safe writers
│   ├── crons/
│   │   ├── morning_plan.py
│   │   └── weekly_learning.py
│   └── eval/                      # Stubs in v1, filled in late-v1
├── frontend/
│   ├── streamlit_app.py           # Student UI
│   └── admin_app.py               # Admin + eval console
├── data/
│   ├── mess_macros_seed.json
│   └── canteen_seed.json
├── tests/
│   ├── test_nutrition.py
│   ├── test_constraint_engine.py
│   ├── test_confidence.py
│   ├── test_planner_graph.py
│   └── test_idempotency.py
├── .env.example
├── railway.toml
└── README.md
```

---

## 24. Out of scope for v1 (locked, do not add)

Photo→macro, Zomato/Swiggy, WhatsApp/Telegram bots, crowdsourcing, multi-college support, nutrition chat, monthly analytics reports, medical-safety subsystem, new agents beyond the three defined.

---

## 25. Interview description (no invented numbers)

> Stateful multi-agent LangGraph system for LNMIIT mess meal planning. Real PDF menu ingestion, verified macro database with normalized servings, deterministic nutrition and Constraint & Validation Engine, tool-driven Planner with bounded candidate generation → validation → selection → commit loop, system-computed confidence with confidence-gated HITL, continuous mid-day replanning against append-only meal logs with full plan-lifecycle history, behavioral learning with explicit user approval, campus-canteen fallback, and idempotent transactional writes. Full structured audit trail of every agent run, tool call, and decision. Deployed to real LNMIIT students.

Numbers are added only after they are measured from real deployments.

---

## ARCHITECTURE FROZEN

Do not extend this document. Next action: start M1.
