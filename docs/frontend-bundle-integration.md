# Frontend integration contract: ICPC 2026 bundles

This document describes the required Axios + TypeScript frontend migration from
individual presentation/workshop purchases to three all-access bundles. Preserve
the frontend's existing Axios instance, JWT refresh/interceptors, routes,
localization, visual system, error presentation, registration history, FIFO
waitlist behavior, and on-demand payment flow.

This document supersedes the individual-purchase parts of
`frontend-individual-offerings-prompt.md`. The waitlist behavior in
`frontend-registration-capacity-waitlist.md` and the payment behavior in
`frontend-on-demand-payment-flow.md` still apply, with a bundle as the purchased
product.

## Product behavior

The public catalogue contains exactly three indivisible products:

| `bundle_type` | Category | Delivery | Price |
| --- | --- | --- | ---: |
| `ALL_ONLINE_PRESENTATIONS` | Presentation | Online | 599,000 Toman |
| `ALL_IN_PERSON_WORKSHOPS` | Workshop | In person | 419,000 Toman |
| `ALL_ONLINE_WORKSHOPS` | Workshop | Online | 299,000 Toman |

Render price and capacity from the API rather than duplicating these values as
frontend business logic. `IRT` is Iranian Toman; do not multiply the API amount
by ten.

Users never select individual members. Remove per-presentation/workshop purchase
buttons, child checkboxes, subset selection state, per-member totals, and every
new-purchase `child_ids` field. A registration request contains only the selected
bundle's `course_id` and optional extra answers.

## TypeScript contracts

Adapt property placement to the existing frontend structure, but preserve these
wire values and nullable legacy fields.

```ts
export type BundleType =
  | "ALL_ONLINE_PRESENTATIONS"
  | "ALL_IN_PERSON_WORKSHOPS"
  | "ALL_ONLINE_WORKSHOPS";

export type BundleCategory = "PRESENTATION" | "WORKSHOP";
export type DeliveryMode = "ONLINE" | "IN_PERSON";

export type OfferingType =
  | "ONLINE_PRESENTATION"
  | "OFFLINE_PRESENTATION"
  | "IN_PERSON_WORKSHOP"
  | "ONLINE_WORKSHOP";

export interface Presenter {
  id: number;
  full_name: string;
  bio: string;
  email: string;
  website: string;
}

export interface ScheduleRule {
  weekday: 0 | 1 | 2 | 3 | 4 | 5 | 6; // Monday = 0
  weekday_display: "Mon" | "Tue" | "Wed" | "Thu" | "Fri" | "Sat" | "Sun";
  start_time: string;
  end_time: string;
}

export interface BundleMember {
  id: number;
  name: string;
  subtitle: string;
  description: string;
  slug: string;
  offering_type: OfferingType;
  offering_type_display: string;
  online: boolean;
  onsite: boolean;
  presenters: Presenter[];
  capacity: number | null;
  remained_capacity: number | null;
  is_unlimited: boolean;
  price: number | null;
  currency: "IRT";
  is_active: boolean;
  schedule: ScheduleRule[];
}

export interface Bundle {
  id: number;
  name: string;
  subtitle: string;
  description: string;
  slug: string;
  start_date: string | null;
  bundle_type: BundleType;
  bundle_type_display: string;
  category: BundleCategory;
  delivery_mode: DeliveryMode;
  online: boolean;
  onsite: boolean;
  classes_count: number;
  price: number;
  currency: "IRT";
  capacity: number | null;
  remained_capacity: number | null;
  is_unlimited: boolean;
  member_count: number;
  members: BundleMember[];
  schedule: ScheduleRule[];
  requires_approval: false;
  is_active: true;
}

// Registration history contains old individual products as well as new bundles.
export interface HistoricalProduct {
  id: number;
  name: string;
  subtitle: string;
  description: string;
  slug: string;
  bundle_type: BundleType | null;
  bundle_type_display: string | null;
  category: BundleCategory | null;
  delivery_mode: DeliveryMode | null;
  offering_type: OfferingType | null;
  offering_type_display: string | null;
  price: number | null;
  currency: "IRT";
  capacity: number | null;
  remained_capacity: number | null;
  is_unlimited: boolean;
  schedule: ScheduleRule[];
  members?: BundleMember[];
}

export type RegistrationStatus =
  | "SUBMITTED"
  | "RESERVED"
  | "QUEUED"
  | "APPROVED"
  | "FINAL"
  | "REJECTED"
  | "CANCELLED";

export interface RegistrationItem {
  id: number;
  child: BundleMember;
  price: number; // zero for new bundle member snapshots
  created_at: string;
}

export interface Registration {
  id: number;
  course: HistoricalProduct;
  user: number;
  price: number | null; // immutable purchase-price snapshot
  currency: "IRT";
  status: RegistrationStatus;
  waitlist_position: number | null;
  resume_url: string;
  payment_link: string;
  rejection_reason: string;
  submitted_at: string;
  decided_at: string | null;
  items: RegistrationItem[];
  total_amount: number;
}

export interface CreateBundleRegistrationRequest {
  course_id: number;
  extra_answers?: Record<string, unknown>;
}

export interface RegistrationPaymentStart {
  registration_id: number;
  payment_id: number;
  authority: string;
  payment_link: string;
  amount: number;
  currency: "IRT";
  status: "PENDING";
}

export interface ApiErrorBody {
  errorCode: number;
  errorMessage: string;
}
```

