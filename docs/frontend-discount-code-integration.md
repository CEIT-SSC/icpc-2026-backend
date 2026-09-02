# Frontend integration: bundle discount codes

This document defines how the frontend validates and redeems discount codes for
ICPC 2026 bundle registrations, and how the resulting price flows into the
existing on-demand payment process.

It complements
[`frontend-bundle-integration.md`](frontend-bundle-integration.md) and
[`frontend-on-demand-payment-flow.md`](frontend-on-demand-payment-flow.md).
The registration response and payment response are always authoritative; the
frontend must not calculate or persist its own discounted amount.

## Flow summary

1. The user selects one of the three bundles.
2. The frontend may validate a code to show a price preview.
3. The frontend sends the same code in `POST /api/presentations/register/`.
4. Registration atomically reserves the code and snapshots the final price.
5. If the final price is greater than zero, the user explicitly starts payment
   using the registration ID. No discount code is sent to the payment endpoint.
6. If the final price is zero, the registration becomes `FINAL` immediately and
   there is no payment step.

Discount validation is only a preview. It neither reserves nor consumes the
code, so registration can still reject a limited code if another user consumes
the last use between the two requests.

## TypeScript contracts

```ts
export interface DiscountValidationRequest {
  course_id: number;
  code: string;
}

export interface DiscountValidationResponse {
  valid: true;
  code: string; // normalized uppercase code
  original_price: number;
  final_price: number;
}

export interface CreateBundleRegistrationRequest {
  course_id: number;
  discount_code?: string;
  extra_answers?: Record<string, unknown>;
}

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
  price: number | null;
  discount_code: string | null;
  total_amount: number;
  currency: "IRT";
  status: RegistrationStatus;
  waitlist_position: number | null;
  payment_link: string;
  // Preserve the existing course, items, user, and timestamp fields.
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

Amounts use Iranian Toman (`IRT`). Do not multiply them by ten.

## 1. Validate a code for preview

This endpoint requires the existing Bearer access token:

```http
POST /api/presentations/discount/validate/
Authorization: Bearer <access-token>
Content-Type: application/json

{
  "course_id": 12,
  "code": "launch25"
}
```

A valid code returns HTTP `200`:

```json
{
  "valid": true,
  "code": "LAUNCH25",
  "original_price": 599000,
  "final_price": 449250
}
```

The endpoint accepts only an active bundle ID. It does not accept an individual
member course. Codes are trimmed and matched without case sensitivity; the
response contains the normalized uppercase value.

Axios example:

```ts
const validateBundleDiscount = async (
  courseId: number,
  code: string,
): Promise<DiscountValidationResponse> => {
  const response = await api.post<DiscountValidationResponse>(
    "/api/presentations/discount/validate/",
    { course_id: courseId, code },
  );
  return response.data;
};
```

Use `final_price` only as a preview. Do not update registration state or enable
owned/paid UI after validation.

## 2. Redeem the code during registration

Send the code in the existing authenticated bundle registration request:

```http
POST /api/presentations/register/
Authorization: Bearer <access-token>
Content-Type: application/json

{
  "course_id": 12,
  "discount_code": "LAUNCH25",
  "extra_answers": {}
}
```

`discount_code` is optional. Omit it or send `""` when the user is not using a
code. Do not send `null`, and do not send `child_ids`.

Axios example:

```ts
const createBundleRegistration = async (
  payload: CreateBundleRegistrationRequest,
): Promise<Registration> => {
  const response = await api.post<Registration>(
    "/api/presentations/register/",
    payload,
  );
  return response.data;
};

