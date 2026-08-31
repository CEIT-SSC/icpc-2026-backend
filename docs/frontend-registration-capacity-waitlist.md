# Frontend handoff: registration capacity and waitlist

این سند تغییرات لازم فرانت برای جریان جدید ظرفیت ثبت‌نام و صف انتظار را مشخص
می‌کند. این موارد، بخش‌های مرتبط با approval و sold-out در
`frontend-individual-offerings-prompt.md` را جایگزین می‌کنند.

## خلاصه تغییر رفتار

- تأیید دستی ادمین حذف شده است. وقتی ظرفیت وجود دارد، کاربر بلافاصله امکان
  پرداخت می‌گیرد.
- پر بودن ظرفیت نباید دکمه ثبت‌نام را غیرفعال کند؛ کاربر باید بتواند وارد صف
  انتظار شود.
- صف انتظار FIFO است و API موقعیت فعلی کاربر را در `waitlist_position` برمی‌گرداند.
- بعد از آزاد شدن ظرفیت، بک‌اند کاربر را ارتقا می‌دهد، لینک پرداخت می‌سازد و
  ایمیل اطلاع‌رسانی ارسال می‌کند.

## تغییر Type و Schema

نوع registration باید فیلد زیر را داشته باشد:

```ts
type RegistrationStatus =
  | "SUBMITTED"
  | "RESERVED"
  | "QUEUED"
  | "APPROVED"
  | "FINAL"
  | "REJECTED"
  | "CANCELLED";

type Registration = {
  // existing fields...
  status: RegistrationStatus;
  payment_link: string;
  waitlist_position: number | null;
};
```

فیلد `requires_approval` برای سازگاری قبلی در پاسخ offering باقی مانده، اما
همیشه `false` است. هیچ متن، badge یا مرحله‌ای با مفهوم «در انتظار تأیید ادمین»
نمایش داده نشود.

## رفتار صفحه Offering

اطلاعات offering همچنان از این endpoint دریافت می‌شود:

```http
GET /api/presentations/offerings/
```

- اگر `is_unlimited` برابر `true` است، ظرفیت به صورت نامحدود نمایش داده شود.
- اگر `remained_capacity > 0` است، CTA معمول ثبت‌نام نمایش داده شود.
- اگر `remained_capacity === 0` است، offering همچنان قابل انتخاب باشد و CTA با
  متنی مانند «ورود به صف انتظار» نمایش داده شود.
- صفر بودن ظرفیت فقط به معنی ورود به صف است و نباید به صورت «ثبت‌نام بسته است»
  تفسیر شود؛ مگر اینکه `is_active` برابر `false` باشد یا API خطای unavailable
  برگرداند.

## رفتار ثبت‌نام

درخواست ثبت‌نام تغییری در payload ندارد:

```http
POST /api/presentations/register/
Content-Type: application/json

{
  "course_id": 123,
  "extra_answers": {}
}
```

پس از پاسخ، UI باید بر اساس وضعیت عمل کند:

| وضعیت | رفتار فرانت |
| --- | --- |
| `RESERVED` | پیام ورود موفق به صف و `waitlist_position` را نمایش بده؛ دکمه پرداخت نمایش داده نشود. |
| `APPROVED` | کاربر واجد پرداخت است؛ اگر `payment_link` وجود دارد CTA پرداخت نمایش بده. |
| `FINAL` | ثبت‌نام نهایی/پرداخت‌شده نمایش داده شود؛ CTA پرداخت پنهان باشد. |
| `CANCELLED` | شکست یا لغو پرداخت نمایش داده شود؛ برای تلاش دوباره از جریان موجود پروژه استفاده شود. |
| `QUEUED` یا `SUBMITTED` | فقط به عنوان وضعیت گذرای سازگاری در نظر گرفته و داده registration دوباره fetch شود؛ متن تأیید ادمین نمایش داده نشود. |
| `REJECTED` | دلیل رد موجود، در صورت وجود، نمایش داده شود. |

