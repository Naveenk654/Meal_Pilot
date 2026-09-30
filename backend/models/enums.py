from enum import Enum


class MealSlot(str, Enum):
    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    SNACK = "snack"
    DINNER = "dinner"


class Gender(str, Enum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"


class ActivityLevel(str, Enum):
    SEDENTARY = "sedentary"
    LIGHT = "light"
    GYM_3_4 = "gym_3_4"
    GYM_5_6 = "gym_5_6"
    ATHLETE = "athlete"


class Goal(str, Enum):
    CUT = "cut"
    MAINTAIN = "maintain"
    BULK = "bulk"


class UserMode(str, Enum):
    FITNESS = "fitness"
    GENERAL = "general"


class PlanMode(str, Enum):
    """Which sources the Planner is allowed to build a plan from.

    - mess_only: prefer mess. Fall back to canteen when mess-only is
      infeasible OR when the best mess candidate's soft score is below
      settings.fallback_quality_threshold (catches "4 servings of tea to
      hit target"-style dumb plans).
    - mixed: augment every mess plan with canteen items up to
      user_profile.budget_soft_inr to hit targets more precisely.
    """

    MESS_ONLY = "mess_only"
    MIXED = "mixed"


class UserRole(str, Enum):
    STUDENT = "student"
    ADMIN = "admin"


class Trigger(str, Enum):
    MORNING_CRON = "morning_cron"
    MEAL_LOG = "meal_log"
    OVERRIDE = "override"
    SKIP = "skip"
    MENU_UPDATE = "menu_update"


class HITLStatus(str, Enum):
    NOT_NEEDED = "not_needed"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PlanStatus(str, Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    SENT = "sent"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"


class PreferenceKind(str, Enum):
    ALLERGY = "allergy"
    DISLIKE = "dislike"
    LIKE = "like"
    SUPPLEMENT = "supplement"
    RESTRICTION = "restriction"


class PreferenceSource(str, Enum):
    ONBOARDING = "onboarding"
    LEARNING = "learning"
    MANUAL = "manual"


class PreferenceAuthority(str, Enum):
    HARD = "hard"
    SOFT = "soft"


class PreferenceStatus(str, Enum):
    ACTIVE = "active"
    RETIRED = "retired"


class MacroSource(str, Enum):
    MANUAL = "manual"
    IFCT = "ifct"
    NUTRITIONIX = "nutritionix"
    LLM_ESTIMATED = "llm_estimated"


class MealAction(str, Enum):
    ATE_PLANNED = "ate_planned"
    ATE_DIFFERENT = "ate_different"
    SKIPPED = "skipped"


class HardViolationType(str, Enum):
    ALLERGY = "allergy"
    RESTRICTION = "restriction"
    DISH_UNAVAILABLE = "dish_unavailable"
    BUDGET_HARD_CEILING = "budget_hard_ceiling"
    MEAL_SLOT_MISMATCH = "meal_slot_mismatch"
    PRACTICAL_SERVING_EXCEEDED = "practical_serving_exceeded"
    # Fires when a plan uses >1 dish from the same menu choice group in the
    # same meal slot. Choice groups are things like "Tea / Coffee / Milk" —
    # alternatives, not a set to combine.
    CHOICE_GROUP_VIOLATED = "choice_group_violated"
    # Bends §8's "macro deviation is soft" rule: obviously bad plans (huge
    # protein shortfall or calorie blow-out) get rejected so the revision loop
    # forces the LLM to try again. Threshold ratios are configurable.
    MACRO_KCAL_CEILING = "macro_kcal_ceiling"
    MACRO_PROTEIN_FLOOR = "macro_protein_floor"
