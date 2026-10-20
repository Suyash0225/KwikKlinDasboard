# Google Business Profile owner posting

- The daily GBP job runs at **10:15 AM Asia/Kolkata**.
- Daily posts use AI-generated text. A verified owner-supplied poster is preferred; if none is configured, the job generates one branded poster using Gemini artwork with a deterministic branded fallback. The same poster is reused across all selected locations for that day's run.
- To publish a manual post immediately, send a poster image with a caption from the configured owner/admin WhatsApp number.
- Prefix the caption with `CALL NOW:` to use Google's **Call now** CTA, or `WEBSITE:` / `VISIT WEBSITE:` to use **Visit website**. Without a prefix, the website CTA is used. Google supports one CTA per post, not both simultaneously.
- The image and caption are published to all selected GBP locations. The bot replies with each location's success/failure. A missing caption, invalid image, or unavailable public HTTPS image URL prevents publishing.
- Only the configured owner/admin phone is authorized. Staff and customer messages continue through the normal WhatsApp inbox flow.
- If poster generation and the public HTTPS media URL both fail, the job fails closed rather than publishing a text-only post. Google API failures include bounded response diagnostics in server logs; do not blindly retry a POST because the result can be ambiguous.
