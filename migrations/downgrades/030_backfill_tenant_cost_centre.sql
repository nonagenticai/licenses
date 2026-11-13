-- Downgrade 030. Returns the 12 rows this file set back to NULL.
--
-- SCOPED TWICE, deliberately, so a downgrade cannot erase somebody else's work:
--   * by NAME — only the 12 rows 030 names, never "everything that is not NULL";
--   * by VALUE — only where the row still holds the value 030 wrote. If an
--     operator has since changed `svc-vlm-smoke` from 'service' to something
--     else, that is THEIR value, not 030's, and this file leaves it alone.
-- The 43 pre-existing classifications (the 33 svc-* rows, the 5 customers, the
-- rest) are untouched by construction: 030 only ever wrote where cost_centre was
-- NULL, so it never owned them.
--
-- Losing these 12 values is not a data loss of consequence — they are fully
-- re-derivable from 030 itself, which is why the header rule ("100% of classified
-- siblings agree") is written down rather than left as a judgement.

UPDATE tenants SET cost_centre = NULL
 WHERE cost_centre = 'service'
   AND name IN (
     'svc-image-e2e-customer',
     'svc-kag-eval-r46',
     'svc-modality-gpuproof-e2e',
     'svc-modality-gw-e2e',
     'svc-modality-phaseb-e2e',
     'svc-probe-gwflip-20260813',
     'svc-probe-tenant-header-20260812',
     'svc-probe-tenant-header-20260812b',
     'svc-probe-vouch-20260813',
     'svc-vlm-smoke'
   );

UPDATE tenants SET cost_centre = NULL
 WHERE cost_centre = 'customers' AND name = 'subhankar-kag-2';

UPDATE tenants SET cost_centre = NULL
 WHERE cost_centre = 'tests' AND name = 'fr-test';