Do not model history as `Bundle[]`: old individual records legitimately have a
null `bundle_type`, and old package records may contain legacy items. Use the
bundle type only when it is non-null.

## Axios calls

Paths are relative to the existing API base URL. List responses are raw arrays,
not pagination wrappers.

```ts
const listBundles = async (): Promise<Bundle[]> =>
  (await api.get<Bundle[]>("/api/presentations/offerings/")).data;

const getBundle = async (slug: string): Promise<Bundle> =>
  (await api.get<Bundle>(
    `/api/presentations/course/${encodeURIComponent(slug)}/`,
  )).data;

const createBundleRegistration = async (
  payload: CreateBundleRegistrationRequest,
): Promise<Registration> =>
  (await api.post<Registration>("/api/presentations/register/", payload)).data;

const listMyRegistrations = async (): Promise<Registration[]> =>
  (await api.get<Registration[]>(
    "/api/presentations/me/registrations/",
  )).data;

const startRegistrationPayment = async (
  registrationId: number,
): Promise<RegistrationPaymentStart> =>
  (await api.post<RegistrationPaymentStart>(
    `/api/presentations/me/registrations/${registrationId}/payment/`,
  )).data;
```

Do not send a body to the payment-start endpoint. Catalogue/detail are public;
registration, history, and payment start use the existing Bearer access token.
Creating a registration also requires a verified email.

## Catalogue layout

Use explicit API metadata; never classify products by translated names, IDs,
slugs, member counts, or `online`/`onsite` guesses.

1. Render the presentation bundle where `category === "PRESENTATION"`.
2. Render a workshop section with a two-button segmented control:
   - **In person** selects `category === "WORKSHOP" && delivery_mode === "IN_PERSON"`.
   - **Online** selects `category === "WORKSHOP" && delivery_mode === "ONLINE"`.
3. Keep the selected workshop mode in component state (and optionally in a URL
   query parameter if that matches the existing routing style). Do not use two
   independent booleans that can both be selected.
4. Use `bundle_type` as the stable analytics/test identifier. Do not hard-code
   database IDs.
5. Display `members` as an included, read-only list. There are no checkboxes and
   clicking a member must not create an individual registration.

Expected schedules are Sunday and Tuesday 17:00-20:00 for presentations and
Thursday 09:00-12:00 for workshops. Render the API's `schedule` values so future
admin changes do not require a frontend deployment. Remember that Tuesday is
weekday `1`, Thursday is `3`, and Sunday is `6`; sort for display explicitly if
the desired localized order differs from numeric weekday order.

## Capacity UI

The backend has already computed the all-members bottleneck:

- Show “Unlimited” only when `is_unlimited` is true; in that case both capacity
  fields are null.
- For a finite bundle, show `remained_capacity` exactly as returned. Do not sum,
  average, or independently take a minimum over `members` in frontend code.
- `remained_capacity === 0` means the bundle is currently full, but the existing
  FIFO waitlist is still available. Keep the CTA enabled and label it with the
  localized equivalent of “Join waitlist”.
- Capacity may change after the catalogue was loaded. The registration response
  and subsequent refetch are authoritative.

