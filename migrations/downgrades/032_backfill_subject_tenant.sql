-- Downgrade 032. Returns the 51 subjects this file parented back to NULL.
--
-- EXACT. All 51 held `tenant_id IS NULL` before 032 ran — that is the predicate
-- 032 selected them with, and prod held NULL on all 254 rows on 2026-09-05 — so
-- restoring NULL restores the exact prior state. There is no per-row prior value
-- to remember.
--
-- SCOPED TWICE, deliberately, so a downgrade cannot erase somebody else's work:
--   * by SUBJECT ID — only the 51 rows 032 names, never `WHERE tenant_id IS NOT
--     NULL`. A subject parented by an operator, or by a later migration, was
--     never 032's and must survive this file.
--   * by TENANT ID — only where the subject still points at the tenant 032
--     wrote. If an operator has since re-parented `svc-workers-ahtapol` to a
--     different org, that is THEIR value, not 032's, and this file leaves it
--     alone.
-- The 203 subjects 032 deliberately left NULL are untouched by construction:
-- 032 only ever wrote where `tenant_id` was NULL and only to the ids below, so
-- it never owned them.
--
-- ⚠️ WHAT DOWNGRADING COSTS — LESS THAN 029's DOWNGRADE, AND FOR A REASON WORTH
-- STATING. 029's downgrade warns that anything in this column "is NOT
-- re-derivable ... there is no list in this repo to regenerate them from". That
-- warning is about parentage an OPERATOR stated. It does not apply to these 51:
-- they are re-derivable, because the derivation is written down in 032's header
-- and the pairs are listed below. Losing them costs one re-apply of 032.
-- The 41 machine-parented subjects, the 8 ambiguous ones and the 154 without
-- licences never had a value to lose.
--
-- IDs, not names, on both sides: tenants get renamed in this corpus (024, 026);
-- primary keys do not change.