نمونه پاسخ صف انتظار:

```json
{
  "id": 42,
  "status": "RESERVED",
  "waitlist_position": 3,
  "payment_link": ""
}
```

نمونه پاسخ آماده پرداخت:

```json
{
  "id": 42,
  "status": "APPROVED",
  "waitlist_position": null,
  "payment_link": "https://payment.example/start/..."
}
```

ارسال دوباره درخواست توسط کاربری که قبلاً در صف است، همان registration را
برمی‌گرداند و جای او را تغییر نمی‌دهد. UI می‌تواند پاسخ را مثل یک عملیات موفق
و idempotent نمایش دهد.

## نمایش و به‌روزرسانی صف

registrationهای کاربر از endpoint فعلی خوانده می‌شوند:

```http
GET /api/presentations/me/registrations/
```

- برای `RESERVED`، متن واضحی مانند «موقعیت فعلی شما در صف: ۳» نمایش داده شود.
- `waitlist_position` فقط برای `RESERVED` عدد دارد و برای وضعیت‌های دیگر `null`
  است.
- موقعیت صف پویاست؛ پس مقدار cacheشده به عنوان مقدار دائمی نگهداری نشود.
- در ورود به صفحه، بازگشت focus به تب، و پس از بازگشت از پرداخت، registrationها
  دوباره fetch شوند تا ارتقا از صف به `APPROVED` یا `FINAL` دیده شود.
- بک‌اند برای ارتقا ایمیل می‌فرستد؛ فرانت نیازی به ارسال ایمیل یا اجرای promotion
  ندارد.

## پرداخت

- لینک پرداخت فقط وقتی نمایش داده شود که `status === "APPROVED"` و
  `payment_link` خالی نباشد.
- برای `RESERVED` هرگز مسیر پرداخت آغاز نشود؛ این وضعیت هنوز slot رزروشده ندارد.
- جریان callback و verify فعلی پرداخت حفظ شود.
- بعد از پرداخت موفق، registration باید دوباره fetch و وضعیت `FINAL` نمایش داده
  شود.

## حالت‌های UI پیشنهادی

- ظرفیت موجود: «ثبت‌نام و پرداخت»
- ظرفیت پر: «ورود به صف انتظار»
- در صف: «موقعیت شما در صف: N»
- ارتقایافته: «ظرفیت برای شما باز شده؛ پرداخت را تکمیل کنید»
- نهایی: «ثبت‌نام نهایی شد»

متن نهایی باید با لحن و سیستم ترجمه موجود فرانت هماهنگ شود.

## تست‌های لازم فرانت

- ثبت‌نام با ظرفیت موجود و نمایش مستقیم CTA پرداخت.
- ثبت‌نام در offering پر و نمایش موقعیت صف.
- فعال بودن CTA ورود به صف وقتی `remained_capacity` صفر است.
- عدم نمایش پرداخت برای `RESERVED`.
- نمایش پرداخت برای `APPROVED` و عدم نمایش آن برای `FINAL`.
- تغییر UI از `RESERVED` به `APPROVED` پس از refetch.
- تغییر موقعیت صف پس از refetch.
- عدم نمایش approval دستی حتی اگر fixture قدیمی `requires_approval: true` داشته
  باشد.
- رفتار درست `null` برای `waitlist_position`.
- حفظ جریان موجود callback/verify پرداخت.

## نکات سازگاری

- مقدار دیتابیسی `RESERVED` در UI باید «در صف انتظار» ترجمه شود، نه «رزرو شده».
- فرانت نباید ظرفیت را محلی محاسبه یا slot اختصاص دهد؛ پاسخ بک‌اند منبع حقیقت
  است.
- در raceها ممکن است `remained_capacity` روی کارت قدیمی باشد؛ نتیجه نهایی همان
  registration برگشتی از `POST /register/` است.
- هیچ تغییری در payload مربوط به `child_ids` ایجاد نشود؛ خرید package همچنان
  غیرفعال است.
