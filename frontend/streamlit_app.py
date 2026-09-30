"""Streamlit shell for M1 (§21 onboarding).

Auth: Supabase email OTP. User enters email → receives 6-digit code → pastes it back.
Post-auth: 7-step onboarding form (single page), calibration screen showing computed targets.

Talks only to the FastAPI backend for domain writes. Uses supabase-py only for auth.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
import streamlit as st
from dotenv import load_dotenv
from supabase import create_client

# Streamlit doesn't auto-load .env — do it here so local runs "just work".
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# --- Config ------------------------------------------------------------------

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
# Dev-only: free-tier Supabase can't customize email templates to include the OTP
# code, so we allow the service-role key to bypass email verification for local
# smoke testing. NEVER ship a build with SUPABASE_SERVICE_ROLE_KEY exposed to a
# browser — this is dev-mode only, and Streamlit runs server-side so the key
# never crosses the wire.
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
APP_ENV = os.environ.get("APP_ENV", "dev")
# Where Supabase redirects after a magic-link click. This URL must be on
# Supabase's Redirect URLs allowlist. It points at the FastAPI backend's
# /auth/callback route (not Streamlit directly) because Streamlit's iframe
# sandbox blocks reading URL fragments — the FastAPI page does the
# fragment→query-param handoff, then forwards to Streamlit.
APP_URL = os.environ.get("APP_URL", f"{BACKEND_URL}/auth/callback")


# --- Cached backend GETs ----------------------------------------------------
# Streamlit reruns the entire script on every widget interaction and every
# `with tabs[i]:` block runs regardless of which tab is active. Without
# caching, a single button click fires 10+ backend GETs. `_cached_get` gives
# every read-only endpoint a 30s memoization window keyed by path+token so
# clicks feel instant. Any mutation (POST/PUT) calls `_bust_cache()` so the
# next rerun refetches fresh data.


@dataclass
class _CachedResp:
    """Minimal stand-in for httpx.Response so call sites don't have to change
    shape. Exposes `status_code`, `text`, and `.json()` — the only members
    the rest of the module reads from GET responses."""

    status_code: int
    text: str

    def json(self):
        return json.loads(self.text) if self.text else None


@st.cache_data(ttl=30, show_spinner=False)
def _cached_get_raw(path: str, token: str) -> tuple[int, str]:
    r = httpx.get(
        f"{BACKEND_URL}{path}",
        headers={"Authorization": f"Bearer {token}"} if token else {},
        timeout=30,
    )
    return r.status_code, r.text


def _cached_get(path: str, token: str) -> _CachedResp:
    return _CachedResp(*_cached_get_raw(path, token))


def _bust_cache() -> None:
    """Clear all cached read responses. Call before `st.rerun()` after any
    write so the fresh render sees post-mutation state."""
    _cached_get_raw.clear()
    try:
        _admin_get_cached.clear()   # defined further down
    except NameError:
        pass


def _run_with_status(label: str, stages: list[str], fn, *args, **kwargs):
    """Run `fn(*args, **kwargs)` in a background thread while updating an
    `st.status` container with staged messages so the UI feels alive.

    The default `st.spinner` icon is small and static — users perceive it as
    hung on 3–8 s LLM calls. `st.status` with a label that changes every
    ~1.2 s reads as visible progress instead.

    Stages advance on a timer, not on real backend progress (Streamlit can't
    hook into httpx mid-request without SSE/WebSockets, which the backend
    doesn't expose yet). Advancement stops as soon as the worker thread
    completes so we don't linger on stale messages.
    """
    import threading
    import time

    result_box: dict = {}

    def _worker() -> None:
        try:
            result_box["value"] = fn(*args, **kwargs)
        except Exception as exc:
            result_box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    with st.status(label, expanded=True) as status:
        for stage in stages:
            if not thread.is_alive():
                break
            status.update(label=stage)
            # Poll instead of one long sleep so we exit promptly when the
            # thread finishes mid-stage.
            waited = 0.0
            while waited < 1.2 and thread.is_alive():
                time.sleep(0.1)
                waited += 0.1
        thread.join()
        if "error" in result_box:
            status.update(label=f"Failed: {result_box['error']}", state="error")
            raise result_box["error"]
        status.update(label="Done", state="complete", expanded=False)
    return result_box["value"]


def _require_config() -> None:
    missing = [
        name
        for name, value in [
            ("SUPABASE_URL", SUPABASE_URL),
            ("SUPABASE_ANON_KEY", SUPABASE_ANON_KEY),
        ]
        if not value
    ]
    if missing:
        st.error(
            "Missing environment variables: " + ", ".join(missing) + ". "
            "Set them in .env and restart Streamlit."
        )
        st.stop()


def _get_client():
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


# --- Auth --------------------------------------------------------------------


def _render_auth_screen() -> None:
    st.title("mess-agent · sign in")
    st.caption("LNMIIT-only for v1")

    email = st.text_input("Email", key="auth_email")
    _render_magic_link_flow(email)


def _render_magic_link_flow(email: str) -> None:
    """Send a Supabase magic link and let the user click through the email.
    On return, the browser lands on APP_URL with the session in the URL
    fragment; `_handle_magic_link_callback` consumes it."""
    if st.button("Send magic link", type="primary"):
        if not email:
            st.warning("Enter your email first.")
            return
        try:
            client = _get_client()
            client.auth.sign_in_with_otp(
                {"email": email, "options": {"email_redirect_to": APP_URL}}
            )
        except Exception as exc:
            st.error(f"Send failed: {exc}")
            return
        st.success(
            f"Sent! Check **{email}** and click the login link. "
            "It'll redirect you back here signed in."
        )
        st.info(
            "📬 **Not in your inbox?** Check your **Spam / Junk** folder — "
            "the link may land there the first time. Mark it as *Not spam* "
            "so future sign-ins arrive normally."
        )


def _handle_magic_link_callback() -> None:
    """Two jobs, in priority order:

    1. First-time sign-in: user just clicked the magic link. FastAPI's
       /auth/callback redirected here with `?at=…&rt=…`. We consume `at`,
       stash it in session_state, mirror the auth.users row, then move `rt`
       (the refresh_token) into the URL under `?rt=…` and clean `at` out so
       it doesn't leak in bookmarks / history.
    2. Page refresh with a live session in the URL: session_state was
       wiped by Streamlit reloading, but `?rt=…` is still there. We exchange
       it for a fresh access_token via `refresh_session`. Supabase rotates
       the refresh_token on every call — we write the new one back to the
       URL. Result: refresh survives F5 without leaking a long-lived
       access token.

    Only `rt` (refresh token) is ever persisted in the URL — never `at`
    (access token) beyond the first hop. Refresh tokens are still sensitive
    but shorter-lived once rotated, and Streamlit has no better place to
    stash session state that survives reloads."""
    qp = st.query_params
    at = qp.get("at")
    rt = qp.get("rt")

    if st.session_state.get("access_token"):
        return   # already authenticated this session, nothing to do

    # Path 1 — first-time consumption from the callback bridge.
    if at:
        try:
            client = _get_client()
            client.auth.set_session(at, rt or "")
            user_resp = client.auth.get_user(at)
            email = user_resp.user.email if user_resp and user_resp.user else None
            if not email:
                raise RuntimeError("magic-link session had no user email")

            # Mirror auth.users → public.users so downstream FK-dependent
            # tables accept this user_id. Onboarding does this too, but a
            # user may hit /plans/today before onboarding on first login.
            if SUPABASE_SERVICE_ROLE_KEY:
                from supabase import create_client as _create

                svc = _create(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
                svc.table("users").upsert(
                    {"id": user_resp.user.id, "email": email, "mode": "general"},
                    on_conflict="id",
                ).execute()

            st.session_state.access_token = at
            st.session_state.user_email = email
        except Exception as exc:
            st.error(f"Magic-link sign-in failed: {exc}")
            st.query_params.clear()
            return

        # Strip `at` from URL, keep `rt` for future refreshes.
        st.query_params.clear()
        if rt:
            st.query_params["rt"] = rt
        st.rerun()
        return

    # Path 2 — page refresh with only `rt` in the URL. Exchange for a
    # fresh access token via Supabase's refresh flow.
    if rt:
        try:
            client = _get_client()
            # Some supabase-py versions require the client to have a session
            # loaded before `refresh_session()` will accept a positional rt.
            # Priming the client with a blank access token + the rt handles
            # both API variants without a version check.
            try:
                client.auth.set_session("", rt)
            except Exception:
                pass
            refreshed = client.auth.refresh_session(rt)
            session = getattr(refreshed, "session", None) or refreshed
            new_at = getattr(session, "access_token", None)
            new_rt = getattr(session, "refresh_token", None)
            user_obj = getattr(session, "user", None)
            email = getattr(user_obj, "email", None) if user_obj else None
            if not new_at or not email:
                raise RuntimeError(
                    f"refresh_session returned no session (at={bool(new_at)}, email={bool(email)})"
                )
            st.session_state.access_token = new_at
            st.session_state.user_email = email
            # Supabase rotates the refresh_token; persist the new one so the
            # next reload works too.
            st.query_params["rt"] = new_rt or rt
            st.rerun()
        except Exception as exc:
            # Refresh failed (rt expired, revoked, Supabase down, or SDK
            # mismatch). Surface it once so the user can tell us WHY instead
            # of silently bouncing to the sign-in screen. Then clear the URL
            # so retries don't loop on the same broken token.
            st.warning(
                f"Session refresh failed — please sign in again. "
                f"(details: {type(exc).__name__}: {exc})"
            )
            st.query_params.clear()


# --- Onboarding form --------------------------------------------------------


def _load_existing_profile() -> dict:
    """GET /onboarding/me. Returns {} if the user hasn't onboarded yet."""
    token = st.session_state.get("access_token")
    if not token:
        return {}
    try:
        resp = _cached_get("/onboarding/me", token)
    except httpx.HTTPError:
        return {}
    if resp.status_code >= 400:
        return {}
    return resp.json() or {}


def _render_onboarding_form() -> None:
    st.title("Onboarding")
    st.caption(f"Signed in as {st.session_state.get('user_email')}")

    prev = _load_existing_profile()
    if prev:
        st.info("Editing your stored profile — values below are what we have on file.")

    gender_opts = ["male", "female", "other"]
    activity_opts = ["sedentary", "light", "gym_3_4", "gym_5_6", "athlete"]
    goal_opts = ["cut", "maintain", "bulk"]
    mode_opts = ["fitness", "general"]

    with st.form("onboarding_form"):
        st.subheader("Profile")
        c1, c2 = st.columns(2)
        with c1:
            age = st.number_input("Age", 10, 100, value=int(prev.get("age", 22)))
            height_cm = st.number_input(
                "Height (cm)", 100.0, 250.0, value=float(prev.get("height_cm", 175.0))
            )
            weight_kg = st.number_input(
                "Weight (kg)", 30.0, 250.0, value=float(prev.get("weight_kg", 70.0))
            )
        with c2:
            gender = st.selectbox(
                "Gender", gender_opts,
                index=gender_opts.index(prev.get("gender", "male"))
                if prev.get("gender") in gender_opts else 0,
            )
            activity_level = st.selectbox(
                "Activity", activity_opts,
                index=activity_opts.index(prev.get("activity_level", "gym_3_4"))
                if prev.get("activity_level") in activity_opts else 2,
            )
            goal = st.selectbox(
                "Goal", goal_opts,
                index=goal_opts.index(prev.get("goal", "maintain"))
                if prev.get("goal") in goal_opts else 1,
            )

        st.subheader("Mode")
        mode = st.radio(
            "Planning mode", mode_opts, horizontal=True,
            index=mode_opts.index(prev.get("mode", "fitness"))
            if prev.get("mode") in mode_opts else 0,
        )

        st.subheader("Diet")
        veg_default = st.checkbox(
            "Vegetarian by default", value=bool(prev.get("veg_default", True))
        )
        allergies_raw = st.text_input(
            "Allergies (comma-separated)",
            help="Supported: nuts, dairy, gluten, soy, shellfish",
        )
        dislikes_raw = st.text_input("Dislikes (comma-separated)")
        restrictions_raw = st.text_input("Other dietary restrictions (comma-separated)")

        st.subheader("Supplements")
        supplements_raw = st.text_input(
            "Supplements you own (comma-separated)",
            help="Recognized: whey, casein, creatine, bcaa, multivitamin, fish_oil, mass_gainer, peanut_butter",
        )

        st.subheader("Budget")
        c3, c4 = st.columns(2)
        with c3:
            budget_soft_inr = st.number_input(
                "Soft cap ₹/day", 0.0, 5000.0,
                value=float(prev.get("budget_soft_inr", 200.0)),
            )
        with c4:
            budget_hard_inr_str = st.text_input(
                "Hard cap ₹/day (optional)",
                value=str(prev.get("budget_hard_inr", "") or ""),
            )

        submitted = st.form_submit_button("Compute targets" if not prev else "Update targets")

    if not submitted:
        return

    payload = {
        "age": int(age),
        "gender": gender,
        "height_cm": float(height_cm),
        "weight_kg": float(weight_kg),
        "activity_level": activity_level,
        "goal": goal,
        "mode": mode,
        "veg_default": bool(veg_default),
        "allergies": _split_csv(allergies_raw),
        "dislikes": _split_csv(dislikes_raw),
        "restrictions": _split_csv(restrictions_raw),
        "supplements": _split_csv(supplements_raw),
        "budget_soft_inr": float(budget_soft_inr),
        "budget_hard_inr": float(budget_hard_inr_str) if budget_hard_inr_str.strip() else None,
    }
    _submit_onboarding(payload)


def _split_csv(raw: str) -> list[str]:
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _submit_onboarding(payload: dict) -> None:
    token = st.session_state.access_token
    try:
        resp = httpx.post(
            f"{BACKEND_URL}/onboarding",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        st.error(f"Backend unreachable: {exc}")
        return
    if resp.status_code != 200:
        st.error(f"Backend returned {resp.status_code}: {resp.text}")
        return

    st.success("Profile saved. Here are your computed targets — you can adjust weekly.")
    result = resp.json()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Calories", f"{result['target_kcal']:.0f} kcal")
    c2.metric("Protein", f"{result['target_protein_g']:.0f} g")
    c3.metric("Carbs", f"{result['target_carbs_g']:.0f} g")
    c4.metric("Fats", f"{result['target_fats_g']:.0f} g")
    st.caption(f"BMR {result['bmr']:.0f} · TDEE {result['tdee']:.0f}")


# --- Main --------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="mess-agent", page_icon=None)
    _require_config()

    # First: if the browser just returned from a Supabase magic-link click,
    # the URL has session tokens (either in fragment on the first paint, or
    # in ?at/?rt query params on the follow-up reload). Consume them before
    # rendering anything else so the sign-in feels instant.
    if "access_token" not in st.session_state:
        _handle_magic_link_callback()

    if "access_token" not in st.session_state:
        _render_auth_screen()
        return

    with st.sidebar:
        st.write(f"Signed in as **{st.session_state.get('user_email')}**")
        _render_sidebar_weight_log()
        if st.button("Sign out"):
            # Best-effort server-side revoke so the refresh_token that just
            # got left in the URL can't be reused. Failure here still
            # completes the client-side sign-out below.
            try:
                _get_client().auth.sign_out()
            except Exception:
                pass
            for k in ("access_token", "user_email"):
                st.session_state.pop(k, None)
            _bust_cache()
            st.query_params.clear()
            st.rerun()

    # Detect admin role — sourced from public.users.role via /me/profile-like
    # endpoint. We reuse /hitl/pending to piggyback role info… simpler: just
    # fetch users row via a fresh anon-scoped Supabase call using stored JWT.
    is_admin = _fetch_role_is_admin()
    tabs_labels = ["Today's plan", "History", "Preferences", "Onboarding"]
    if is_admin:
        tabs_labels.append("Admin")
    tabs = st.tabs(tabs_labels)
    with tabs[0]:
        _render_today_plan()
    with tabs[1]:
        _render_history()
    with tabs[2]:
        _render_preferences_editor()
    with tabs[3]:
        _render_onboarding_form()
    if is_admin:
        with tabs[4]:
            _render_admin_console()


# --- Today's plan (M3) ------------------------------------------------------


def _render_menu_source_line(plan: dict) -> None:
    """Persistent trust signal: show the user which menu upload their plan is
    built from and when the mess admin uploaded it. If the timestamp is
    missing (pre-migration plan row), fall back to a neutral message so we
    don't render a broken line."""
    from datetime import datetime, timezone

    ingested = plan.get("plan_cycle_ingested_at")
    source = plan.get("plan_cycle_source") or "menu"
    if not ingested:
        st.caption("📋 _Plan menu source unknown — refresh to stamp the current menu._")
        return
    try:
        ts = datetime.fromisoformat(ingested.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        secs = int(delta.total_seconds())
        if secs < 60:
            ago = "just now"
        elif secs < 3600:
            ago = f"{secs // 60} min ago"
        elif secs < 86400:
            ago = f"{secs // 3600}h ago"
        else:
            ago = f"{secs // 86400}d ago"
        pretty = ts.astimezone().strftime("%b %d, %H:%M")
    except (ValueError, TypeError):
        ago = "recently"
        pretty = str(ingested)[:16]
    st.caption(f"📋 Based on **{source}** menu uploaded **{ago}** _(at {pretty})_")


def _render_today_plan() -> None:
    st.subheader("Today's plan")
    token = st.session_state.access_token

    # First-run gate: the Planner needs a `user_profile` row to compute
    # targets and validate plans. Without it, /planner/run 500s with an
    # opaque "profile not found" — worse for a fresh user. Redirect them
    # to Onboarding instead of showing the generate button.
    profile = _load_existing_profile()
    if not profile:
        st.info(
            "👋 Welcome! Fill out the **Onboarding** tab first so we can "
            "compute your macro targets. Come back here after saving to "
            "generate your first plan."
        )
        return

    col_l, col_m, col_r = st.columns([1, 1, 2])
    force = col_r.checkbox("Force fresh run (ignore prior)", value=False)
    if col_m.button("Override plan", help="Reject the current plan and force a fresh one"):
        try:
            _run_with_status(
                "Overriding your current plan",
                [
                    "Marking the current plan as rejected…",
                    "Loading today's menu and your macros…",
                    "Asking the AI for fresh meal candidates…",
                    "Validating and scoring options…",
                    "Committing your new plan…",
                ],
                lambda: httpx.post(
                    f"{BACKEND_URL}/planner/override",
                    json={"note": "user override"},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=90,
                ),
            )
        except httpx.HTTPError as exc:
            st.error(str(exc))
            return
        _bust_cache()
        st.rerun()
    if col_l.button("Generate / refresh plan", type="primary"):
        try:
            resp = _run_with_status(
                "Building your plan for today",
                [
                    "Loading today's mess menu…",
                    "Reading your profile, preferences, and remaining macros…",
                    "Asking the AI for meal candidates…",
                    "Validating each candidate against constraints…",
                    "Scoring options and picking the best one…",
                    "Committing your plan…",
                ],
                lambda: httpx.post(
                    f"{BACKEND_URL}/planner/run",
                    json={"trigger": "morning_cron", "force": force},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=90,
                ),
            )
        except httpx.HTTPError as exc:
            st.error(f"Backend unreachable: {exc}")
            return
        if resp.status_code >= 400:
            st.error(f"{resp.status_code}: {resp.text}")
        else:
            _bust_cache()
            data = resp.json()
            if data.get("infeasible"):
                st.warning(
                    "No feasible plan today. Most common causes, in order:\n\n"
                    "1. **No active menu for today** — ask admin to upload the "
                    "current week's mess menu, or upload it yourself in the "
                    "Admin tab if you have that role.\n"
                    "2. **All meals already logged** — nothing left to plan.\n"
                    "3. **Mess alone can't hit your protein target on a cut** — "
                    "either switch your goal from 'cut' to 'maintain' in "
                    "Onboarding (target drops from 2.2g/kg to 1.6g/kg), or "
                    "turn on **Mess + Shops (mixed)** in Preferences so the "
                    "planner can top up with canteen items."
                )
            elif not data.get("inserted"):
                st.info("Plan already exists for this trigger (idempotent).")
            else:
                st.success(f"Plan #{data['plan_id']} · confidence {data['confidence']:.2f}")

    try:
        plan_resp = _cached_get("/plans/today", token)
    except httpx.HTTPError as exc:
        st.error(f"Backend unreachable: {exc}")
        return
    if plan_resp.status_code >= 400:
        st.error(f"{plan_resp.status_code}: {plan_resp.text}")
        return
    plan = plan_resp.json()
    logged_meals = _fetch_logged_meals(token)
    if plan is None:
        # No active plan for today. Two flavors:
        #   a) user hasn't generated yet → prompt them
        #   b) all meals logged & prior plan superseded → show tracker + "day done"
        _render_macro_tracker(token)
        if logged_meals:
            st.success(f"You've logged {len(logged_meals)} meal(s) today. No new plan generated — day is set.")
        else:
            st.info(
                "**No plan yet.** Click **Generate / refresh plan** above. If "
                "it says infeasible, the most likely cause is that the mess "
                "hasn't uploaded a menu covering today — ask admin, or upload "
                "one from the Admin tab if you have that role."
            )
        _render_topup_card(token)
        return

    if plan.get("menu_stale"):
        col_msg, col_btn = st.columns([3, 1])
        col_msg.warning(
            "🍽️ Menu was updated after this plan was made. Refresh with the new menu."
        )
        if col_btn.button("Refresh menu", key="refresh-menu", type="primary"):
            try:
                _run_with_status(
                    "Rebuilding plan against updated menu",
                    [
                        "Loading the newly-published menu…",
                        "Comparing against your existing plan…",
                        "Asking the AI for fresh meal candidates…",
                        "Validating picks against your preferences…",
                        "Committing your refreshed plan…",
                    ],
                    lambda: httpx.post(
                        f"{BACKEND_URL}/planner/run",
                        json={"trigger": "menu_update", "force": True},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=90,
                    ),
                )
            except httpx.HTTPError as exc:
                st.error(str(exc))
                return
            _bust_cache()
            st.rerun()

    # Single source of truth: consumed vs target on top. Plan totals get a
    # separate, clearly-labeled "if you follow the plan" summary underneath.
    _render_macro_tracker(token)
    st.divider()

    st.markdown(
        f"#### Plan for today (confidence {plan['confidence']:.2f})"
    )
    _render_menu_source_line(plan)
    st.caption(
        f"If you eat everything on this plan: "
        f"**{plan['total_kcal']:.0f} kcal** · "
        f"{plan['total_protein_g']:.0f}g P · "
        f"{plan['total_carbs_g']:.0f}g C · "
        f"{plan['total_fats_g']:.0f}g F "
        f"(target {plan['target_kcal']:.0f} kcal / {plan['target_protein_g']:.0f}g P)"
    )

    cost = float(plan.get("total_cost_inr") or 0.0)
    soft = plan.get("budget_soft_inr")
    hard = plan.get("budget_hard_inr")
    cost_line = f"**Cost: ₹{cost:.0f}** (mess is free; canteen items priced)"
    if soft is not None:
        cost_line += f" · soft budget ₹{soft:.0f}"
    if hard is not None:
        cost_line += f" · hard budget ₹{hard:.0f}"
    st.caption(cost_line)

    kcal_ratio = plan["total_kcal"] / max(plan["target_kcal"], 1)
    if kcal_ratio > 1.15:
        st.warning(f"Plan would overshoot target by {(kcal_ratio-1)*100:.0f}% if fully eaten.")
    elif kcal_ratio < 0.85:
        st.info(f"Plan undershoots target by {(1-kcal_ratio)*100:.0f}% — you may still be hungry.")
    st.divider()

    meal_order = ["breakfast", "lunch", "snack", "dinner"]
    by_meal: dict[str, list[dict]] = {m: [] for m in meal_order}
    for entry in plan.get("entries", []):
        by_meal.setdefault(entry["meal"], []).append(entry)

    # Surface a single, honest campus-cost total at the top so the user
    # sees at a glance what they'll actually spend today.
    canteen_entries = [e for e in plan.get("entries", []) or [] if e.get("source") == "canteen"]
    total_canteen_cost = sum(float(e.get("price_inr", 0) or 0) for e in canteen_entries)
    if canteen_entries:
        st.markdown(
            f"**🛒 Campus food today: ₹{total_canteen_cost:.0f}** "
            f"across {len(canteen_entries)} canteen item(s)."
        )
    else:
        st.markdown("**🥣 Mess-only plan today — ₹0 out-of-pocket.**")

    logged_slots = {row["meal"] for row in logged_meals}

    for meal in meal_order:
        entries = by_meal.get(meal) or []
        already_logged = meal in logged_slots
        header_note = " ✅ logged" if already_logged else ""
        st.markdown(f"### {meal.title()}{header_note}")
        if not entries and not already_logged:
            st.caption("_no entry planned_")
            continue
        for entry in entries:
            name = entry["dish_ref"].replace("_", " ").title()
            is_canteen = entry.get("source") == "canteen"
            if is_canteen:
                # canteen:<shop>:<dish_norm> → prettier "Dish @ Shop"
                parts = entry["dish_ref"].split(":")
                shop = parts[1].title() if len(parts) > 1 else ""
                dish = parts[2].replace("_", " ").title() if len(parts) > 2 else name
                name = f"{dish} @ {shop}"
            verified_tag = "" if entry["macro_verified"] else " · _unverified_"
            # Money badge: mess = free, canteen = ₹X (prominent green).
            price_val = float(entry.get("price_inr", 0) or 0)
            price_tag = (
                f" · :green[**₹{price_val:.0f}**]" if is_canteen and price_val > 0
                else " · :gray[free]"
            )
            source_tag = " 🛒" if is_canteen else ""
            st.markdown(
                f"- **{name}**{source_tag} — {entry['servings']:.1f} serving"
                f"{'s' if entry['servings'] != 1 else ''} · "
                f"{entry['kcal']:.0f} kcal · {entry['protein_g']:.0f}g P"
                f"{price_tag}{verified_tag}"
            )
        if not already_logged and entries:
            _render_log_buttons(meal, entries, token)

    _render_topup_card(token)


# --- End-of-day top-up card (M5) -------------------------------------------


@st.fragment
def _render_topup_card(token: str) -> None:
    """After the dinner slot is logged, if the day's macros still fall short,
    show canteen suggestions that close the gap. Advisory — not a committed plan.

    Wrapped in `@st.fragment` so ticking a checkbox only re-runs this card
    instead of the whole script (plan card, tracker, and 4+ API calls). Cuts
    the perceived latency of each tick from ~1s to instant.
    """
    try:
        resp = _cached_get("/nutrition/topup-suggestions", token)
    except httpx.HTTPError as exc:
        st.caption(f"_(top-up unavailable: {exc})_")
        return
    if resp.status_code == 404:
        # Endpoint not found — backend probably hasn't been restarted to pick up
        # the new route. Surface it so the user knows to restart rather than
        # silently showing nothing.
        st.caption("_(top-up endpoint not found — restart FastAPI to pick up the new /nutrition/topup-suggestions route)_")
        return
    if resp.status_code >= 400:
        st.caption(f"_(top-up failed: {resp.status_code} {resp.text[:120]})_")
        return
    data = resp.json()
    # Only surface the card once dinner is logged. Pre-dinner, silence.
    if not data.get("dinner_logged"):
        return
    st.divider()
    st.markdown("### 🛒 Fill the gap")
    if not data.get("eligible"):
        st.caption(data.get("reason") or "No top-up needed.")
        return
    st.caption(
        f"You're short **{data['gap_kcal']:.0f} kcal** and **{data['gap_protein_g']:.0f}g protein** "
        f"vs your target. Check what you actually grabbed:"
    )
    suggestions = data.get("suggestions", []) or []
    selected: list[dict] = []
    picked_kcal = 0.0
    picked_protein = 0.0
    picked_cost = 0.0
    checkbox_keys: list[str] = []
    for i, s in enumerate(suggestions):
        # Use dish_ref in the key so checkboxes remain stable across reruns even
        # if the greedy algorithm reorders items between renders.
        key = f"topup-sel-{s['dish_ref']}-{i}"
        checkbox_keys.append(key)
        verified = "" if s.get("macro_verified") else " · _unverified_"
        label = (
            f"**{s['dish_name']}** @ {s['shop_name'].title()} — "
            f"{s['servings']:.1f} serving"
            f"{'s' if s['servings'] != 1 else ''} · "
            f"{s['kcal']:.0f} kcal · {s['protein_g']:.0f}g P · "
            f":green[**₹{s['price_inr']:.0f}**]{verified}"
        )
        if st.checkbox(label, key=key):
            selected.append(s)
            picked_kcal += float(s.get("kcal", 0.0))
            picked_protein += float(s.get("protein_g", 0.0))
            picked_cost += float(s.get("price_inr", 0.0))

    if selected:
        st.caption(
            f"Selected: +{picked_kcal:.0f} kcal · +{picked_protein:.0f}g P · "
            f"₹{picked_cost:.0f} out-of-pocket"
        )
    else:
        st.caption(
            f"If you take all {len(suggestions)}: +{data['total_added_kcal']:.0f} kcal · "
            f"+{data['total_added_protein_g']:.0f}g P · ₹{data['total_cost_inr']:.0f}"
        )

    if st.button(
        "Log selected as eaten",
        key="topup-log-btn",
        type="primary",
        disabled=not selected,
    ):
        _log_topup_selection(selected, checkbox_keys, token)


def _log_topup_selection(
    selected: list[dict], checkbox_keys: list[str], token: str
) -> None:
    """Bundle the checked canteen picks into a single snack meal_log.

    Uses macro_id per item so the backend recomputes macros deterministically
    from macro_db × servings — the totals will match the tracker exactly.

    On success: clear the checkbox session_state keys so the boxes come back
    unchecked, bust the cache, and rerun the whole app (not just this fragment)
    so the macro tracker + plan card reflect the new consumption.
    """
    import uuid as _uuid

    dishes = []
    for s in selected:
        entry: dict = {
            "dish_ref": s["dish_ref"],
            "servings": float(s["servings"]),
        }
        if s.get("macro_id") is not None:
            entry["macro_id"] = int(s["macro_id"])
        else:
            # No macro_id → fall back to client-supplied macros. The backend
            # scales by servings, so we need per-serving numbers here.
            servings = max(float(s["servings"]), 1e-6)
            entry["kcal"] = float(s["kcal"]) / servings
            entry["protein_g"] = float(s["protein_g"]) / servings
            entry["carbs_g"] = float(s["carbs_g"]) / servings
            entry["fats_g"] = float(s["fats_g"]) / servings
        dishes.append(entry)

    payload = {
        "event_id": str(_uuid.uuid4()),
        "meal": "snack",  # canteen add-ons slot into snack — the log allows multiple snack rows/day
        "action": "ate_different",
        "actual_dishes": dishes,
        "note": "canteen top-up",
    }
    ok, msg, warnings = _post_log(payload, token)
    (st.success if ok else st.error)(msg)
    for w in warnings:
        st.warning(w)
    if ok:
        # Reset checkbox state — otherwise Streamlit's session_state keeps
        # the ticks on and the same items appear pre-selected next render.
        for key in checkbox_keys:
            st.session_state.pop(key, None)
        _bust_cache()
        # Full-app rerun so the macro tracker + plan card refresh with the
        # newly-logged snack. Fragment-scoped rerun would leave them stale.
        st.rerun(scope="app")
        st.rerun()


# --- Macro tracker (M4) -----------------------------------------------------


def _render_macro_tracker(token: str) -> None:
    try:
        resp = _cached_get("/nutrition/today", token)
    except httpx.HTTPError:
        return
    if resp.status_code >= 400:
        return
    data = resp.json()
    st.markdown("### Consumed so far today")
    for label, consumed_key, target_key, unit in [
        ("Calories", "consumed_kcal", "target_kcal", "kcal"),
        ("Protein", "consumed_protein_g", "target_protein_g", "g"),
        ("Carbs", "consumed_carbs_g", "target_carbs_g", "g"),
        ("Fats", "consumed_fats_g", "target_fats_g", "g"),
    ]:
        consumed = float(data[consumed_key])
        target = float(data[target_key])
        pct = min(1.0, consumed / target) if target > 0 else 0.0
        st.progress(pct, text=f"{label}: {consumed:.0f}{unit} / {target:.0f}{unit}")

    supp = data.get("supplement_breakdown") or []
    if supp:
        with st.expander(f"Includes {len(supp)} auto-counted supplement(s)"):
            for s in supp:
                st.write(
                    f"- **{s['display_name']}** × {s['servings_per_day']:.0f}/day "
                    f"→ {s['kcal']:.0f} kcal / {s['protein_g']:.0f}g P"
                )


# --- Meal-log form (M4) -----------------------------------------------------


def _render_hitl_pending(token: str) -> None:
    """M5/M6: surface pending HITL requests inline on the Today's plan tab."""
    try:
        resp = _cached_get("/hitl/pending", token)
    except httpx.HTTPError:
        return
    if resp.status_code >= 400:
        return
    reqs = resp.json() or []
    if not reqs:
        return
    st.markdown("### 🙋 Pending review")
    for req in reqs:
        with st.container(border=True):
            st.markdown(f"**{req['surface']}** · {req['question']}")
            # Weekly review — per-fact approve/reject.
            if req["surface"] == "weekly_review" and req.get("options"):
                for opt in req["options"]:
                    mid = opt.get("memory_id")
                    c1, c2, c3 = st.columns([4, 1, 1])
                    c1.write(f"• {opt.get('fact')}  _(conf {opt.get('confidence',0):.2f})_")
                    if c2.button("✓", key=f"mem-a-{mid}"):
                        _decide_memory(mid, "approve", token)
                    if c3.button("✗", key=f"mem-r-{mid}"):
                        _decide_memory(mid, "reject", token)
                # Also let the user dismiss the whole review card.
                if st.button("Done reviewing", key=f"hitl-done-{req['id']}"):
                    _hitl_respond(req["id"], "approved", token)
                continue
            if req.get("options"):
                st.json(req["options"])
            if req.get("context"):
                st.caption(str(req["context"]))
            col_a, col_r = st.columns(2)
            if col_a.button("Approve", key=f"hitl-a-{req['id']}", type="primary"):
                _hitl_respond(req["id"], "approved", token)
            if col_r.button("Reject", key=f"hitl-r-{req['id']}"):
                _hitl_respond(req["id"], "rejected", token)


def _decide_memory(memory_id: int, decision: str, token: str) -> None:
    try:
        resp = httpx.post(
            f"{BACKEND_URL}/learning/memory/decide",
            json={"memory_id": memory_id, "decision": decision},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        st.error(str(exc))
        return
    if resp.status_code >= 400:
        st.error(resp.text)
        return
    _bust_cache()
    st.rerun()


def _hitl_respond(hitl_id: int, status_str: str, token: str) -> None:
    with st.spinner("Recording your response — agent may replan…"):
        try:
            resp = httpx.post(
                f"{BACKEND_URL}/hitl/{hitl_id}/respond",
                json={"status": status_str, "response": {}},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
        except httpx.HTTPError as exc:
            st.error(f"HITL respond failed: {exc}")
            return
    if resp.status_code >= 400:
        st.error(f"{resp.status_code}: {resp.text}")
        return
    st.success(f"Recorded {status_str}")
    _bust_cache()
    st.rerun()


def _fetch_logged_meals(token: str) -> list[dict]:
    try:
        resp = _cached_get("/meal-logs/today", token)
    except httpx.HTTPError:
        return []
    if resp.status_code >= 400:
        return []
    return resp.json() or []


def _post_log(payload: dict, token: str) -> tuple[bool, str, list[str]]:
    # Stages differ by action. `ate_planned` on the golden path returns in
    # ~200 ms so a single stage is enough; skipped / ate_different always run
    # the LangGraph planner (candidate gen → validation → commit), so we walk
    # the user through what the agent is doing.
    is_golden = payload.get("action") == "ate_planned"
    stages = (
        ["Saving your meal…"]
        if is_golden
        else [
            "Saving your meal to the database…",
            "Recalculating your remaining macros for today…",
            "Asking the AI for the best remaining meals…",
            "Validating picks against your preferences and budget…",
            "Committing your updated plan…",
        ]
    )
    label = "Logging meal" if is_golden else "Agent is working on your day"

    def _do_post():
        return httpx.post(
            f"{BACKEND_URL}/meal-logs",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=120,
        )

    try:
        resp = _run_with_status(label, stages, _do_post)
    except httpx.HTTPError as exc:
        return False, str(exc), []
    if resp.status_code >= 400:
        return False, resp.text, []
    data = resp.json()
    msg = f"Log #{data['log_id']} saved"
    if data.get("replan_triggered"):
        msg += f" · replanned (plan #{data.get('replan_plan_id')})"
    return True, msg, data.get("warnings") or []


@st.cache_data(ttl=60)
def _search_macros(token: str, q: str) -> list[dict]:
    try:
        resp = httpx.get(
            f"{BACKEND_URL}/macro-db/search",
            params={"q": q, "limit": 100},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except httpx.HTTPError:
        return []
    if resp.status_code >= 400:
        return []
    return resp.json() or []


def _render_log_buttons(meal: str, entries: list[dict], token: str) -> None:
    """Three-button meal log. `Ate different` opens a picker with dropdown
    + custom-macros fallback so unknown dishes still record real numbers.

    For entries with `alternatives` (Tea/Coffee/Milk-style choice groups),
    a per-entry radio lets the user pick what they actually had before
    hitting `Ate planned`. Picking a non-default alternative auto-flips the
    action to `ate_different` and records the swap — feeds the Learning
    Agent's per-dish preference detection."""
    import uuid as _uuid

    # Per-entry alternative pickers. Each entry with a non-empty
    # `alternatives` list gets a radio. session_state[f"alt-{meal}-{idx}"]
    # holds the user's current selection (dish_ref). Default is the
    # planned dish itself.
    alt_selections: list[str] = []
    for idx, entry in enumerate(entries):
        alts = entry.get("alternatives") or []
        planned_ref = entry["dish_ref"]
        planned_name = entry.get("dish_name") or planned_ref
        if not alts:
            alt_selections.append(planned_ref)
            continue
        options = [(planned_ref, planned_name)] + [
            (a["dish_ref"], a["dish_name"]) for a in alts
        ]
        labels = [name for _, name in options]
        refs = [ref for ref, _ in options]
        key = f"alt-{meal}-{idx}"
        chosen = st.radio(
            f"What did you actually have (from {' / '.join(labels)})?",
            options=labels,
            index=0,
            key=key,
            horizontal=True,
            label_visibility="collapsed",
        )
        alt_selections.append(refs[labels.index(chosen)])

    # Detect whether any radio was flipped away from the planned pick. If
    # so, the correct action is `ate_different` with the actual dish_refs,
    # not `ate_planned` — that's what feeds meaningful pattern detection.
    swapped_any = any(
        alt_selections[i] != entries[i]["dish_ref"] for i in range(len(entries))
    )

    cols = st.columns(3)
    ate_label = "Ate this" if swapped_any else "Ate planned"
    if cols[0].button(ate_label, key=f"ate-{meal}"):
        actual = [
            {"dish_ref": alt_selections[i], "servings": entries[i]["servings"]}
            for i in range(len(entries))
        ]
        payload = {
            "event_id": str(_uuid.uuid4()),
            "meal": meal,
            "action": "ate_different" if swapped_any else "ate_planned",
            "actual_dishes": actual,
        }
        ok, msg, warnings = _post_log(payload, token)
        (st.success if ok else st.error)(msg)
        for w in warnings:
            st.warning(w)
        if ok:
            _bust_cache()
            st.rerun()

    if cols[1].button("Ate different", key=f"diff-{meal}"):
        st.session_state[f"diff_form_{meal}"] = True

    if cols[2].button("Skipped", key=f"skip-{meal}"):
        payload = {
            "event_id": str(_uuid.uuid4()),
            "meal": meal,
            "action": "skipped",
            "actual_dishes": [],
        }
        ok, msg, warnings = _post_log(payload, token)
        (st.success if ok else st.error)(msg)
        if ok:
            _bust_cache()
            st.rerun()

    if st.session_state.get(f"diff_form_{meal}"):
        _render_ate_different_form(meal, token)


def _render_ate_different_form(meal: str, token: str) -> None:
    import uuid as _uuid

    st.markdown("**Log what you actually ate:**")
    macros_list = _search_macros(token, "")
    options = [f"{m['dish_name']} · {m['kcal']:.0f} kcal / {m['protein_g']:.0f}g P" for m in macros_list]
    normalized = [m["dish_name_normalized"] for m in macros_list]
    options.append("— Other (enter macros manually) —")

    with st.form(f"diff-form-{meal}"):
        picked = st.selectbox("Dish", options, index=0, key=f"diff-pick-{meal}")
        servings = st.number_input(
            "Servings", min_value=0.25, max_value=10.0, value=1.0, step=0.25,
            key=f"diff-serv-{meal}",
        )
        custom = picked == options[-1]
        custom_name = ""
        kcal = protein = carbs = fats = 0.0
        if custom:
            custom_name = st.text_input("Dish name (free-form)", key=f"diff-name-{meal}")
            c1, c2, c3, c4 = st.columns(4)
            kcal = c1.number_input("kcal", 0.0, 3000.0, 300.0, key=f"k-{meal}")
            protein = c2.number_input("protein_g", 0.0, 200.0, 10.0, key=f"p-{meal}")
            carbs = c3.number_input("carbs_g", 0.0, 500.0, 40.0, key=f"c-{meal}")
            fats = c4.number_input("fats_g", 0.0, 200.0, 10.0, key=f"f-{meal}")

        note = st.text_input("Note (optional)", key=f"diff-note-{meal}")
        submitted = st.form_submit_button("Save log")
        if not submitted:
            return

        if custom:
            dish_entry = {
                "dish_ref": custom_name.strip().lower().replace(" ", "_") or "custom_dish",
                "servings": servings,
                "kcal": kcal,
                "protein_g": protein,
                "carbs_g": carbs,
                "fats_g": fats,
            }
        else:
            idx = options.index(picked)
            dish_entry = {
                "dish_ref": normalized[idx],
                "servings": servings,
                "macro_id": macros_list[idx]["id"],
            }

        payload = {
            "event_id": str(_uuid.uuid4()),
            "meal": meal,
            "action": "ate_different",
            "actual_dishes": [dish_entry],
            "note": note or None,
        }
        ok, msg, warnings = _post_log(payload, token)
        (st.success if ok else st.error)(msg)
        for w in warnings:
            st.warning(w)
        if ok:
            st.session_state.pop(f"diff_form_{meal}", None)
            st.rerun()


def _render_preferences_editor() -> None:
    """Edit allergies / dislikes / restrictions / supplements + plan-mode
    without re-submitting the whole onboarding form."""
    token = st.session_state.access_token
    try:
        resp = _cached_get("/onboarding/me/preferences", token)
    except httpx.HTTPError as exc:
        st.error(f"Backend unreachable: {exc}")
        return
    if resp.status_code >= 400:
        st.error(f"{resp.status_code}: {resp.text}")
        return
    prefs = resp.json() or {}

    st.subheader("Preferences")
    st.caption("Editing here replaces your onboarding preferences. Learning-agent memory survives.")

    current_mode = prefs.get("plan_mode") or "mess_only"
    current_soft = float(prefs.get("budget_soft_inr") or 0.0)

    with st.form("prefs_form"):
        st.markdown("**Dietary**")
        allergies = st.text_input("Allergies", value=", ".join(prefs.get("allergies") or []))
        dislikes = st.text_input("Dislikes", value=", ".join(prefs.get("dislikes") or []))
        restrictions = st.text_input(
            "Restrictions", value=", ".join(prefs.get("restrictions") or [])
        )
        supplements = st.text_input(
            "Supplements", value=", ".join(prefs.get("supplements") or [])
        )

        st.divider()
        st.markdown("**Plan sources**")
        st.caption(
            "**Mess only** — plans use mess dishes first. Canteen fallback kicks "
            "in only when the mess plan is infeasible or gets impractical (e.g. "
            "4 cups of tea to hit target).  \n"
            "**Mess + Shops (mixed)** — every plan is augmented with canteen items "
            "up to your soft budget below."
        )
        mode_choice = st.radio(
            "Plan mode",
            options=["Mess only", "Mess + Shops (mixed)"],
            index=0 if current_mode == "mess_only" else 1,
            key="prefs-plan-mode",
            horizontal=True,
        )
        soft_budget = st.number_input(
            "Soft daily budget for canteen (₹)",
            min_value=0.0,
            max_value=2000.0,
            value=float(current_soft),
            step=10.0,
            help=(
                "Canteen additions in Mess + Shops mode won't push spend past this "
                "as much as possible. Ignored in Mess only mode unless canteen "
                "fallback is triggered by an infeasible mess plan."
            ),
            key="prefs-soft-budget",
        )
        # Nudge users away from a confusing config: mixed mode with a ₹0 cap
        # will produce no canteen additions and feel broken.
        if mode_choice == "Mess + Shops (mixed)" and soft_budget <= 0.0:
            st.warning(
                "Mixed mode with a ₹0 soft budget means no canteen items will be "
                "added — bump the budget or switch to Mess only."
            )

        if st.form_submit_button("Save preferences", type="primary"):
            payload = {
                "allergies": _split_csv(allergies),
                "dislikes": _split_csv(dislikes),
                "restrictions": _split_csv(restrictions),
                "supplements": _split_csv(supplements),
                "plan_mode": "mess_only" if mode_choice == "Mess only" else "mixed",
                "budget_soft_inr": float(soft_budget),
            }
            try:
                r = httpx.put(
                    f"{BACKEND_URL}/onboarding/me/preferences",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"}, timeout=15,
                )
            except httpx.HTTPError as exc:
                st.error(str(exc))
                return
            if r.status_code >= 400:
                st.error(r.text)
                return
            st.success("Preferences updated.")
            _bust_cache()
            st.rerun()


def _render_sidebar_weight_log() -> None:
    """Tiny weight-log widget. Triggers target recompute on >1kg delta."""
    token = st.session_state.get("access_token")
    if not token:
        return
    with st.sidebar.expander("Log weight"):
        with st.form("sidebar_weight"):
            w = st.number_input("Today's weight (kg)", 30.0, 250.0, value=70.0, step=0.1)
            note = st.text_input("Note", value="")
            if st.form_submit_button("Save"):
                try:
                    resp = httpx.post(
                        f"{BACKEND_URL}/onboarding/me/weight",
                        json={"weight_kg": float(w), "note": note or None},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=15,
                    )
                except httpx.HTTPError as exc:
                    st.sidebar.error(str(exc))
                    return
                if resp.status_code >= 400:
                    st.sidebar.error(resp.text)
                    return
                data = resp.json()
                if data.get("recomputed"):
                    st.sidebar.success(
                        f"Weight saved · targets recomputed "
                        f"({data['target_kcal']:.0f} kcal / {data['target_protein_g']:.0f}g P)"
                    )
                else:
                    st.sidebar.success("Weight saved (no target change)")
                _bust_cache()


def _render_history() -> None:
    """Past 7 days of plans + meal logs."""
    token = st.session_state.access_token
    st.subheader("Past 7 days")
    try:
        p_resp = _cached_get("/plans/history?days=7", token)
        l_resp = _cached_get("/meal-logs/week?days=7", token)
    except httpx.HTTPError as exc:
        st.error(f"Backend unreachable: {exc}")
        return
    plans = p_resp.json() if p_resp.status_code < 400 else []
    logs = l_resp.json() if l_resp.status_code < 400 else []

    st.markdown("### Plans")
    if not plans:
        st.info("No plans committed yet in the last 7 days.")
    for p in plans:
        st.markdown(
            f"- **{p['date']}** · plan #{p['plan_id']} · "
            f"{p['total_kcal']:.0f} kcal · {p['total_protein_g']:.0f}g P · "
            f"₹{p['total_cost_inr']:.0f} · conf {p['confidence']:.2f} · {p['status']}"
        )

    st.markdown("### Meal logs")
    if not logs:
        st.info("No meal logs in the last 7 days.")
    for l in logs:
        macros = l.get("macros") or {}
        st.markdown(
            f"- **{l['logged_at'][:10]}** · {l['meal']} · {l['action']} · "
            f"{float(macros.get('kcal',0)):.0f} kcal · {float(macros.get('protein_g',0)):.0f}g P"
        )


def _fetch_role_is_admin() -> bool:
    token = st.session_state.get("access_token")
    if not token:
        return False
    try:
        resp = _cached_get("/onboarding/me/role", token)
    except httpx.HTTPError:
        return False
    if resp.status_code >= 400:
        return False
    return resp.json().get("role") == "admin"


# --- Admin console (merged from admin_app.py) ------------------------------


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {st.session_state.access_token}"}


def _admin_api(method: str, path: str, **kwargs) -> httpx.Response:
    fn = getattr(httpx, method)
    return fn(f"{BACKEND_URL}{path}", headers=_admin_headers(), timeout=300, **kwargs)


@st.cache_data(ttl=30, show_spinner=False)
def _admin_get_cached(path: str, token: str) -> tuple[int, str]:
    """Cached GET for admin read endpoints. TTL is short so a stale view
    self-heals; mutating actions call `_admin_get_cached.clear()` to force
    a refetch on the next rerun."""
    r = httpx.get(
        f"{BACKEND_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    return r.status_code, r.text


def _render_admin_console() -> None:
    st.subheader("Admin console")
    sub_upload, sub_pending, sub_macros = st.tabs(
        ["Upload menu", "Pending cycles", "Verify macros"]
    )
    with sub_upload:
        _admin_upload_menu()
    with sub_pending:
        _admin_pending_cycles()
    with sub_macros:
        _admin_verify_macros()


def _admin_upload_menu() -> None:
    from datetime import date, timedelta

    default_from = date.today()
    default_to = default_from + timedelta(days=6)
    c1, c2, c3 = st.columns(3)
    with c1:
        eff_from = st.date_input("Effective from", value=default_from, key="adm-ef")
    with c2:
        eff_to = st.date_input("Effective to", value=default_to, key="adm-et")
    with c3:
        source = st.selectbox("Source", ["pdf", "email", "manual"], index=0, key="adm-src")
    pdf = st.file_uploader("Menu PDF", type=["pdf"], key="adm-pdf")
    if st.button("Ingest", type="primary", disabled=pdf is None, key="adm-ingest"):
        files = {"file": (pdf.name, pdf.getvalue(), "application/pdf")}
        data = {
            "effective_from": eff_from.isoformat(),
            "effective_to": eff_to.isoformat(),
            "source": source,
        }
        with st.spinner("Parsing + LLM extracting + estimating…"):
            resp = _admin_api("post", "/admin/menu/upload", files=files, data=data)
        if resp.status_code >= 400:
            st.error(f"{resp.status_code}: {resp.text}")
            return
        result = resp.json()
        st.success(
            f"Cycle {result['cycle_id']} — added={result['added']} · "
            f"est={result['unknown_dishes_estimated']} · flagged={result['flagged_for_review']}"
        )
        if not result["inserted"]:
            st.info("Same PDF already ingested — returned existing cycle.")


def _admin_pending_cycles() -> None:
    token = st.session_state.access_token
    status, body = _admin_get_cached("/admin/menu/pending", token)
    if status >= 400:
        st.error(f"{status}: {body}")
        return
    cycles = json.loads(body) or []
    if not cycles:
        st.info("No pending cycles.")
        return
    for cyc in cycles:
        with st.expander(
            f"#{cyc['id']} · {cyc['source']} · {cyc['effective_from']} → {cyc['effective_to']} "
            f"(flags: {cyc['pending_review_count']})"
        ):
            det_status, det_body = _admin_get_cached(f"/admin/menu/cycles/{cyc['id']}", token)
            if det_status < 400:
                st.json(json.loads(det_body).get("pending_review_json", []))
            ca, cr = st.columns(2)
            if ca.button("Approve", key=f"adm-app-{cyc['id']}", type="primary"):
                with st.spinner(f"Approving cycle #{cyc['id']} + activating menu…"):
                    r = _admin_api("post", f"/admin/menu/cycles/{cyc['id']}/approve")
                st.write(r.status_code, r.text)
                _admin_get_cached.clear()
                st.rerun()
            if cr.button("Reject", key=f"adm-rej-{cyc['id']}"):
                with st.spinner(f"Rejecting cycle #{cyc['id']}…"):
                    r = _admin_api("post", f"/admin/menu/cycles/{cyc['id']}/reject")
                st.write(r.status_code, r.text)
                _admin_get_cached.clear()
                st.rerun()


def _admin_verify_macros() -> None:
    token = st.session_state.access_token
    status, body = _admin_get_cached("/admin/macro-db/unverified", token)
    if status >= 400:
        st.error(f"{status}: {body}")
        return
    rows = json.loads(body) or []
    st.caption(f"{len(rows)} unverified macro rows")
    for row in rows:
        with st.expander(
            f"#{row['id']} · {row['dish_name_normalized']} · conf={row['confidence']:.2f}"
        ):
            cols = st.columns(4)
            kcal = cols[0].number_input("kcal", value=float(row["kcal"]), key=f"adm-k-{row['id']}")
            protein = cols[1].number_input("protein_g", value=float(row["protein_g"]), key=f"adm-p-{row['id']}")
            carbs = cols[2].number_input("carbs_g", value=float(row["carbs_g"]), key=f"adm-c-{row['id']}")
            fats = cols[3].number_input("fats_g", value=float(row["fats_g"]), key=f"adm-f-{row['id']}")
            if st.button("Approve verified", key=f"adm-v-{row['id']}", type="primary"):
                r = _admin_api(
                    "post",
                    f"/admin/macro-db/{row['id']}/verify",
                    json={
                        "kcal": kcal, "protein_g": protein, "carbs_g": carbs, "fats_g": fats,
                        "source": "manual",
                    },
                )
                st.write(r.status_code, r.text)
                _admin_get_cached.clear()
                st.rerun()


if __name__ == "__main__":
    main()
