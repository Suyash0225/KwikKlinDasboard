# Google Business Profile owner posting

- The daily GBP job runs at **10:00 AM Asia/Kolkata**.
- Daily posts use AI-generated text but reuse the latest image sent by the verified owner WhatsApp number. Automatic Gemini poster generation is disabled.
- To publish a manual post immediately, send a poster image with a caption from the configured owner/admin WhatsApp number.
- Prefix the caption with `CALL NOW:` to use Google's **Call now** CTA, or `WEBSITE:` / `VISIT WEBSITE:` to use **Visit website**. Without a prefix, the website CTA is used. Google supports one CTA per post, not both simultaneously.
- The image and caption are published to all selected GBP locations. The bot replies with each location's success/failure. A missing caption, invalid image, or unavailable public HTTPS image URL prevents publishing.
- Only the configured owner/admin phone is authorized. Staff and customer messages continue through the normal WhatsApp inbox flow.
- If the owner has not supplied a poster yet, the 10 AM job fails closed and reports a missing poster rather than publishing a text-only post.