UPDATE subject s
   SET tenant_id = NULL
  FROM (VALUES
     ('019ff6db-3654-7208-9c7f-ae551d37be64'::uuid, '0195f48b-d000-7000-8000-000000000003'::uuid),  -- 22560966-8ac0-4b56-af59-b646325d95d1        -> aa-agents
     ('01a06ff3-8c55-703e-87bc-debf10f3cff0'::uuid, '0195f48b-d000-7000-8000-000000000003'::uuid),  -- svc-kag-aa-agents                           -> aa-agents
     ('01a06ff1-e959-7534-bbb3-41b9dca71334'::uuid, '0195f48b-d000-7000-8000-000000000003'::uuid),  -- svc-workers-aa-agents                       -> aa-agents
     ('019ed1d7-0306-739b-8197-f24b981477aa'::uuid, '0195f48b-d000-7000-8000-000000000003'::uuid),  -- tenant-0195f48b-d000-7000-8000-000000000003 -> aa-agents
     ('019fa340-ae45-761a-9a7d-b42604f320b2'::uuid, '019fa340-a5e4-709d-8047-f555f063068a'::uuid),  -- 5836a145-1d1e-4890-a0fc-3cddeaf111cf        -> aa-tenant-proof
     ('019ff0a5-9052-7315-8a37-c696dff12509'::uuid, '0195f48b-d000-7000-8000-000000000101'::uuid),  -- 2b08c2e6-a1b2-4eff-9f1c-4366e2636a9e        -> ahtapol
     ('019ff0a5-63db-772d-a2b7-8ae2cc802cdb'::uuid, '0195f48b-d000-7000-8000-000000000101'::uuid),  -- 4e0daa8a-252f-4203-a8aa-0f2b6a7d5e27        -> ahtapol
     ('019ff0a4-b4dd-7445-87db-986d16df5736'::uuid, '0195f48b-d000-7000-8000-000000000101'::uuid),  -- aad9fff9-fcd6-4a02-83f3-cf133f15d384        -> ahtapol
     ('01a06ff2-d1b6-70da-9ab5-ca95047b5627'::uuid, '0195f48b-d000-7000-8000-000000000101'::uuid),  -- svc-workers-ahtapol                         -> ahtapol
     ('019e97ad-ee93-7609-bdc2-3a11a054b1c2'::uuid, '1d0090ac-adbf-4c07-b704-bc66061b572e'::uuid),  -- 1d0090ac-adbf-4c07-b704-bc66061b572e        -> book-fulfillment
     ('019fe9ca-81cb-71dc-9da0-f7cd6e87d8d4'::uuid, '0195f48b-d000-7000-8000-000000000100'::uuid),  -- 9897e262-100a-4487-adc3-121b2705568d        -> cdmsfirst
     ('019fe700-4d80-70c7-8a9b-77c44eaeb65a'::uuid, '0195f48b-d000-7000-8000-000000000100'::uuid),  -- c98824db-e650-4429-a4f4-1bb3442c9fce        -> cdmsfirst
     ('01a06ff2-a74c-7039-b273-3e3975166dbe'::uuid, '0195f48b-d000-7000-8000-000000000100'::uuid),  -- svc-workers-cdmsfirst                       -> cdmsfirst
     ('01a03dd9-a386-7731-b6a5-6fa01df183db'::uuid, '01a03dd9-941a-7300-ad03-f099dcf93864'::uuid),  -- 19141de3-ca06-4c04-84dc-d3ceb4b33ba4        -> claude
     ('019ec6e8-53e9-7522-a66f-11795b884625'::uuid, '00000000-0000-0000-0000-000000000000'::uuid),  -- 00000000-0000-0000-0000-000000000000        -> default-anon
     ('019dcaf1-b4a3-7798-853d-2d9383aa98a5'::uuid, '019de842-c92b-7579-a7e6-5b79642d6c80'::uuid),  -- e2e-subject-qe95guoq                        -> e2e-smoke-qe95guoq
     ('019dcaf3-3f6e-762b-9073-6507b0d9ca20'::uuid, '019de842-c92b-75b0-9502-88a436832a67'::uuid),  -- e2e-subject-xtzz3028                        -> e2e-smoke-xtzz3028
     ('019ed54e-a826-71da-ba73-b0036ca34c57'::uuid, '019efaa0-512b-733e-9e98-b1ac82a6f7ee'::uuid),  -- smoke-operator-3806d6e5                     -> fr-test
     ('019f5237-90cc-758d-9b5c-14853a85c156'::uuid, '0195f48b-d000-7000-8000-000000000099'::uuid),  -- 019ec527-8219-7564-99a5-81b181715630        -> fr-test-user
     ('01a04ce2-64d3-71b6-9a67-4d5b4ae5d2e6'::uuid, '0195f48b-d000-7000-8000-000000000099'::uuid),  -- svc-kag-eval                                -> fr-test-user
     ('01a06ff2-0d83-7074-85af-57d84495d5ad'::uuid, '0195f48b-d000-7000-8000-000000000099'::uuid),  -- svc-workers-fr-test-user                    -> fr-test-user
     ('01a06ff3-d969-75fe-bb31-af7511f33131'::uuid, '639c333a-1613-4fbc-aa14-a7b1430dc0db'::uuid),  -- svc-kag-merqi                               -> merqi
     ('019f0929-c88f-71ea-989f-c86318267e3c'::uuid, '639c333a-1613-4fbc-aa14-a7b1430dc0db'::uuid),  -- tenant-merqi                                -> merqi
     ('06992eff-2805-7dd5-accf-ffd5ad61614b'::uuid, '019de842-c92a-7e37-8fdd-eb0d8c5396f9'::uuid),  -- test-user                                   -> n8n-dev
     ('09e52820-1a96-4ba0-8164-0f361f295324'::uuid, '0195f48b-d000-7000-8000-000000000001'::uuid),  -- c82077b7-08c2-46cd-886d-b0fa6a69f732        -> n8n-dev-portal-realm
     ('019ede27-b577-7640-a6f3-2b91ced833ec'::uuid, '0195f48b-d000-7000-8000-000000000001'::uuid),  -- f204a489-5cc5-41ac-82e7-4b0becc78827        -> n8n-dev-portal-realm
     ('01a06ff2-fcdf-72be-8ffd-881784ee4d82'::uuid, '0195f48b-d000-7000-8000-000000000001'::uuid),  -- svc-kag-n8n-dev-portal-realm                -> n8n-dev-portal-realm
     ('01a06ff1-c225-722e-b482-8c98fa637886'::uuid, '0195f48b-d000-7000-8000-000000000001'::uuid),  -- svc-workers-n8n-dev-portal-realm            -> n8n-dev-portal-realm
     ('019fa076-7e9e-776f-b99c-2137aae20282'::uuid, '019de842-c92b-752e-b39c-30d64950e1cc'::uuid),  -- 00000000-0000-7000-8000-0000deadbeef        -> n8n-test
     ('9144c7dc-e9ef-491e-96b2-d3b69b11e21e'::uuid, '019de842-c92b-752e-b39c-30d64950e1cc'::uuid),  -- n8n-test-perpetual                          -> n8n-test
     ('01a06ff3-5ad9-746a-8786-c7340b7f8260'::uuid, 'e4f70a83-81ee-429a-abca-6999e3a0d24f'::uuid),  -- svc-kag-nonagentic-gtm                      -> nonagentic-gtm
     ('019edc49-7c65-7602-8a72-6d185a75b0bf'::uuid, '019edc48-6060-76e9-8d8f-2e720e12c609'::uuid),  -- d71bc8cc-ad71-4836-8f75-32e29980a3aa        -> ola-armatys
     ('019edee5-03a6-77c1-bd30-f726e9cf0fa1'::uuid, '019edc48-6060-76e9-8d8f-2e720e12c609'::uuid),  -- tenant-019edc48-6060-76e9-8d8f-2e720e12c609 -> ola-armatys
     ('019dc19a-ab50-757c-bce5-2f4990b78ff9'::uuid, '0195f48b-d000-7000-8000-000000000002'::uuid),  -- 70640a77-153d-403b-95bc-0f3b3291b2ef        -> platform-core
     ('019fe83a-8b15-7661-a79a-3854ef09bc65'::uuid, 'e148e8af-3751-49bf-a0fa-56ab46d31ddc'::uuid),  -- 019fe83a-8590-7439-90ac-ce5a88800e4d        -> portal-render-e2e
     ('019ede27-b71a-7516-b603-6ad9ce563e48'::uuid, '0195f48b-d000-7000-8000-000000000005'::uuid),  -- 155ffb93-e49a-4662-aa36-57c1f2385126        -> procedure-improvement
     ('01a06d0d-50b7-7238-8e44-e16b03f1ce89'::uuid, '0195f48b-d000-7000-8000-000000000005'::uuid),  -- svc-inference-router-procedure-improvement  -> procedure-improvement
     ('01a06ff2-59e9-76aa-a6f5-71c6c546b931'::uuid, '0195f48b-d000-7000-8000-000000000005'::uuid),  -- svc-workers-procedure-improvement           -> procedure-improvement
     ('019ede27-b69b-75c1-b029-d8a933c4266f'::uuid, '0195f48b-d000-7000-8000-000000000004'::uuid),  -- 4e2c7635-e1b9-498d-9c3e-b084838fa02a        -> prompt-improvement
     ('01a06d0d-0b0a-7791-88ac-5330f3eb703a'::uuid, '0195f48b-d000-7000-8000-000000000004'::uuid),  -- svc-inference-router-prompt-improvement     -> prompt-improvement
     ('01a06ff2-3172-7126-9ce9-e226cb084059'::uuid, '0195f48b-d000-7000-8000-000000000004'::uuid),  -- svc-workers-prompt-improvement              -> prompt-improvement
     ('019fef81-708b-73b1-9685-f48517a4b188'::uuid, '019fb1f4-7342-7bb7-8ac6-2ff2fcda645f'::uuid),  -- f66eb059-029f-40c3-957d-e059300d07ae        -> rmt-e2e
     ('019edc10-d1c4-71c4-a854-bdcea801ca0e'::uuid, '019edc1e-313d-779d-9330-fe013c705be8'::uuid),  -- 1e3b6058-192c-4939-9b67-898a9923b3fb        -> subhankar-kag
     ('019ec6e8-5610-773e-b4da-20bc0d8aec27'::uuid, '00000000-0000-0000-0000-000000000001'::uuid),  -- 4a191861-b30e-430d-9660-975ae8dcdf52        -> system
     ('01a06ff3-2a1a-7635-adcf-1bb14e0b6884'::uuid, '00000000-0000-0000-0000-000000000001'::uuid),  -- svc-kag-system                              -> system
     ('01a06ff2-8057-7038-8f22-c4f9666be226'::uuid, '00000000-0000-0000-0000-000000000001'::uuid),  -- svc-workers-system                          -> system
     ('019ed470-9b21-7232-a6a8-2cd08f027fe2'::uuid, '00000000-0000-0000-0000-000000000001'::uuid),  -- tenant-00000000-0000-0000-0000-000000000001 -> system
     ('01a06ff3-b21d-71bc-9982-d4230b79d793'::uuid, '3806d6e5-4698-571e-9bb2-22ffd076ac73'::uuid),  -- svc-kag-tenant-dev                          -> tenant-dev
     ('019ed9f6-3b58-776f-bf9a-8b4abef22112'::uuid, '3806d6e5-4698-571e-9bb2-22ffd076ac73'::uuid),  -- tenant-3806d6e5-4698-571e-9bb2-22ffd076ac73 -> tenant-dev
     ('019fef99-e8bc-719a-934d-756389783fb3'::uuid, '019fef97-bc1a-74b1-89e6-5d0574210203'::uuid),  -- 019fef97-c061-73d4-a80c-29df91b3ffdd        -> zz-s62-probe-a1
     ('019fef97-c061-73d4-a80c-29df91b3ffdd'::uuid, '019fef97-bc1a-74b1-89e6-5d0574210203'::uuid)   -- zz-s62-probe-subject                        -> zz-s62-probe-a1
  ) AS v(subject_id, tenant_id)
 WHERE s.id = v.subject_id
   AND s.tenant_id = v.tenant_id;