## Registration and history behavior

Send exactly one bundle ID:

```json
{
  "course_id": 101,
  "extra_answers": {}
}
```

Never send `child_ids`, member IDs, a price, a capacity, `bundle_type`, or
delivery metadata. The response contains the server-created item snapshot for
all bundle members.

Keep the existing status UI:

| Status | Frontend behavior |
| --- | --- |
| `RESERVED` | Show FIFO waitlist state and `waitlist_position`; no payment CTA. |
| `APPROVED` | Show **Pay now**, even when `registration.payment_link` is empty. |
| `FINAL` | Show bundle ownership/completion; no payment CTA. |
| `CANCELLED` | Show failed/cancelled state and allow the established deliberate retry. |
| `SUBMITTED` / `QUEUED` | Treat as transitional and refetch; do not show manual-approval copy. |
| `REJECTED` | Show `rejection_reason` when present; no payment CTA. |

After create, update the cache/history with the returned registration. Do not
redirect automatically. Payment begins only when the user deliberately clicks
the CTA and the payment-start endpoint returns HTTP 201; redirect to that
response's `payment_link`. Disable the clicked button while the request is in
flight. Never construct or persist a Zarinpal URL.

On the user's registration/history page:

- group or label new purchases by `course.bundle_type` so the three buyer groups
  remain distinct;
- use `registration.total_amount` (or its `price` snapshot), never the product's
  current price;
- show the snapshotted `items` for a bundle registration as read-only access
  contents; and
- continue to render legacy individual registrations with
  `course.bundle_type === null`. Do not relabel a historical partial purchase as
  an all-access bundle and do not hide it merely because it is absent from the
  public catalogue.

## Payment return and refresh

The existing callback contract is unchanged. The backend redirects to the
frontend result route with lowercase `status` and, when available, `authority`.
Use the existing authenticated `POST /api/payment/verify/` call when appropriate,
treat its returned status as authoritative, and then refetch registration
history and catalogue capacity. A successful payment makes the whole bundle
registration `FINAL`; definitive failure makes it `CANCELLED` and releases all
member seats. A transient verification error may remain `PENDING` and must not be
shown as a false failure.

## Errors

Continue to narrow Axios errors with
`axios.isAxiosError<ApiErrorBody>(error)` and use
`error.response?.data.errorCode`. Important existing codes include:

| HTTP | Code | Meaning/action |
| ---: | ---: | --- |
| 400 | `2110` | Member/child selection is not accepted; fix the client request. |
| 403 | `2007` | Verified-email login required. |
| 404 | `1004` | Bundle ID or slug is not public/found; refresh catalogue. |
| 409 | `2108` | The submitted ID is not a currently purchasable bundle. |
| 409 | `2100` / `2106` | Already approved/finalized or already owns this bundle. |
| 409 | `2109` | Bundle capacity/FIFO claim disappeared; refresh catalogue and history. |
| 409 | `2111` | Registration status is not eligible for payment; refresh history. |
| 404 | `2202` | Registration is not available to this user. |
| 409 | `2200` / `2207` | Gateway initiation failed; stay on page and allow a deliberate retry. |
| 409 | `2205` | A previous payment succeeded; refresh history. |

Do not depend on exact English error messages. Show `errorMessage` only as a
fallback for unknown codes.

## Frontend acceptance checklist

- Catalogue renders one presentation bundle and one workshop selected by the
  mutually exclusive in-person/online buttons.
- No new-purchase code path contains `child_ids` or an individual member
  registration CTA.
- Each bundle shows the server price, member list, schedule, and bottleneck
  remaining capacity.
- Zero capacity offers waitlisting rather than disabling the product.
- Register sends one `course_id` and handles `RESERVED`, `APPROVED`, and `FINAL`
  without an automatic gateway redirect.
- `APPROVED` always exposes the on-demand pay button; `RESERVED` never does.
- Callback/verification refetches both registrations and capacities.
- History clearly separates all three new bundle types and still shows old
  individual/legacy rows accurately.
- Tests/fixtures/mocks cover raw array responses, all three bundle types, both
  workshop button states, Toman formatting, schedule weekday mapping, finite
  zero, unlimited null, every registration status, payment retry, and a legacy
  row with `bundle_type: null`.
