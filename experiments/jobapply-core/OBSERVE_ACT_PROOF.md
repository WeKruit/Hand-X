# observe_act — Generic Fill PROOF

> Fill-only, NEVER submitted. Single-page ATS (Greenhouse / Lever / Ashby) — no auth, no rate limit.
> Each run = one live company posting x one synthetic throwaway profile, filled field-by-field
> via the generic `observe_act` state machine (NOT the per-archetype `fill_with_ladder`).

## Headline

- **Runs:** 68 filled / 72 attempted (4 blocked/timeout/error)
- **Companies:** 36  |  **Profiles:** 10
- **Overall fill-rate:** **97%** (840/867 non-skip fields reached a DONE/OTHER terminal)
- **Cost:** $0.3266 total, $0.0048/run avg

## Fill-rate by ATS

| ATS | runs | blocked | fields filled | fillable | fill-rate | cost |
|---|---|---|---|---|---|---|
| ashby | 36 | 4 | 311 | 320 | 97% | $0.1257 |
| greenhouse | 24 | 0 | 352 | 357 | 99% | $0.1124 |
| lever | 12 | 0 | 177 | 190 | 93% | $0.0885 |

## Failure taxonomy (ESCALATE fields, bucketed by widget shape / field kind)

| failure bucket | count |
|---|---|
| location-geocomplete | 11 |
| radio-checkbox-boolean | 4 |
| date | 4 |
| multi-value-chips | 3 |
| free-text | 3 |
| file-upload | 1 |
| closed-list-select | 1 |

## Per-run detail

