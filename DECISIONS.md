# Decisions Log — v3 FROZEN

Every locked decision with rationale. Architecture is frozen after this document. Any further change requires a demonstrated implementation problem, not a preference.

---

## Architectural principles (non-negotiable)

| # | Decision | Rationale |
|---|---|---|
| AR1 | Agents reason, deterministic tools calculate | No LLM number is ever ground truth. |
| AR2 | Every candidate plan passes the Constraint Engine before commit | Hard constraints never violated; validation is separate from generation. |
| AR3 | Planner is a LangGraph state machine, not sequential LLM calls | Stateful, tool-driven is the core claim. |
| AR4 | Structured decision traces authoritative; LLM reasoning secondary | Enables late-v1 eval dashboard without schema migration. |
| AR5 | System architected so deterministic degraded mode is possible | Full degraded-mode planner ships late-v1; router flag day 1. |
| AR6 | No silent hallucination — unknown data is surfaced | Stale menu, missing macros, unknown dish → HITL or safe fallback. |
| AR7 | Idempotent by design | Every event-driven op has an explicit idempotency key. Retries never duplicate. |
| AR8 | Meal-planning, not medical | Nutrition estimation only. No disease-treatment logic. |

## Product scope

| # | Decision | Rationale |
|---|---|---|
| P1 | LNMIIT students only for v1 | Own data source, own campus, real user access. |
| P2 | User modes: fitness OR general, picked at onboarding | Two planners in one system with a mode flag. |
| P3 | Veg/non-veg = per-day toggle | Matches real behavior. |
| P4 | Budget = soft cap + optional hard cap | Soft = scored, hard = Constraint Engine rejects. |
| P5 | 4 meals/day | Matches mess schedule. |
| P6 | Multi-tenant from day 1 with RLS | Retrofitting auth is painful. |

## Data sources

| # | Decision | Rationale |
|---|---|---|
| D1 | Mess menu = admin PDF upload for v1 | PDF mailed at cycle start. Gmail auto-sync deferred. |
| D2 | Nearby food = campus canteen only, manually seeded | No Zomato/Swiggy — no clean API. |
| D3 | Macro DB seeded manually (~150 mess + ~50 canteen) | Indian mess-food data online is garbage. Project's moat. |
| D4 | Every macro record has normalized serving unit + grams | "One plate" without grams is unusable. |
| D5 | LLM-estimated macros land `verified=false` and stay uncertain | Usable, but never silently trusted. |
| D6 | Every menu cycle stamped with source + freshness + content_hash | Planner detects stale menu; content_hash enables idempotent ingestion. |
| D7 | Supplements declared at onboarding, editable later | User can log new supplement mid-cycle. |

## Agents

| # | Decision | Rationale |
|---|---|---|
| A1 | 3 real agents: Planner, Menu Intelligence, Profile & Learning | Nutrition, Memory, Confidence, Canteen are tools not agents. |
| A2 | Canteen search = tool | Lookup + filter, no autonomy. |
| A3 | Planner state = single Pydantic contract (`PlannerState`) | State is the interface between LangGraph nodes. |
| A4 | Planner generates 2-4 candidates | Real planning loop, not "ask LLM for a plan". |
| A5 | Constraint Engine sole authority on validity | Deterministic Python, unit-tested. |
| A6 | Constraint Engine explicitly distinguishes invalid vs valid-but-suboptimal | Prevents wasted optimization on impossible plans. |
| A7 | Infeasibility triggers fallback or HITL, not more optimization attempts | If no valid plan exists under hard constraints, accept it. |
| A8 | `MAX_CANDIDATES` and `MAX_REVISIONS` are hard bounds, env-configurable | Planner cannot loop indefinitely. |
| A9 | Meal log commits transactionally BEFORE Planner run | Failed planning never corrupts meal history. |
| A10 | `meal_logs` append-only, replan touches only future meals | Historical record is immutable. |
| A11 | Planner replans on every meal_log event | Max responsiveness. |
| A12 | Fallback triggered by full plan infeasibility, not just protein deficit | Honest failure signal. |
| A13 | Canteen options pass through the same Constraint Engine | No two-tier validity. |
| A14 | Practicality (per-dish serving cap) is a hard constraint | Prevents "8 bowls of curd". |
| A15 | Learning Agent runs weekly, proposes only | Prevents drift from noisy daily data. |
| A16 | Cold start = default plan from profile only | No calibration mode in v1. |

