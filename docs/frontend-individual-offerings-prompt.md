# Frontend implementation prompt: individual 2026 offerings

Use the following prompt in the frontend repository:

> Update the frontend purchasing experience to match the ICPC 2026 backend.
> Study the existing presentation, workshop, bundle, registration, payment-return,
> and authenticated-user flows before editing. Preserve the current visual system,
> routing conventions, API client, error handling, and unrelated user changes.
>
> The backend no longer offers full packages or child-course bundles. Remove package
> cards, package pricing, child selection, bundle totals, and every `child_ids` field
> from registration requests. Do not present last year's parent/child bundle UI.
>
> Load the current catalogue with `GET /api/presentations/offerings/`. Each item now
> includes:
>
> - `offering_type`: `ONLINE_PRESENTATION`, `OFFLINE_PRESENTATION`,
>   `IN_PERSON_WORKSHOP`, or `ONLINE_WORKSHOP`
> - `offering_type_display`
> - `capacity`: an integer, or `null` when unlimited
> - `remained_capacity`: an integer, or `null` when unlimited
> - `is_unlimited`: boolean
> - `price`: an integer in Toman
> - `currency`: always `IRT` for this release
> - the existing descriptive, presenter, schedule, approval, slug, and activity fields
>
> Keep `GET /api/presentations/course/{slug}/` for detail pages. Register for exactly
> one item with `POST /api/presentations/register/` using
> `{ "course_id": number, "extra_answers"?: object }`. Never send `child_ids`.
>
> Present these catalogue defaults correctly:
>
> - Online presentation: 250 seats, 85,000 Toman
> - Offline presentation: unlimited, 60,000 Toman
> - In-person workshop: 125 seats, 125,000 Toman
> - Online workshop: 80 seats, 85,000 Toman
>
> Treat the API values as authoritative if an administrator changes a particular
> offering. Format `price` as Toman; do not multiply by ten. For unlimited offerings,
> show “Unlimited” (or the product's established localized equivalent) and never
> render `null` as zero, sold out, or unavailable. For finite offerings, disable the
> purchase action when `remained_capacity` is zero while still rendering the item.
>
> Continue using `GET /api/presentations/me/registrations/` for the user's purchases.
> Registration responses now expose the price snapshot and `currency`. Handle states
> consistently with the backend lifecycle: `RESERVED` is the capacity-full waitlist,
> `QUEUED` awaits approval/payment progression, `APPROVED` has reserved a seat and may
> contain `payment_link`, `FINAL` is paid/finalized, and `CANCELLED` releases the seat
> after a failed payment. Keep the existing gateway callback/verification experience.
>
> Update frontend types, schemas, fixtures, API mocks, copy, and tests. Cover all four
> offering types, Toman formatting, unlimited capacity, zero remaining finite capacity,
> the single-item registration payload, registration statuses, and removal of package
> controls. Run the frontend's relevant unit, integration, lint, and type-check commands,
> then report changed files, results, and remaining compatibility concerns. Do not commit
> or push until explicitly approved.