const registerWithDiscount = async (courseId: number, enteredCode: string) => {
  const discountCode = enteredCode.trim();
  return createBundleRegistration({
    course_id: courseId,
    ...(discountCode ? { discount_code: discountCode } : {}),
  });
};
```

A paid registration with capacity returns `APPROVED`. Its `price` and
`total_amount` contain the final snapshotted amount:

```json
{
  "id": 42,
  "price": 449250,
  "discount_code": "LAUNCH25",
  "total_amount": 449250,
  "currency": "IRT",
  "status": "APPROVED",
  "waitlist_position": null,
  "payment_link": ""
}
```

Treat this response as authoritative even when it differs from an earlier
validation preview. The backend applies the code, reserves one use, and stores
the discounted price in the registration snapshot within one transaction.

## Registration and waitlist behavior

- `APPROVED`: the discounted seat is held. Show **Pay now** when
  `total_amount > 0`.
- `RESERVED`: the user is waitlisted. The discounted price and code are already
  snapshotted, but payment must not start until promotion to `APPROVED`.
- `FINAL`: registration is complete. A discount that reduces the amount to zero
  reaches this status immediately.
- `CANCELLED`: preserve the displayed `price` and `discount_code`. A payment retry
  uses the same snapshot if capacity can be reclaimed fairly.

Repeated registration by a user who is already `RESERVED` returns the same row
without changing its FIFO timestamp, price, bundle-member snapshot, or discount
reservation. Do not use another registration request to change the code while
the user is waitlisted.

## 3. Start payment with the snapshotted price

For an `APPROVED` registration whose `total_amount` is greater than zero, call:

```http
POST /api/presentations/me/registrations/42/payment/
Authorization: Bearer <access-token>
Content-Length: 0
```

Do not send a body, `course_id`, amount, or discount code. The backend reads the
registration snapshot and creates the gateway payment for exactly that amount.

```ts
const startRegistrationPayment = async (
  registrationId: number,
): Promise<RegistrationPaymentStart> => {
  const response = await api.post<RegistrationPaymentStart>(
    `/api/presentations/me/registrations/${registrationId}/payment/`,
  );
  return response.data;
};
```

Example HTTP `201` response:

```json
{
  "registration_id": 42,
  "payment_id": 91,
  "authority": "A000000000000000000000000000001",
  "payment_link": "https://payment.zarinpal.com/pg/StartPay/A000000000000000000000000000001",
  "amount": 449250,
  "currency": "IRT",
  "status": "PENDING"
}
```

The payment response's `amount` should equal the registration's
`total_amount`. Redirect only to the returned `payment_link`; never construct the
gateway URL in frontend code.

If `total_amount === 0`, the registration is already `FINAL`. Hide the payment
button and do not call the payment-start endpoint; it will reject the request.

## Gateway return and retries

The callback and verification flow is unchanged by discounts:

1. Zarinpal redirects through the backend callback.
2. The backend verifies the payment server-side.
3. The frontend refreshes
   `GET /api/presentations/me/registrations/` after the callback or explicit
   verification.
4. A successful payment changes the registration to `FINAL`.
5. A definitive failure changes it to `CANCELLED` and releases its bundle seats.

To retry a `CANCELLED` registration, call the same payment-start endpoint with no
body. Do not validate or resend the discount code. The backend preserves the
original discounted price and first checks whether capacity can be reclaimed
without bypassing the waitlist.

## Discount errors

All API errors use `{ errorCode, errorMessage }`.

| HTTP | `errorCode` | Meaning / frontend behavior |
| --- | ---: | --- |
| `400` | `2400` | Code was not found or is inactive. Clear the preview and show the API message. |
| `400` | `2401` | Code has not started yet or has expired. Clear the preview. |
| `400` | `2402` | Global usage limit has been reached. Clear the preview and prevent submission with that code. |
| `400` | `2403` | This user already redeemed the code on another registration. Remove the code and let them continue without it. |
| `400` | `2404` | Code does not apply to the selected bundle. Keep the selected bundle and clear the preview. |
| `401` | `1002` | Authentication is missing or expired. Re-authenticate. |
| `404` | `1004` | The selected bundle does not exist or is not currently purchasable. Refresh the catalogue. |
| `409` | `2100` | The user already has an approved or final registration for this bundle. Refresh registration history. |

Validation errors and redemption errors must be shown using `errorMessage`; do
not duplicate discount validity rules in frontend code. A successful validation
does not guarantee successful redemption, so keep the registration submit error
path visible and usable.

## Suggested UI state

Keep the entered code, its preview, and its request state separate:

```ts
type DiscountUiState = {
  input: string;
  validating: boolean;
  preview: DiscountValidationResponse | null;
  error: string | null;
};
```

- Clear the preview when the selected bundle or input changes.
- Disable **Apply code** while validation is in flight.
- Disable the registration CTA while registration submission is in flight.
- Do not mark a code as redeemed until registration succeeds.
- Render the final amount from the returned registration, not the preview.
- Keep payment-button state separate from discount-validation state.

## Acceptance cases

1. Valid percentage code previews and registers at the discounted amount.
2. Valid fixed-amount code never produces a negative final price.
3. Lowercase or whitespace-padded input returns a normalized uppercase code.
4. Invalid, inactive, expired, exhausted, and wrong-bundle codes show their API
   errors without creating a registration.
5. A code used by the same user on another bundle is rejected during
   registration.
6. The last globally available use can disappear after preview; registration
   handles `2402` without trusting stale preview state.
7. A waitlisted registration keeps its discounted snapshot after promotion.
8. A zero-price result becomes `FINAL` and never starts payment.
9. A paid result starts payment without sending the amount or code, and the
   gateway response amount matches `registration.total_amount`.
10. A failed-payment retry preserves the original discounted amount and still
    respects bundle capacity and FIFO priority.
