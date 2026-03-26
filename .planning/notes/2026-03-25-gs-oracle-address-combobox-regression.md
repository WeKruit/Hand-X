---
date: "2026-03-25 15:55"
promoted: false
---

Goldman Sachs Oracle regression root cause: live extractor sees Address Line 1 as select/combobox (ff-25), not text. DomHand routes it into generic dropdown flow, but this widget actually needs typed address search + suggestion commit that backfills ZIP/City/State/County. Current live state proves phone is already filled ((571) 778-8080), while address cluster remains broken with State=Nevada and ZIP/City/County empty.

Why this is a regression versus earlier versions: current extraction/fill path now classifies Oracle `role="combobox"` / `data-uxi-widget-type="selectinput"` more aggressively as generic select, which is correct for normal Oracle dropdowns but wrong for this address-autocomplete transaction. The live page proves typing `13600 Brockmeyer Ct` into ff-25 immediately produces visible `role="gridcell"` suggestions such as `13600 BROCKMEYER CT, CHANTILLY, VIRGINIA, 20151`, so the broken step is not data loading; it is our failure to select and commit one of those suggestions. This slipped because we had Oracle Step 2/3 fixtures (visa type, latest employer, degree) but no harsh Step 1 address-cluster regression that requires suggestion click plus dependent ZIP/City/State/County backfill.
