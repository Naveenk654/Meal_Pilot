"""Seed the real LNMIIT weekly mess menu from the user's PDF.

Idempotent-ish: deletes any existing 'manual' cycle covering today, inserts a
fresh 14-day 'manual' cycle with proper day-by-meal items. Adds macro_db
rows for the ~30 dishes in the menu that aren't in mess_macros_seed.json,
with hand-picked plausible values.

Run: python -m data.lnmiit_menu_seed
"""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from supabase import create_client


# --- Additional macro_db entries needed by the LNMIIT menu -----------------
# Values are per-serving; portions match the mess's typical serve sizes.

EXTRA_MACROS = {
    "sprouts": {"serving_unit": "1 bowl (100g)", "serving_grams": 100, "kcal": 130, "protein_g": 9, "carbs_g": 20, "fats_g": 1, "is_veg": True, "practical_max_servings_per_day": 2},
    "omelet": {"serving_unit": "1 (2 eggs)", "serving_grams": 100, "kcal": 180, "protein_g": 13, "carbs_g": 1, "fats_g": 14, "is_veg": False, "practical_max_servings_per_day": 2},
    "boiled_chana_chat": {"serving_unit": "1 bowl (150g)", "serving_grams": 150, "kcal": 220, "protein_g": 11, "carbs_g": 32, "fats_g": 4, "is_veg": True, "practical_max_servings_per_day": 2},
    "garlic_chutney": {"serving_unit": "1 tbsp (15g)", "serving_grams": 15, "kcal": 25, "protein_g": 0.5, "carbs_g": 3, "fats_g": 1, "is_veg": True, "practical_max_servings_per_day": 3},
    "ketchup": {"serving_unit": "1 tbsp (15g)", "serving_grams": 15, "kcal": 20, "protein_g": 0.2, "carbs_g": 5, "fats_g": 0, "is_veg": True, "practical_max_servings_per_day": 3},
    "sambhar_vada": {"serving_unit": "2 vada + sambhar", "serving_grams": 200, "kcal": 280, "protein_g": 10, "carbs_g": 34, "fats_g": 12, "is_veg": True, "practical_max_servings_per_day": 2},
    "moong_dal_chilla": {"serving_unit": "2 pieces (120g)", "serving_grams": 120, "kcal": 220, "protein_g": 12, "carbs_g": 26, "fats_g": 7, "is_veg": True, "practical_max_servings_per_day": 2},
    "green_chutney": {"serving_unit": "1 tbsp (15g)", "serving_grams": 15, "kcal": 15, "protein_g": 0.5, "carbs_g": 2, "fats_g": 0.5, "is_veg": True, "practical_max_servings_per_day": 3},
    "boiled_moong_chat": {"serving_unit": "1 bowl (150g)", "serving_grams": 150, "kcal": 210, "protein_g": 14, "carbs_g": 30, "fats_g": 2, "is_veg": True, "practical_max_servings_per_day": 2},
    "matar_kulcha": {"serving_unit": "1 kulcha + matar", "serving_grams": 250, "kcal": 380, "protein_g": 12, "carbs_g": 52, "fats_g": 12, "is_veg": True, "practical_max_servings_per_day": 2},
    "dosa_sambhar": {"serving_unit": "1 dosa + sambhar", "serving_grams": 250, "kcal": 320, "protein_g": 9, "carbs_g": 48, "fats_g": 10, "is_veg": True, "practical_max_servings_per_day": 2},
    "samosa": {"serving_unit": "1 piece (70g)", "serving_grams": 70, "kcal": 230, "protein_g": 4, "carbs_g": 22, "fats_g": 14, "is_veg": True, "practical_max_servings_per_day": 2},
    "jalebi": {"serving_unit": "2 pieces (60g)", "serving_grams": 60, "kcal": 220, "protein_g": 1, "carbs_g": 40, "fats_g": 7, "is_veg": True, "practical_max_servings_per_day": 1},
    "imli_chutney": {"serving_unit": "1 tbsp (15g)", "serving_grams": 15, "kcal": 25, "protein_g": 0.3, "carbs_g": 6, "fats_g": 0, "is_veg": True, "practical_max_servings_per_day": 3},
    "butter_roti": {"serving_unit": "1 piece (45g)", "serving_grams": 45, "kcal": 130, "protein_g": 3.5, "carbs_g": 20, "fats_g": 4, "is_veg": True, "practical_max_servings_per_day": 5},
    "missi_roti": {"serving_unit": "1 piece (55g)", "serving_grams": 55, "kcal": 160, "protein_g": 5, "carbs_g": 24, "fats_g": 5, "is_veg": True, "practical_max_servings_per_day": 4},
    "matar_paneer": {"serving_unit": "1 bowl (180g)", "serving_grams": 180, "kcal": 300, "protein_g": 13, "carbs_g": 14, "fats_g": 22, "is_veg": True, "practical_max_servings_per_day": 2},
    "dal_arhar": {"serving_unit": "1 bowl (180g)", "serving_grams": 180, "kcal": 170, "protein_g": 10, "carbs_g": 22, "fats_g": 4, "is_veg": True, "practical_max_servings_per_day": 3},
    "veg_raita": {"serving_unit": "1 bowl (150g)", "serving_grams": 150, "kcal": 130, "protein_g": 5, "carbs_g": 10, "fats_g": 7, "is_veg": True, "practical_max_servings_per_day": 2},
    "pudina_rice": {"serving_unit": "1 bowl (200g)", "serving_grams": 200, "kcal": 300, "protein_g": 5, "carbs_g": 55, "fats_g": 6, "is_veg": True, "practical_max_servings_per_day": 2},
    "coconut_burfi": {"serving_unit": "1 piece (30g)", "serving_grams": 30, "kcal": 140, "protein_g": 2, "carbs_g": 18, "fats_g": 7, "is_veg": True, "practical_max_servings_per_day": 1},
    "dry_chhole_masala": {"serving_unit": "1 bowl (180g)", "serving_grams": 180, "kcal": 260, "protein_g": 12, "carbs_g": 32, "fats_g": 10, "is_veg": True, "practical_max_servings_per_day": 2},
    "dal_fry": {"serving_unit": "1 bowl (180g)", "serving_grams": 180, "kcal": 180, "protein_g": 11, "carbs_g": 22, "fats_g": 5, "is_veg": True, "practical_max_servings_per_day": 3},
    "schezwan_rice": {"serving_unit": "1 plate (200g)", "serving_grams": 200, "kcal": 310, "protein_g": 6, "carbs_g": 55, "fats_g": 8, "is_veg": True, "practical_max_servings_per_day": 2},
    "angoori_petha": {"serving_unit": "2 pieces (50g)", "serving_grams": 50, "kcal": 180, "protein_g": 1, "carbs_g": 42, "fats_g": 0.5, "is_veg": True, "practical_max_servings_per_day": 1},
    "aloo_bhujia": {"serving_unit": "1 bowl (150g)", "serving_grams": 150, "kcal": 210, "protein_g": 4, "carbs_g": 26, "fats_g": 10, "is_veg": True, "practical_max_servings_per_day": 2},
    "roohafza": {"serving_unit": "1 glass (200ml)", "serving_grams": 200, "kcal": 120, "protein_g": 1, "carbs_g": 30, "fats_g": 0, "is_veg": True, "practical_max_servings_per_day": 2},
    "lauki_tamatar": {"serving_unit": "1 bowl (150g)", "serving_grams": 150, "kcal": 120, "protein_g": 3, "carbs_g": 12, "fats_g": 7, "is_veg": True, "practical_max_servings_per_day": 2},
    "besan_burfi": {"serving_unit": "1 piece (30g)", "serving_grams": 30, "kcal": 150, "protein_g": 3, "carbs_g": 18, "fats_g": 7, "is_veg": True, "practical_max_servings_per_day": 1},
    "bati": {"serving_unit": "2 pieces (100g)", "serving_grams": 100, "kcal": 320, "protein_g": 8, "carbs_g": 44, "fats_g": 12, "is_veg": True, "practical_max_servings_per_day": 2},
    "mix_dal": {"serving_unit": "1 bowl (180g)", "serving_grams": 180, "kcal": 180, "protein_g": 11, "carbs_g": 22, "fats_g": 5, "is_veg": True, "practical_max_servings_per_day": 3},
    "mirch_ka_salan": {"serving_unit": "1 bowl (150g)", "serving_grams": 150, "kcal": 180, "protein_g": 4, "carbs_g": 12, "fats_g": 14, "is_veg": True, "practical_max_servings_per_day": 2},
    "chhach": {"serving_unit": "1 glass (200ml)", "serving_grams": 200, "kcal": 60, "protein_g": 3, "carbs_g": 5, "fats_g": 3, "is_veg": True, "practical_max_servings_per_day": 3},
    "masala_mirch": {"serving_unit": "1 bowl (100g)", "serving_grams": 100, "kcal": 90, "protein_g": 2, "carbs_g": 10, "fats_g": 5, "is_veg": True, "practical_max_servings_per_day": 2},
    "lassi": {"serving_unit": "1 glass (250ml)", "serving_grams": 250, "kcal": 210, "protein_g": 7, "carbs_g": 26, "fats_g": 8, "is_veg": True, "practical_max_servings_per_day": 2},
    "lemon_rice": {"serving_unit": "1 bowl (200g)", "serving_grams": 200, "kcal": 290, "protein_g": 5, "carbs_g": 52, "fats_g": 7, "is_veg": True, "practical_max_servings_per_day": 2},
    "bhature": {"serving_unit": "1 piece (100g)", "serving_grams": 100, "kcal": 280, "protein_g": 5, "carbs_g": 32, "fats_g": 14, "is_veg": True, "practical_max_servings_per_day": 2},
    "namkeen_poha": {"serving_unit": "1 plate (150g)", "serving_grams": 150, "kcal": 250, "protein_g": 5, "carbs_g": 40, "fats_g": 8, "is_veg": True, "practical_max_servings_per_day": 2},
    "thandai": {"serving_unit": "1 glass (250ml)", "serving_grams": 250, "kcal": 260, "protein_g": 7, "carbs_g": 32, "fats_g": 12, "is_veg": True, "practical_max_servings_per_day": 1},
    "white_sauce_pasta": {"serving_unit": "1 plate (200g)", "serving_grams": 200, "kcal": 420, "protein_g": 12, "carbs_g": 50, "fats_g": 18, "is_veg": True, "practical_max_servings_per_day": 1},
    "rasna": {"serving_unit": "1 glass (200ml)", "serving_grams": 200, "kcal": 90, "protein_g": 0, "carbs_g": 22, "fats_g": 0, "is_veg": True, "practical_max_servings_per_day": 2},
    "veg_puff": {"serving_unit": "1 piece (80g)", "serving_grams": 80, "kcal": 240, "protein_g": 4, "carbs_g": 26, "fats_g": 13, "is_veg": True, "practical_max_servings_per_day": 2},
    "nimbu_pani": {"serving_unit": "1 glass (250ml)", "serving_grams": 250, "kcal": 90, "protein_g": 0, "carbs_g": 22, "fats_g": 0, "is_veg": True, "practical_max_servings_per_day": 3},
    "stuffed_kulcha": {"serving_unit": "1 kulcha (120g)", "serving_grams": 120, "kcal": 320, "protein_g": 8, "carbs_g": 44, "fats_g": 12, "is_veg": True, "practical_max_servings_per_day": 2},
    "vanilla_shake": {"serving_unit": "1 glass (300ml)", "serving_grams": 300, "kcal": 280, "protein_g": 8, "carbs_g": 40, "fats_g": 10, "is_veg": True, "practical_max_servings_per_day": 1},
    "veg_maggie": {"serving_unit": "1 plate (180g)", "serving_grams": 180, "kcal": 380, "protein_g": 9, "carbs_g": 52, "fats_g": 15, "is_veg": True, "practical_max_servings_per_day": 2},
    "pani_puri": {"serving_unit": "6 pieces (120g)", "serving_grams": 120, "kcal": 300, "protein_g": 5, "carbs_g": 48, "fats_g": 10, "is_veg": True, "practical_max_servings_per_day": 1},
    "jaljeera": {"serving_unit": "1 glass (200ml)", "serving_grams": 200, "kcal": 40, "protein_g": 0, "carbs_g": 10, "fats_g": 0, "is_veg": True, "practical_max_servings_per_day": 3},
    "corn_chat": {"serving_unit": "1 bowl (150g)", "serving_grams": 150, "kcal": 180, "protein_g": 5, "carbs_g": 32, "fats_g": 4, "is_veg": True, "practical_max_servings_per_day": 2},
    "milk_rose": {"serving_unit": "1 glass (200ml)", "serving_grams": 200, "kcal": 180, "protein_g": 6, "carbs_g": 24, "fats_g": 6, "is_veg": True, "practical_max_servings_per_day": 2},
    "achaari_aloo": {"serving_unit": "1 bowl (150g)", "serving_grams": 150, "kcal": 200, "protein_g": 3, "carbs_g": 24, "fats_g": 10, "is_veg": True, "practical_max_servings_per_day": 2},
    "dal_moong_tadka": {"serving_unit": "1 bowl (180g)", "serving_grams": 180, "kcal": 180, "protein_g": 11, "carbs_g": 22, "fats_g": 5, "is_veg": True, "practical_max_servings_per_day": 3},
    "curd_rice": {"serving_unit": "1 bowl (200g)", "serving_grams": 200, "kcal": 250, "protein_g": 7, "carbs_g": 40, "fats_g": 6, "is_veg": True, "practical_max_servings_per_day": 2},
    "ice_cream": {"serving_unit": "1 scoop (75g)", "serving_grams": 75, "kcal": 160, "protein_g": 3, "carbs_g": 18, "fats_g": 9, "is_veg": True, "practical_max_servings_per_day": 1},
    "black_chana_curry": {"serving_unit": "1 bowl (180g)", "serving_grams": 180, "kcal": 240, "protein_g": 12, "carbs_g": 32, "fats_g": 7, "is_veg": True, "practical_max_servings_per_day": 2},
    "dal_urad_tadka": {"serving_unit": "1 bowl (180g)", "serving_grams": 180, "kcal": 200, "protein_g": 12, "carbs_g": 24, "fats_g": 6, "is_veg": True, "practical_max_servings_per_day": 3},
    "peas_pulao": {"serving_unit": "1 bowl (200g)", "serving_grams": 200, "kcal": 320, "protein_g": 8, "carbs_g": 54, "fats_g": 8, "is_veg": True, "practical_max_servings_per_day": 2},
    "boondi": {"serving_unit": "1 bowl (50g)", "serving_grams": 50, "kcal": 240, "protein_g": 3, "carbs_g": 28, "fats_g": 12, "is_veg": True, "practical_max_servings_per_day": 1},
    "shahi_paneer": {"serving_unit": "1 bowl (180g)", "serving_grams": 180, "kcal": 360, "protein_g": 14, "carbs_g": 12, "fats_g": 28, "is_veg": True, "practical_max_servings_per_day": 2},
    "fried_rice": {"serving_unit": "1 bowl (200g)", "serving_grams": 200, "kcal": 320, "protein_g": 6, "carbs_g": 52, "fats_g": 10, "is_veg": True, "practical_max_servings_per_day": 2},
    "soya_chilli": {"serving_unit": "1 bowl (150g)", "serving_grams": 150, "kcal": 240, "protein_g": 16, "carbs_g": 18, "fats_g": 11, "is_veg": True, "practical_max_servings_per_day": 2},
    "panchratan_dal": {"serving_unit": "1 bowl (180g)", "serving_grams": 180, "kcal": 200, "protein_g": 12, "carbs_g": 24, "fats_g": 6, "is_veg": True, "practical_max_servings_per_day": 3},
    "dum_biryani": {"serving_unit": "1 plate (250g)", "serving_grams": 250, "kcal": 420, "protein_g": 10, "carbs_g": 62, "fats_g": 14, "is_veg": True, "practical_max_servings_per_day": 1},
    "rasgulla": {"serving_unit": "2 pieces (80g)", "serving_grams": 80, "kcal": 150, "protein_g": 3, "carbs_g": 30, "fats_g": 2, "is_veg": True, "practical_max_servings_per_day": 1},
    "dry_tinda_masala": {"serving_unit": "1 bowl (150g)", "serving_grams": 150, "kcal": 130, "protein_g": 3, "carbs_g": 10, "fats_g": 9, "is_veg": True, "practical_max_servings_per_day": 2},
    "arhar_dal": {"serving_unit": "1 bowl (180g)", "serving_grams": 180, "kcal": 170, "protein_g": 10, "carbs_g": 22, "fats_g": 4, "is_veg": True, "practical_max_servings_per_day": 3},
    "tawa_pulao": {"serving_unit": "1 plate (200g)", "serving_grams": 200, "kcal": 340, "protein_g": 7, "carbs_g": 56, "fats_g": 10, "is_veg": True, "practical_max_servings_per_day": 2},
    "matar_mushroom": {"serving_unit": "1 bowl (150g)", "serving_grams": 150, "kcal": 180, "protein_g": 6, "carbs_g": 12, "fats_g": 12, "is_veg": True, "practical_max_servings_per_day": 2},
    "dal_urad_fry": {"serving_unit": "1 bowl (180g)", "serving_grams": 180, "kcal": 210, "protein_g": 12, "carbs_g": 24, "fats_g": 7, "is_veg": True, "practical_max_servings_per_day": 3},
    "fruit_custard": {"serving_unit": "1 bowl (150g)", "serving_grams": 150, "kcal": 220, "protein_g": 5, "carbs_g": 30, "fats_g": 8, "is_veg": True, "practical_max_servings_per_day": 1},
    "sama_rice_kheer": {"serving_unit": "1 bowl (150g)", "serving_grams": 150, "kcal": 240, "protein_g": 5, "carbs_g": 36, "fats_g": 8, "is_veg": True, "practical_max_servings_per_day": 1},
    "panjabi_chhole": {"serving_unit": "1 bowl (180g)", "serving_grams": 180, "kcal": 260, "protein_g": 12, "carbs_g": 32, "fats_g": 10, "is_veg": True, "practical_max_servings_per_day": 2},
    "salad": {"serving_unit": "1 plate (100g)", "serving_grams": 100, "kcal": 35, "protein_g": 1.5, "carbs_g": 7, "fats_g": 0.3, "is_veg": True, "practical_max_servings_per_day": 3},
    "fruit_piece": {"serving_unit": "1 piece (150g)", "serving_grams": 150, "kcal": 80, "protein_g": 1, "carbs_g": 20, "fats_g": 0.3, "is_veg": True, "practical_max_servings_per_day": 2},
    "bread_slice": {"serving_unit": "2 slices (60g)", "serving_grams": 60, "kcal": 150, "protein_g": 5, "carbs_g": 28, "fats_g": 2, "is_veg": True, "practical_max_servings_per_day": 3},
    "jam": {"serving_unit": "1 tbsp (20g)", "serving_grams": 20, "kcal": 55, "protein_g": 0, "carbs_g": 14, "fats_g": 0, "is_veg": True, "practical_max_servings_per_day": 2},
    "butter": {"serving_unit": "1 tbsp (14g)", "serving_grams": 14, "kcal": 100, "protein_g": 0.1, "carbs_g": 0, "fats_g": 11, "is_veg": True, "practical_max_servings_per_day": 3},
    "peanut_butter_serving": {"serving_unit": "1 tbsp (16g)", "serving_grams": 16, "kcal": 95, "protein_g": 4, "carbs_g": 3, "fats_g": 8, "is_veg": True, "practical_max_servings_per_day": 2},
    "cornflakes": {"serving_unit": "1 bowl (40g flakes + 200ml milk)", "serving_grams": 240, "kcal": 275, "protein_g": 9, "carbs_g": 44, "fats_g": 7, "is_veg": True, "practical_max_servings_per_day": 2},
    "oats": {"serving_unit": "1 bowl (40g + 200ml milk)", "serving_grams": 240, "kcal": 260, "protein_g": 10, "carbs_g": 40, "fats_g": 6, "is_veg": True, "practical_max_servings_per_day": 2},
    "bournvita": {"serving_unit": "1 glass (200ml with milk)", "serving_grams": 220, "kcal": 180, "protein_g": 7, "carbs_g": 24, "fats_g": 6, "is_veg": True, "practical_max_servings_per_day": 1},
    "kadhi_pakora": {"serving_unit": "1 bowl (180g)", "serving_grams": 180, "kcal": 210, "protein_g": 6, "carbs_g": 18, "fats_g": 12, "is_veg": True, "practical_max_servings_per_day": 2},
}


