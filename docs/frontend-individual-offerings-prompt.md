# Frontend implementation contract: individual ICPC 2026 offerings

This is the complete handoff for updating the Axios + TypeScript frontend. Preserve
its existing Axios instance, JWT refresh/interceptors, routing, visual system,
localization, and error presentation.

## Product changes

The 2026 catalogue sells every presentation or workshop separately. Full packages
and parent/child bundles are no longer purchasable.

- Remove package cards/prices, child selectors, bundle totals, and package copy.
- Remove `children` from course types and `child_ids` from request types, forms,
  fixtures, mocks, and calls. Never send `child_ids`, even as `[]`.
- A new registration always targets exactly one offering by `course_id`.
- Old registrations can still contain legacy `items`. Render them read-only if they
  occur in history, but never use them to construct a new purchase.

## TypeScript contracts

Dates and times are JSON strings. Adapt names/locations to the existing type system.

```ts
export type OfferingType =
  | "ONLINE_PRESENTATION"
  | "OFFLINE_PRESENTATION"
  | "IN_PERSON_WORKSHOP"
  | "ONLINE_WORKSHOP";

export type Currency = "IRT";

export interface Presenter {
  id: number;
  full_name: string;
  bio: string;
  email: string;
  website: string;
}

export interface ScheduleRule {
  weekday: 0 | 1 | 2 | 3 | 4 | 5 | 6; // Monday = 0, Sunday = 6
  weekday_display: "Mon" | "Tue" | "Wed" | "Thu" | "Fri" | "Sat" | "Sun";
  start_time: string;
  end_time: string;
}

export interface Offering {
  id: number;
  name: string;
  subtitle: string;
  description: string;
  presenters: Presenter[];
  start_date: string | null;
  online: boolean;
  onsite: boolean;
  classes_count: number;
  offering_type: OfferingType;
  offering_type_display: string;
  capacity: number | null;
  remained_capacity: number | null;
  is_unlimited: boolean;
  price: number;
  currency: Currency;
  requires_approval: boolean;
  slug: string;
  is_active: boolean;
  schedule: ScheduleRule[];
}

// Registration history can contain pre-2026 legacy courses.
export type HistoricalCourse = Omit<Offering, "offering_type" | "price"> & {
  offering_type: OfferingType | null;
  price: number | null;
};

export type RegistrationStatus =
  | "SUBMITTED"
  | "RESERVED"
  | "QUEUED"
  | "APPROVED"
  | "FINAL"
  | "REJECTED"
  | "CANCELLED";

export interface LegacyRegistrationItem {
  id: number;
  child: Pick<HistoricalCourse, "id" | "name" | "offering_type" |
    "offering_type_display" | "capacity" | "remained_capacity" |
    "is_unlimited" | "price" | "currency" | "slug" | "is_active" |
    "schedule">;
  price: number;
  created_at: string;
}

export interface Registration {
  id: number;
  course: HistoricalCourse;
  user: number;
  price: number | null; // price snapshot at registration time
  currency: Currency;
  status: RegistrationStatus;
  resume_url: string;
  payment_link: string;
  rejection_reason: string;
  submitted_at: string;
  decided_at: string | null;
  items: LegacyRegistrationItem[];
  total_amount: number;
}

export interface CreateRegistrationRequest {
  course_id: number;
  extra_answers?: Record<string, unknown>;
}

export interface ApiErrorBody {
  errorCode: number;
  errorMessage: string;
}

export type PaymentStatus =
  | "PENDING" | "SUCCESSFUL" | "FAILED" | "PG_INITIATE_ERROR";

export interface Payment {
  id: number;
  status: PaymentStatus;
  authority: string;
  ref_id: string;
  amount: number;
  currency: Currency;
  zarinpal_code: string;
  zarinpal_message: string;
  target_type: "COURSE" | "COMPETITION";
  target_id: string;
}
```

The database retains nullable legacy course prices and types, hence
`HistoricalCourse`. Public 2026 offerings are active records with a valid
`offering_type` and normally have numeric prices.

## Axios calls

Paths are relative to the existing API base URL. List responses are raw arrays, not
pagination wrappers such as `{ results: [...] }`.

```ts
const listOfferings = async (): Promise<Offering[]> =>
  (await api.get<Offering[]>("/api/presentations/offerings/")).data;

const getOffering = async (slug: string): Promise<Offering> =>
  (await api.get<Offering>(
    `/api/presentations/course/${encodeURIComponent(slug)}/`,
  )).data;

const createRegistration = async (
  payload: CreateRegistrationRequest,
): Promise<Registration> =>
  (await api.post<Registration>("/api/presentations/register/", payload)).data;

const listMyRegistrations = async (): Promise<Registration[]> =>
  (await api.get<Registration[]>(
    "/api/presentations/me/registrations/",
  )).data;

const verifyPayment = async (authority: string): Promise<Payment> =>
  (await api.post<Payment>("/api/payment/verify/", { authority })).data;
```

