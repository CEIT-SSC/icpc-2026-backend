# Frontend integration: on-demand registration payments

This document is the complete frontend contract for the new course-registration
payment flow. It supersedes the payment-link instructions in the older individual
offering and capacity/waitlist documents.

For registrations using discount codes, also follow
[`frontend-discount-code-integration.md`](frontend-discount-code-integration.md).

## What changed

Creating or approving a paid registration no longer contacts the payment gateway.
The registration owns its seat and becomes `APPROVED`, but its `payment_link` is
empty. The frontend must create a gateway link only after the user presses the pay
button.

Use this new authenticated endpoint for that action:

```http
POST /api/presentations/me/registrations/{registration_id}/payment/
Authorization: Bearer <access-token>
Content-Length: 0
```

Do not send a request body. A successful request creates a `PENDING` payment and
returns HTTP `201`:

```json
{
  "registration_id": 42,
  "payment_id": 91,
  "authority": "A000000000000000000000000000001",
  "payment_link": "https://payment.zarinpal.com/pg/StartPay/A000000000000000000000000000001",
  "amount": 85000,
  "currency": "IRT",
  "status": "PENDING"
}
```

Redirect the browser to the returned `payment_link`. Never construct a Zarinpal
URL in frontend code.

## TypeScript contract

```ts
export type RegistrationStatus =
  | "SUBMITTED"
  | "RESERVED"
  | "QUEUED"
  | "APPROVED"
  | "FINAL"
  | "REJECTED"
  | "CANCELLED";

export interface Registration {
  id: number;
  status: RegistrationStatus;
  price: number | null;
  total_amount: number;
  currency: "IRT" | string;
  waitlist_position: number | null;
  payment_link: string;
  rejection_reason: string;
  // Keep the remaining existing course/items/timestamp fields unchanged.
}

export interface RegistrationPaymentStart {
  registration_id: number;
  payment_id: number;
  authority: string;
  payment_link: string;
  amount: number;
  currency: "IRT" | string;
  status: "PENDING";
}

export interface ApiError {
  errorCode: number;
  errorMessage: string;
}
```

`payment_link` remains in `Registration` for backward compatibility. It is `""`
when a registration is created or promoted and is populated after the explicit
payment-start request. The payment-start response is the authoritative source for
the redirect; do not require a non-empty registration `payment_link` to show the
pay button.

## Registration status behavior

| Status | Frontend behavior |
| --- | --- |
| `SUBMITTED` | Show a processing state. Do not offer payment. |
| `RESERVED` | The user is waitlisted and does not hold a seat. Show `waitlist_position`; do not offer payment. |
| `QUEUED` | Transitional processing state. Do not offer payment yet; refresh registration history. |
| `APPROVED` | The seat is held and payment is allowed. Show an enabled **Pay now** button even when `payment_link === ""`. |
| `FINAL` | Payment is complete, or the offering was free. Show the owned/registered state and no pay button. |
| `REJECTED` | Show `rejection_reason` and no pay button. |
| `CANCELLED` | The previous payment failed or was cancelled and its seat was released. A retry may be offered; the backend will reclaim capacity only if it is still fair to do so. |

Free registrations skip payment and become `FINAL` automatically. Waitlist
promotion changes `RESERVED` to `APPROVED` without generating a link; the user can
pay whenever they next choose to.

## Required frontend flow

### 1. Register without redirecting

Keep the existing registration request:

```http
POST /api/presentations/register/
Content-Type: application/json

{"course_id": 12, "extra_answers": {}}
```

Consume the returned registration immediately, but remove any code that redirects
to `registration.payment_link` after this response.

- `APPROVED`: show the registration and a **Pay now** CTA.
- `RESERVED`: show the FIFO waitlist state and position.
- `FINAL`: show success; this is normally a free offering.

### 2. Start payment only from a user action

```ts
const startRegistrationPayment = async (
  registrationId: number,
): Promise<RegistrationPaymentStart> => {
  const response = await api.post<RegistrationPaymentStart>(
    `/api/presentations/me/registrations/${registrationId}/payment/`,
  );
  return response.data;
};

const payNow = async (registration: Registration) => {
  if (registration.status !== "APPROVED" && registration.status !== "CANCELLED") {
    return;
  }

  setStartingPayment(registration.id, true);
  try {
    const payment = await startRegistrationPayment(registration.id);
    window.location.assign(payment.payment_link);
  } finally {
    setStartingPayment(registration.id, false);
  }
};
```