# Weekly menu extracted from the LNMIIT PDF. day_of_week: 0=Mon..6=Sun.
# Each entry: (day_of_week, meal, dish_normalized). Common breakfast/lunch/dinner
# staples are added for every day.

DAILY_COMMON = {
    "breakfast": ["bread_slice", "butter", "jam", "tea", "bournvita", "fruit_piece", "cornflakes", "milk"],
    "lunch":     ["salad", "chapati", "butter_roti", "missi_roti"],
    "dinner":    ["salad", "chapati", "butter_roti", "papad"],
}

WEEKLY = {
    # Monday
    0: {
        "breakfast": ["sprouts", "omelet", "idli", "sambhar", "coconut_chutney"],
        "lunch":     ["matar_paneer", "dal_arhar", "boondi_raita", "veg_pulao"],
        "snack":     ["tea", "namkeen_poha", "thandai"],
        "dinner":    ["achaari_aloo", "dal_moong_tadka", "curd_rice", "ice_cream"],
    },
    # Tuesday
    1: {
        "breakfast": ["boiled_chana_chat", "aloo_paratha", "curd", "garlic_chutney", "ketchup"],
        "lunch":     ["mix_veg", "dal_makhani", "veg_raita", "pudina_rice", "coconut_burfi"],
        "snack":     ["tea", "white_sauce_pasta", "rasna"],
        "dinner":    ["black_chana_curry", "dal_urad_tadka", "peas_pulao", "poori", "boondi"],
    },
    # Wednesday
    2: {
        "breakfast": ["sprouts", "boiled_egg", "sambhar_vada", "coconut_chutney"],
        "lunch":     ["dry_chhole_masala", "dal_fry", "boondi_raita", "schezwan_rice", "angoori_petha"],
        "snack":     ["tea", "veg_puff", "nimbu_pani"],
        "dinner":    ["shahi_paneer", "dal_moong_tadka", "fried_rice", "gulab_jamun"],
    },
    # Thursday
    3: {
        "breakfast": ["sprouts", "omelet", "upma", "coconut_chutney", "moong_dal_chilla", "green_chutney"],
        "lunch":     ["aloo_bhujia", "kadhi_pakora", "roohafza", "jeera_rice"],
        "snack":     ["tea", "stuffed_kulcha", "vanilla_shake"],
        "dinner":    ["soya_chilli", "panchratan_dal", "dum_biryani", "rasgulla"],
    },
    # Friday
    4: {
        "breakfast": ["boiled_moong_chat", "boiled_egg", "matar_kulcha"],
        "lunch":     ["lauki_tamatar", "rajma", "boondi_raita", "plain_rice", "besan_burfi"],
        "snack":     ["tea", "veg_maggie", "cold_coffee"],
        "dinner":    ["dry_tinda_masala", "arhar_dal", "tawa_pulao", "milk_rose"],
    },
    # Saturday
    5: {
        "breakfast": ["sprouts", "omelet", "plain_dosa", "sambhar", "coconut_chutney"],
        "lunch":     ["bati", "mix_dal", "mirch_ka_salan", "chhach", "veg_biryani"],
        "snack":     ["tea", "pani_puri", "jaljeera"],
        "dinner":    ["matar_mushroom", "dal_urad_fry", "jeera_rice", "fruit_custard"],
    },
    # Sunday
    6: {
        "breakfast": ["sprouts", "omelet", "samosa", "jalebi", "green_chutney", "imli_chutney"],
        "lunch":     ["panjabi_chhole", "masala_mirch", "lassi", "lemon_rice", "bhature"],
        "snack":     ["tea", "corn_chat", "milk_rose"],
        "dinner":    ["mix_veg", "dal_makhani", "pudina_rice", "sama_rice_kheer"],
    },
}