## Preferences & memory

| # | Decision | Rationale |
|---|---|---|
| M1 | Three preference kinds: explicit, observed, inferred | Different authority, different lifecycle. |
| M2 | Inferred preferences stay `proposed` until user approves via HITL | Never silent promotion. |
| M3 | Behavioral memory advisory only | Never overrides explicit settings, allergies, hard restrictions. |
| M4 | Explicit preferences NEVER auto-expire | Only user action retires them. |
| M5 | Observed/inferred facts retire after 2 contradictions OR 30 days unconfirmed | Behavioral facts stay fresh. |
| M6 | Explicit → explicit contradiction = supersession, not deletion | Old row `retired`, `superseded_by=<new>`. History preserved. |
| M7 | Allergies and hard restrictions require explicit UI action to change | LLM interpretation of chat can never modify them. |

## Constraint Engine

| # | Decision | Rationale |
|---|---|---|
| C1 | Hard constraint violation → INVALID → rejected, not scored | Clear boundary. |
| C2 | Missing macros perfectly matched → VALID BUT SUBOPTIMAL → scored lower | Common case, not a failure. |
| C3 | Soft scoring uses signed `macro_delta`, not clamped remaining | Over-consumption penalizes the score. |
| C4 | `macro_confidence_penalty` reduces scores of candidates leaning on `verified=false` macros | Uncertainty propagates. |

## Nutrition state

| # | Decision | Rationale |
|---|---|---|
| N1 | `consumed_macros` = summation of meal_logs, never mutated | Derived state. |
| N2 | `macro_delta = target - consumed` is SIGNED | Preserves over-consumption information. |
| N3 | `planning_remaining_macros = max(macro_delta, 0)` component-wise | Used only for planning next meals. |
| N4 | Both quantities coexist in state | System can plan correctly AND report over-consumption. |

## Confidence

| # | Decision | Rationale |
|---|---|---|
| CF1 | Confidence is system-computed by deterministic Python | LLM cannot self-authorize confidence. |
| CF2 | Inputs: menu freshness, macro verification, validation, candidate agreement, fallback state, tool health, LLM signal | Multi-factor, auditable. |
| CF3 | LLM uncertainty signal is advisory input only | Not authoritative. |
| CF4 | Confidence below env-configurable threshold → HITL | User is asked, not silently overridden. |

## Idempotency

| # | Decision | Rationale |
|---|---|---|
| I1 | Morning cron key = `(user_id, date, trigger='morning_cron')` | Duplicate cron runs return existing result. |
| I2 | Meal log key = client-generated UUID `event_id` | Duplicate submissions from Streamlit deduped. |
| I3 | Planner run key = `(user_id, date, trigger, triggering_event_id)` | Retries return existing plan. |
| I4 | Menu ingestion key = `(content_hash, effective_from, source)` | Same PDF cannot become multiple cycles. |
| I5 | Weekly learning key = `(user_id, iso_week)` | One learning run per user per week. |
| I6 | HITL response key = `(hitl_request_id, response_hash)` | Duplicate approvals deduped. |
| I7 | All idempotency enforced by Postgres UNIQUE constraints | DB is the last line of defense. |

## Transactionality

| # | Decision | Rationale |
|---|---|---|
| TX1 | Meal log commits BEFORE Planner run is scheduled | Failed planning never corrupts meal_logs. |
| TX2 | Plan lifecycle transitions use if_version guards | Concurrent replans don't stomp. |
| TX3 | Distinguish received, processed, committed | Only committed = done. |

## Plan lifecycle

| # | Decision | Rationale |
|---|---|---|
| PL1 | States: draft → validated → sent → completed (or → superseded) | Explicit, auditable. |
| PL2 | Only validated plans can transition to sent | User never sees unvalidated plans. |
| PL3 | Replan supersedes old plan; snapshot preserved via `supersedes_id` | Full evolution auditable. |
| PL4 | `meal_logs` authoritative for what happened; `daily_plans` authoritative for what was planned | Distinct records, distinct authority. |
| PL5 | Meals already consumed under old plan remain unchanged in meal_logs on replan | Replan affects only future meals. |

## UX

| # | Decision | Rationale |
|---|---|---|
| U1 | Web app for logging, not WhatsApp/Telegram | No API approval, phone browser works. |
| U2 | Streamlit frontend | Standard for AI/ML demos. |
| U3 | Streamlit mobile-vs-desktop split = decided later | Not blocking v1. |
| U4 | Logging = 3 buttons per meal + optional note | Fast, clean. |

