# WAHA production setup

Kwik Klin can run the application WhatsApp transport through self-hosted WAHA.

## App environment

Set:

- `WHATSAPP_PROVIDER=waha`
- `WAHA_BASE_URL=http://127.0.0.1:3000` when WAHA is published on the same EC2 host
- `WAHA_API_KEY=...` when WAHA API-key protection is enabled
- `WAHA_SESSION=default`
- `WAHA_WEBHOOK_SECRET=<long-random-secret>`

There is deliberately no Meta 24-hour window/template check in WAHA mode.

## WAHA webhook

Configure the WAHA session webhook to:

`https://<your-app-domain>/webhook/waha`

Subscribe to:

- `message`
- `message.ack`

If `WAHA_WEBHOOK_SECRET` is set, configure the same HMAC key in the WAHA webhook config.

WAHA sends `message` payloads containing `from`, `body`, `id`, and media information. The application normalizes them into the existing inbox/AI flow. `message.ack` is mapped to sent/delivered/read/failed so the dashboard can show delivery progress.

## Campaigns

The Campaigns screen has **AI Create Offer**.

The model receives:

- live RFM segment size
- customer value/order count
- previous campaign status counts
- current season
- active rate card
- maximum discount guardrail

The model returns an offer, coupon suggestion, WhatsApp copy, Google Business copy and creative brief. Code clamps discount/validity and strips phone numbers from Google Business copy.

The generated Google creative is rendered with the real Kwik Klin logo and **without a phone number**. It is stored as one campaign creative. When the owner approves the campaign, the same image+caption is sent to eligible customers one by one at the existing 1-second pace. No new image is generated per customer.

Nothing is sent merely by generating the draft; the owner still clicks **Save as draft** and then **Approve & send**.

## Google Analytics / Business Profile dashboard

Website realtime metrics use the GA4 Data API Realtime report. Google documents `activeUsers`, views and event counts for realtime reporting.

Set:

- `GA4_PROPERTY_ID=<numeric property id>`
- `GA4_REFRESH_TOKEN=<OAuth refresh token with analytics.readonly>`

The Google Business Profile card uses the Business Profile Performance API and shows daily/aggregated metrics such as website clicks, call clicks, directions and impressions. It does **not** pretend GBP has a per-second live feed.

The dashboard refreshes website realtime data every 30 seconds.

Before deploying, enable the Google Analytics Data API and Business Profile Performance API for the Google Cloud project and grant the Google account access to the relevant GA4 property/listing.
