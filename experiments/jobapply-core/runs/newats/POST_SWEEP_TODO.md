# Post-sweep live-CDP targets (can't live-test during sweep — pkill contention)
- airbnb dual-upload: resume lands on COVER LETTER, resume stays empty (Attach buttons).
  ledger falsely says resume DONE 'uploaded+ui-verified' — card-scoped chip check FALSE-PASSES
  (found cover's chip for resume). Vision caught it (complete=False) so NOT a false-green, but
  fill fails + ui-verify unreliable. Live CDP: check upload ORDER + which input the file lands on.
  Repro: careers.airbnb.com dual Resume/CV + Cover Letter (greenhouse embed).
- ashby Yes/No pill fill: validate the active-class re-verify fix (5c0fd1e31) on tail-rerun of runs 1-4.
- reddit/duolingo veteran EEOC react-select: want='I am not a protected veteran', pick returns
  'No military service' (non-option or 7th), commit doesn't land, re-read sees '*' junk -> escalate.
  3/4 demographic react-selects fill via core; veteran specifically fails. Honest (gate catches).
  ROOT: read captures the veteran CLASSIFICATION sub-options (Active Reserve etc, shown only after picking "I identify as..."), NOT the top-level "I am not a protected veteran"/"I identify"/"do not wish" — read scoping grabs wrong option level. Live CDP the option tree.