## Tech stack

| # | Decision | Rationale |
|---|---|---|
| T1 | Backend = Python + FastAPI | User's strength. |
| T2 | DB = Supabase (Postgres + Auth + RLS) | Free tier, RLS enforces multi-tenancy at DB layer. |
| T3 | Agent framework = LangGraph | 2026 industry standard for stateful multi-agent. |
| T4 | LLM = env-configured primary + fallback | No hardcoded model IDs. |
| T5 | Start Gemini Flash (primary) + Groq Llama 3.1 8B (fallback) | Both free tier. |
| T6 | Hosting = Railway | Free tier, Postgres addon, cron built-in. |
| T7 | PDF parsing = pdfplumber + LLM structured extraction | Deterministic where possible. |
| T8 | Every LLM call routed through `llm_router.py` | No direct SDK calls elsewhere. |

## Observability

| # | Decision | Rationale |
|---|---|---|
| O1 | `agent_runs`, `tool_calls`, `decision_traces` tables in core v1 | Data schema must exist day 1. |
| O2 | Traces are structured JSONB, not free text | Enables automated metrics. |
| O3 | Every important decision has a `reason_code` | Enables aggregation by decision type. |
| O4 | LLM reasoning stored as optional secondary field only | Not the authoritative trace. |
| O5 | Full eval dashboard, cost analytics, baseline comparison → late-v1 | Data captured throughout; dashboards deferred. |

## Auth & privacy

| # | Decision | Rationale |
|---|---|---|
| PR1 | Supabase Auth + RLS on every user-scoped table | DB-layer enforcement. |
| PR2 | Separate admin role with own RLS policies | Menu/macro writes admin-only. |
| PR3 | LLM calls send only minimum required context | Not raw profile PII. |
| PR4 | All secrets via env vars | Nothing checked into repo. |
| PR5 | Access log for admin actions | Menu uploads, macro verification tracked. |

## Safety boundary

| # | Decision | Rationale |
|---|---|---|
| S1 | System is meal-planning, not medical | No disease-treatment logic. |
| S2 | Fixed list of supported dietary modes and common allergies | Bounded, predictable. |
| S3 | Free-text "other conditions" stored, flagged unsupported, Planner conservative | No invented specialized recommendations. |
| S4 | Medical-condition input triggers UI banner directing to professional guidance | Clear scope communication. |
| S5 | Deterministic guards in nutrition module reject obviously unsafe planning inputs | Rules, not a new agent. |
| S6 | No medical-safety agent, no diagnostic reasoning | Scope stays contained. |

## Late-v1 (design for, ship later)

| # | Item | Why deferred |
|---|---|---|
| L1 | Full evaluation dashboard | Data captured day 1; UI is late-v1. |
| L2 | Baseline comparison (rule-based vs LLM-only vs full agentic) | Needs full system stable first. |
| L3 | Deterministic degraded-mode planner | Router flag ships day 1; alternate planner late. |
| L4 | Advanced failure/degraded-mode handling | Handle failures as they surface; systematize late-v1. |
| L5 | Cost analytics dashboard | `cost_inr` captured day 1; dashboard late. |
| L6 | Weekly qualitative rubric review UI | Data ready; UI late. |

## Out of scope for v1 (do not build)

- Photo → macro estimation
- Zomato / Swiggy integration
- WhatsApp / Telegram bots
- Crowdsourced menu additions
- Multi-college support
- Nutrition coach chat
- Weekly/monthly analytics reports
- Medical-safety subsystem
- New agents beyond the three defined

## Open decisions (decide when hit)

- Streamlit mobile UX polish (post-M4)
- Cold-start calibration mode (revisit after 10 real users)
- Notification channel — email vs in-app only (post-M4)
- Exact `MAX_CANDIDATES`, `MAX_REVISIONS`, confidence threshold values (tune with real data)
- Per-factor weights in `confidence_calc` (tune with real data)
- Practical serving cap values per dish (tune per dish with admin review)
- Timeline / demo deadline

## Interview description rule

**No hypothetical numbers.** No fabricated adherence rates, costs, latencies, or adoption metrics. Numbers get added only after they are measured from real deployments. Description emphasizes architecture and real deployment, not invented performance.

---

## ARCHITECTURE FROZEN

Do not add to this log. Any further architectural change requires a demonstrated implementation problem. Start M1.