| # | ATS | org | profile | status | fill-rate | filled/fillable | cost | secs | screenshot |
|---|---|---|---|---|---|---|---|---|---|
| 0 | greenhouse | anthropic | pyr_backend | FILLED | 100% | 6/6 | $0.0022 | 15.0 | 000_greenhouse_anthropic_pyr_backend.png |
| 1 | greenhouse | anthropic | maya_ml | FILLED | 100% | 6/6 | $0.0021 | 21.8 | 001_greenhouse_anthropic_maya_ml.png |
| 2 | lever | palantir | maya_ml | FILLED | 100% | 22/22 | $0.0070 | 77.1 | 002_lever_palantir_maya_ml.png |
| 3 | lever | palantir | diego_mech | FILLED | 100% | 20/20 | $0.0067 | 50.7 | 003_lever_palantir_diego_mech.png |
| 4 | ashby | ramp | diego_mech | FILLED | 100% | 9/9 | $0.0037 | 42.7 | 004_ashby_ramp_diego_mech.png |
| 5 | ashby | ramp | aisha_quant | FILLED | 100% | 11/11 | $0.0042 | 32.4 | 005_ashby_ramp_aisha_quant.png |
| 6 | greenhouse | anthropic | aisha_quant | FILLED | 100% | 6/6 | $0.0020 | 25.2 | 006_greenhouse_anthropic_aisha_quant.png |
| 7 | greenhouse | anthropic | wen_phd | FILLED | 100% | 6/6 | $0.0021 | 15.4 | 007_greenhouse_anthropic_wen_phd.png |
| 8 | lever | palantir | wen_phd | FILLED | 100% | 21/21 | $0.0182 | 66.5 | 008_lever_palantir_wen_phd.png |
| 9 | lever | palantir | rosa_data | FILLED | 100% | 20/20 | $0.0179 | 41.7 | 009_lever_palantir_rosa_data.png |
| 10 | ashby | ramp | rosa_data | FILLED | 100% | 11/11 | $0.0041 | 53.7 | 010_ashby_ramp_rosa_data.png |
| 11 | ashby | ramp | kofi_sre | FILLED | 100% | 11/11 | $0.0042 | 40.1 | 011_ashby_ramp_kofi_sre.png |
| 12 | greenhouse | discord | kofi_sre | FILLED | 100% | 19/19 | $0.0050 | 109.6 | 012_greenhouse_discord_kofi_sre.png |
| 13 | greenhouse | discord | lena_design | FILLED | 100% | 19/19 | $0.0051 | 118.0 | 013_greenhouse_discord_lena_design.png |
| 14 | lever | spotify | lena_design | FILLED | 89% | 8/9 | $0.0049 | 46.1 | 014_lever_spotify_lena_design.png |
| 15 | lever | spotify | omar_security | FILLED | 89% | 8/9 | $0.0049 | 83.7 | 015_lever_spotify_omar_security.png |
| 16 | ashby | openai | omar_security | FILLED | 85% | 11/13 | $0.0049 | 109.7 | 016_ashby_openai_omar_security.png |
| 17 | ashby | openai | tanvi_newgrad | FILLED | 92% | 12/13 | $0.0051 | 82.9 | 017_ashby_openai_tanvi_newgrad.png |
| 18 | greenhouse | discord | tanvi_newgrad | FILLED | 100% | 20/20 | $0.0053 | 139.4 | 018_greenhouse_discord_tanvi_newgrad.png |
| 19 | greenhouse | discord | pyr_backend | FILLED | 100% | 20/20 | $0.0054 | 126.7 | 019_greenhouse_discord_pyr_backend.png |
| 20 | lever | spotify | pyr_backend | FILLED | 82% | 9/11 | $0.0055 | 42.0 | 020_lever_spotify_pyr_backend.png |
| 21 | lever | spotify | maya_ml | FILLED | 80% | 8/10 | $0.0054 | 60.9 | 021_lever_spotify_maya_ml.png |
| 22 | ashby | openai | maya_ml | FILLED | 85% | 11/13 | $0.0050 | 94.3 | 022_ashby_openai_maya_ml.png |
| 23 | ashby | openai | diego_mech | FILLED | 92% | 12/13 | $0.0050 | 99.9 | 023_ashby_openai_diego_mech.png |
| 24 | greenhouse | robinhood | diego_mech | FILLED | 95% | 20/21 | $0.0059 | 85.7 | 024_greenhouse_robinhood_diego_mech.png |
| 25 | greenhouse | robinhood | aisha_quant | FILLED | 100% | 21/21 | $0.0061 | 106.9 | 025_greenhouse_robinhood_aisha_quant.png |
| 26 | lever | ro | aisha_quant | FILLED | 88% | 15/17 | $0.0048 | 35.0 | 026_lever_ro_aisha_quant.png |
| 27 | lever | ro | wen_phd | FILLED | 89% | 16/18 | $0.0049 | 63.5 | 027_lever_ro_wen_phd.png |
| 28 | ashby | linear | wen_phd | FILLED | 100% | 7/7 | $0.0033 | 27.8 | 028_ashby_linear_wen_phd.png |
| 29 | ashby | linear | rosa_data | FILLED | 100% | 7/7 | $0.0030 | 40.3 | 029_ashby_linear_rosa_data.png |
| 30 | greenhouse | robinhood | rosa_data | FILLED | 100% | 22/22 | $0.0060 | 93.7 | 030_greenhouse_robinhood_rosa_data.png |
| 31 | greenhouse | robinhood | kofi_sre | FILLED | 100% | 23/23 | $0.0060 | 103.4 | 031_greenhouse_robinhood_kofi_sre.png |
| 32 | lever | ro | kofi_sre | FILLED | 88% | 15/17 | $0.0040 | 66.8 | 032_lever_ro_kofi_sre.png |
| 33 | lever | ro | lena_design | FILLED | 94% | 15/16 | $0.0042 | 46.4 | 033_lever_ro_lena_design.png |
| 34 | ashby | linear | lena_design | FILLED | 100% | 7/7 | $0.0033 | 46.7 | 034_ashby_linear_lena_design.png |
| 35 | ashby | linear | omar_security | FILLED | 100% | 7/7 | $0.0031 | 24.8 | 035_ashby_linear_omar_security.png |
| 36 | greenhouse | gitlab | omar_security | FILLED | 93% | 13/14 | $0.0052 | 77.9 | 036_greenhouse_gitlab_omar_security.png |
| 37 | greenhouse | gitlab | tanvi_newgrad | FILLED | 93% | 14/15 | $0.0055 | 44.4 | 037_greenhouse_gitlab_tanvi_newgrad.png |
| 38 | ashby | notion | tanvi_newgrad | FILLED | 100% | 10/10 | $0.0037 | 62.0 | 038_ashby_notion_tanvi_newgrad.png |
| 39 | ashby | notion | pyr_backend | FILLED | 100% | 10/10 | $0.0035 | 72.5 | 039_ashby_notion_pyr_backend.png |
| 40 | greenhouse | gitlab | pyr_backend | FILLED | 90% | 9/10 | $0.0042 | 19.0 | 040_greenhouse_gitlab_pyr_backend.png |
| 41 | greenhouse | gitlab | maya_ml | FILLED | 100% | 10/10 | $0.0068 | 33.1 | 041_greenhouse_gitlab_maya_ml.png |
| 42 | ashby | notion | maya_ml | FILLED | 80% | 8/10 | $0.0033 | 68.6 | 042_ashby_notion_maya_ml.png |
| 43 | ashby | notion | diego_mech | FILLED | 100% | 10/10 | $0.0036 | 62.4 | 043_ashby_notion_diego_mech.png |
| 44 | greenhouse | scaleai | diego_mech | FILLED | 100% | 18/18 | $0.0052 | 82.5 | 044_greenhouse_scaleai_diego_mech.png |
| 45 | greenhouse | scaleai | aisha_quant | FILLED | 100% | 18/18 | $0.0051 | 103.9 | 045_greenhouse_scaleai_aisha_quant.png |
| 46 | ashby | vanta | aisha_quant | FILLED | 100% | 11/11 | $0.0043 | 74.1 | 046_ashby_vanta_aisha_quant.png |
| 47 | ashby | vanta | wen_phd | FILLED | 100% | 11/11 | $0.0045 | 69.5 | 047_ashby_vanta_wen_phd.png |
| 48 | greenhouse | scaleai | wen_phd | FILLED | 100% | 14/14 | $0.0044 | 46.9 | 048_greenhouse_scaleai_wen_phd.png |
| 49 | greenhouse | scaleai | rosa_data | FILLED | 100% | 13/13 | $0.0040 | 56.2 | 049_greenhouse_scaleai_rosa_data.png |
| 50 | ashby | vanta | rosa_data | FILLED | 100% | 11/11 | $0.0042 | 59.3 | 050_ashby_vanta_rosa_data.png |
| 51 | ashby | vanta | kofi_sre | FILLED | 100% | 11/11 | $0.0042 | 52.3 | 051_ashby_vanta_kofi_sre.png |
| 52 | greenhouse | figma | kofi_sre | FILLED | 93% | 13/14 | $0.0045 | 80.8 | 052_greenhouse_figma_kofi_sre.png |
| 53 | greenhouse | figma | lena_design | FILLED | 100% | 14/14 | $0.0048 | 52.7 | 053_greenhouse_figma_lena_design.png |
| 54 | ashby | runway | lena_design | FILLED | 100% | 5/5 | $0.0025 | 5.9 | 054_ashby_runway_lena_design.png |
| 55 | ashby | runway | omar_security | FILLED | 100% | 5/5 | $0.0025 | 4.0 | 055_ashby_runway_omar_security.png |
| 56 | greenhouse | figma | omar_security | FILLED | 100% | 14/14 | $0.0049 | 52.2 | 056_greenhouse_figma_omar_security.png |
| 57 | greenhouse | figma | tanvi_newgrad | FILLED | 100% | 14/14 | $0.0046 | 36.7 | 057_greenhouse_figma_tanvi_newgrad.png |
| 58 | ashby | runway | tanvi_newgrad | FILLED | 100% | 5/5 | $0.0025 | 9.4 | 058_ashby_runway_tanvi_newgrad.png |
| 59 | ashby | runway | pyr_backend | FILLED | 100% | 5/5 | $0.0026 | 3.5 | 059_ashby_runway_pyr_backend.png |
| 60 | ashby | cursor | pyr_backend | BLOCKED | 0% | 0/0 | $0.0020 | 22.3 | 060_ashby_cursor_pyr_backend.png |
| 61 | ashby | cursor | maya_ml | BLOCKED | 0% | 0/0 | $0.0020 | 46.4 | 061_ashby_cursor_maya_ml.png |
| 62 | ashby | cursor | maya_ml | BLOCKED | 0% | 0/0 | $0.0020 | 22.3 | 062_ashby_cursor_maya_ml.png |
| 63 | ashby | cursor | diego_mech | BLOCKED | 0% | 0/0 | $0.0019 | 13.8 | 063_ashby_cursor_diego_mech.png |
| 64 | ashby | watershed | diego_mech | FILLED | 100% | 9/9 | $0.0036 | 15.8 | 064_ashby_watershed_diego_mech.png |
| 65 | ashby | watershed | aisha_quant | FILLED | 89% | 8/9 | $0.0039 | 18.5 | 065_ashby_watershed_aisha_quant.png |
| 66 | ashby | watershed | aisha_quant | FILLED | 100% | 7/7 | $0.0027 | 25.4 | 066_ashby_watershed_aisha_quant.png |
| 67 | ashby | watershed | wen_phd | FILLED | 100% | 7/7 | $0.0028 | 22.1 | 067_ashby_watershed_wen_phd.png |
| 68 | ashby | replit | wen_phd | FILLED | 100% | 16/16 | $0.0058 | 100.3 | 068_ashby_replit_wen_phd.png |
| 69 | ashby | replit | rosa_data | FILLED | 100% | 15/15 | $0.0055 | 66.1 | 069_ashby_replit_rosa_data.png |
| 70 | ashby | replit | rosa_data | FILLED | 100% | 15/15 | $0.0054 | 75.4 | 070_ashby_replit_rosa_data.png |
| 71 | ashby | replit | kofi_sre | FILLED | 100% | 16/16 | $0.0057 | 79.6 | 071_ashby_replit_kofi_sre.png |