def main() -> None:
    svc = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    now = datetime.now(timezone.utc).isoformat()

    # 1. Upsert extra macro_db entries.
    for name, spec in EXTRA_MACROS.items():
        body = {
            "dish_name_normalized": name,
            **spec,
            "source": "manual",
            "confidence": 1.0,
            "verified": True,
            "verified_at": now,
        }
        svc.table("macro_db").upsert(body, on_conflict="dish_name_normalized").execute()
    print(f"upserted {len(EXTRA_MACROS)} extra macro rows")

    # 2. Delete any previously-created 'manual' cycles covering today.
    today = date.today()
    old = svc.table("menu_cycles").select("id").eq("source", "manual").execute().data or []
    for c in old:
        svc.table("menu_items").delete().eq("cycle_id", c["id"]).execute()
        svc.table("menu_cycles").delete().eq("id", c["id"]).execute()
    print(f"purged {len(old)} old manual cycle(s)")

    # 3. Insert a new 14-day cycle.
    ef = today
    et = today + timedelta(days=13)
    cyc = (
        svc.table("menu_cycles")
        .insert(
            {
                "effective_from": ef.isoformat(),
                "effective_to": et.isoformat(),
                "source": "manual",
                "version": 1,
                "content_hash": f"lnmiit-{uuid.uuid4()}",
                "status": "active",
            }
        )
        .execute()
        .data[0]
    )
    cycle_id = cyc["id"]
    print(f"created cycle {cycle_id}")

    # 4. Map macros for lookups.
    macros = svc.table("macro_db").select("id, dish_name_normalized, is_veg").execute().data or []
    by_name = {m["dish_name_normalized"]: m for m in macros}

    rows: list[dict] = []
    for dow in range(7):
        # Merge common + day-specific per meal, dedupe.
        merged = {
            meal: list(dict.fromkeys(DAILY_COMMON.get(meal, []) + WEEKLY[dow].get(meal, [])))
            for meal in ("breakfast", "lunch", "snack", "dinner")
        }
        for meal, dishes in merged.items():
            for dn in dishes:
                m = by_name.get(dn)
                if not m:
                    print(f"  ! missing macro_db entry for '{dn}' — skipping")
                    continue
                rows.append(
                    {
                        "cycle_id": cycle_id,
                        "day_of_week": dow,
                        "meal": meal,
                        "dish_name": dn.replace("_", " ").title(),
                        "is_veg": m["is_veg"],
                        "macro_id": m["id"],
                        "confidence": 1.0,
                    }
                )
    if rows:
        svc.table("menu_items").insert(rows).execute()
    print(f"inserted {len(rows)} menu_items across 7 days")


if __name__ == "__main__":
    main()