Catalogue and detail calls are public. Create, history, and payment verification use
the existing `Authorization: Bearer <access-token>` flow. Registration also requires
a verified email; otherwise it returns HTTP 403 / error code `2007`.

## Catalogue behavior

| Offering type | Default capacity | Default price | Flags |
| --- | ---: | ---: | --- |
| `ONLINE_PRESENTATION` | 250 | 85,000 Toman | online |
| `OFFLINE_PRESENTATION` | Unlimited | 60,000 Toman | neither online nor onsite |
| `IN_PERSON_WORKSHOP` | 125 | 125,000 Toman | onsite |
| `ONLINE_WORKSHOP` | 80 | 85,000 Toman | online |

These are creation defaults, not frontend constants. Always render API values because
administrators may override finite offerings. Offline presentations are always
unlimited in this release.

- `IRT` means Iranian Toman. Format the number as Toman; do not multiply by ten.
- When `is_unlimited` is true, both capacity fields are `null`. Show “Unlimited” (or
  the established localized equivalent), never zero/sold-out/unavailable.
- A finite item is sold out only when
  `!is_unlimited && remained_capacity === 0`. Keep it visible and disable purchase.
- Capacity can change after loading. Handle API conflicts and refresh data.
- The list is ordered by `start_date`, then `id`.

## Registration lifecycle

`POST /api/presentations/register/` returns HTTP 200 with the complete registration,
not a wrapper. It can immediately progress beyond `QUEUED` when approval is disabled.

| Status | Meaning and frontend action |
| --- | --- |
| `SUBMITTED` | Legacy/transient; show submitted/pending. |
| `RESERVED` | Capacity was full: waitlisted, no payment link, and no held seat. |
| `QUEUED` | Capacity exists but administrator approval is pending. |
| `APPROVED` | Seat held and payment pending. Use non-empty `payment_link`. |
| `FINAL` | Paid/finalized, or a free registration; show owned/completed. |
| `REJECTED` | Show `rejection_reason` when non-empty. |
| `CANCELLED` | Payment failed/cancelled; seat released and link cleared. |

Use the create response immediately. If it is `APPROVED` with `payment_link`, follow
the established redirect behavior. Otherwise show its status and refresh history as
normal. Prefer `total_amount` for a historical registration cost; do not recompute it
from the course's current price. New individual registrations have no child items.

## Payment return flow

Keep the existing gateway experience with these contracts:

1. The backend callback verifies server-side and redirects to the configured frontend
   result route with `authority` and lowercase `status` (`successful`, `failed`, or
   `pending`). A malformed callback can return only `status=invalid_callback`.
2. With an authority and authenticated user, call `POST /api/payment/verify/` using
   `{ authority }`. It is safe for an already processed payment and now returns
   `currency` with `amount`.
3. Treat returned `Payment.status` as authoritative and refresh registration history.
   Success finalizes it; definitive failure cancels an approved registration.
4. A transient gateway/network error can leave it `PENDING`; do not show false failure.

Never construct a Zarinpal URL; use `payment_link`. The legacy retry route
`GET /api/payment/startpay/?authority=...` remains, but new UI should retain it only
if the current frontend already uses it.

## Errors to handle

Failures use `{ errorCode, errorMessage }`. With Axios, narrow via
`axios.isAxiosError<ApiErrorBody>(error)` and read `error.response?.data`.

| HTTP | Code | Meaning |
| ---: | ---: | --- |
| 400 | `2110` | Package/child selection is unavailable. |
| 403 | `2007` | Verified-email login required. |
| 404 | `1004` | Offering ID/slug not found or not public. |
| 409 | `2100` | Already approved/finalized. |
| 409 | `2106` | User already owns the offering. |
| 409 | `2108` | Not available for individual purchase. |
| 409 | `2109` | Capacity disappeared during processing; refresh. |
| 404 | `2208` | Payment authority not found for this user. |

Do not depend on exact English messages; map known codes and fall back to
`errorMessage`. Posting `child_ids` is rejected even when it is empty.

## Completion checklist

- Replace the old catalogue source with `GET /api/presentations/offerings/` and keep
  slug details on `GET /api/presentations/course/{slug}/`.
- Update types, schemas, Axios mocks, fixtures, copy, and all request call sites.
- Remove package UI and every new-purchase `children`/`child_ids` dependency.
- Support all four types; do not infer offering type from `online`/`onsite`.
- Test Toman formatting, unlimited `null`, finite zero, raw arrays, auth/errors,
  immediate payment links, every registration/payment status, and legacy read-only
  history.
- Run unit/integration tests, lint, and TypeScript checks. Report changed files,
  results, and compatibility concerns. Do not commit or push unless requested.