Disable the clicked button while the request is running so a double click cannot
create two gateway attempts. Do not prefetch this endpoint, invoke it while
rendering, or call it from a registration/history polling effect: it creates a
payment attempt and must represent deliberate user intent.

### 3. Handle the gateway return

The existing callback contract is unchanged:

1. Zarinpal calls `GET /api/payment/callback/?Authority=...&Status=...` on the
   backend.
2. The backend verifies or fails the payment server-side and redirects to the
   configured frontend payment-result route with lowercase `status` and, when
   available, `authority` query parameters.
3. If the frontend has an `authority` and an authenticated user, it may call the
   existing idempotent endpoint:

```http
POST /api/payment/verify/
Content-Type: application/json

{"authority": "A000000000000000000000000000001"}
```

4. Treat the returned payment `status` as authoritative, then refresh
   `GET /api/presentations/me/registrations/`.

A successful payment changes the registration to `FINAL`. A definitive failure
or gateway cancellation changes it to `CANCELLED` and releases its seat.

### 4. Retry behavior

The same new payment-start endpoint accepts a `CANCELLED` registration. The
backend first tries to reclaim the seat under the current capacity and FIFO rules.

- If the seat is available and no waitlisted registration has priority, a new
  payment link is returned.
- If capacity was taken or a waitlisted user has priority, the endpoint returns
  HTTP `409` with error code `2109`. Refresh registrations/capacity and explain
  that the seat is no longer available.

Do not use the legacy `GET /api/payment/startpay/?authority=...` route for new UI.
It remains only for compatibility with old payment links.

## Error handling

All API errors use `{ errorCode, errorMessage }`.

| HTTP | `errorCode` | Meaning / frontend action |
| --- | ---: | --- |
| `401` | `1002` | Session is missing or expired. Re-authenticate. |
| `404` | `2202` | The registration does not exist or belongs to another user. Refresh history; do not disclose ownership details. |
| `409` | `2111` | This status is not eligible for payment (for example `RESERVED`, `FINAL`, or `REJECTED`). Refresh history. |
| `409` | `2109` | A cancelled registration could not reclaim capacity or waitlist priority. Refresh capacity/history. |
| `400` | `2204` | The payment merchant is not configured. Show a temporary payment-unavailable message. |
| `409` | `2200` or `2207` | The gateway could not create the payment. Keep the user on the page and allow a deliberate retry. |
| `409` | `2205` | A previous payment for this registration was discovered as successful. Refresh registration history instead of starting another payment. |

On any payment-start error, do not mark the registration paid locally and do not
redirect. Clear the loading state and refetch registration history when the error
may reflect a status/capacity change.

## UI migration checklist

- Remove automatic redirects after `POST /api/presentations/register/`.
- Remove the condition `status === "APPROVED" && payment_link !== ""` from CTA
  rendering; `status === "APPROVED"` is sufficient.
- Add the authenticated payment-start API function and redirect only from its
  successful `201` response.
- Disable the pay button while payment start is in flight.
- Keep waitlisted and transitional states gateway-free.
- Refresh registration history after callback/verification.
- Support `CANCELLED` retry and handle capacity error `2109`.
- Do not persist or reuse payment links as durable frontend state.
- Do not use the legacy `startpay` endpoint in new code.

## End-to-end acceptance cases

1. Register for a paid offering with capacity: response is `APPROVED`,
   `payment_link` is empty, and no redirect occurs.
2. Reload or poll history before clicking pay: no gateway payment is created.
3. Click **Pay now**: one payment-start request returns `201`; redirect to its
   `payment_link`.
4. Join a full offering: response is `RESERVED`; no pay CTA and no gateway call.
5. Get promoted from the waitlist: status becomes `APPROVED`, link stays empty,
   and the pay CTA becomes available.
6. Complete payment: callback/verification leads to registration `FINAL`.
7. Cancel payment: registration becomes `CANCELLED`; retry succeeds only if the
   backend can reclaim capacity fairly.
8. Try another user's registration ID: the API returns `404`/`2202` and creates
   no payment.
