"""Website ke service pages (local SEO) — /dry-cleaning-varanasi jaise.

Har page ek asli search ka jawab hai ("dry cleaning in Varanasi", "laundry
service near me Banaras"). Yahan sirf metadata + FAQ; page ka lamba content
app/site/templates/services/<slug>.html mein, daam CRM rate card se
(site_rates.category_block). FAQ yahin ek jagah — HTML aur JSON-LD (FAQPage)
dono isi se bante hain, taaki dono kabhi alag na hon.
"""

SERVICES: dict[str, dict] = {
    "dry-cleaning-varanasi": {
        "name": "Dry Cleaning",
        "title": "Dry Cleaning in Varanasi (Banaras) – Suits, Sarees, Lehengas",
        "description": "Professional dry cleaning in Varanasi with free doorstep pickup. Suits, blazers, silk sarees, sherwanis, lehengas and woollens. See prices and book on WhatsApp.",
        "h1": "Dry cleaning in Varanasi, picked up from your door",
        "lead": "Expert dry cleaning for suits, silk sarees, sherwanis, lehengas and woollens across Varanasi (Banaras) — tagged, cleaned by fabric type and delivered back neatly packed.",
        "rates": ["dry"],
        "faq": [
            ("How much does dry cleaning cost in Varanasi?",
             "Prices depend on the garment and are listed on this page from our live rate list. You get the full bill on WhatsApp before delivery."),
            ("Can you dry clean silk and Banarasi sarees?",
             "Yes. Silk and Banarasi sarees are dry cleaned with solvent care suited to delicate silk and zari work, and returned folded or on a hanger."),
            ("How long does dry cleaning take?",
             "Most dry cleaning orders are delivered within about 4 days of pickup. Heavy or embellished occasion wear can take up to 7 days."),
            ("Do you pick up dry cleaning from home?",
             "Yes. We pick up and deliver across Varanasi. Book on WhatsApp or with the form on our website."),
        ],
    },
    "laundry-service-varanasi": {
        "name": "Laundry Service",
        "title": "Laundry Service in Varanasi (Banaras) – Wash & Iron, Wash & Fold",
        "description": "Laundry service in Varanasi with doorstep pickup and delivery. Wash & iron per piece or wash & fold by kg. Clear prices, updates on WhatsApp.",
        "h1": "Laundry service in Varanasi with doorstep pickup",
        "lead": "Everyday laundry for homes, students and working professionals in Varanasi (Banaras) — wash & iron by the piece, or wash & fold by weight for the weekly family load.",
        "rates": ["wash", "kg"],
        "faq": [
            ("What is the difference between wash & iron and wash & fold?",
             "Wash & iron is charged per piece and each garment is ironed. Wash & fold is charged by weight and clothes are washed, dried and neatly folded — ideal for daily wear and bulk loads."),
            ("Do you offer laundry pickup near BHU and Lanka?",
             "Yes. We pick up and deliver across Varanasi, including BHU, Lanka, Assi, Sigra and nearby areas. Share your locality when you book."),
            ("How long does laundry take?",
             "Most laundry orders are delivered within about 4 days of pickup. You receive the expected delivery date on WhatsApp."),
            ("How do I pay for laundry?",
             "Pay by any UPI app using the link we send on WhatsApp, or pay at delivery."),
        ],
    },
    "steam-ironing-varanasi": {
        "name": "Steam Ironing",
        "title": "Steam Ironing & Press Service in Varanasi (Banaras)",
        "description": "Steam ironing and pressing service in Varanasi with pickup and delivery. Crisp, wrinkle-free shirts, trousers, kurtas and sarees at per-piece prices.",
        "h1": "Steam ironing service in Varanasi",
        "lead": "Crisp, wrinkle-free steam pressing for clothes that are already clean — shirts, trousers, kurtas and sarees, picked up and delivered across Varanasi.",
        "rates": ["iron"],
        "faq": [
            ("Is steam ironing better than regular ironing?",
             "Steam relaxes the fibres, so creases come out evenly with less heat on the fabric. It is gentler on cotton, linen and blends and gives a sharper finish."),
            ("Can I send only ironing without washing?",
             "Yes. Steam ironing is a separate service for clothes that are already clean, charged per piece."),
            ("Do you iron sarees?",
             "Yes, including cotton and silk sarees, pressed and folded carefully."),
        ],
    },
    "blanket-curtain-cleaning-varanasi": {
        "name": "Blanket & Curtain Cleaning",
        "title": "Blanket, Quilt & Curtain Cleaning in Varanasi (Banaras)",
        "description": "Blanket, quilt (razai), comforter and curtain cleaning in Varanasi with doorstep pickup. Thorough cleaning, fresh and dry delivery before winter.",
        "h1": "Blanket, quilt and curtain cleaning in Varanasi",
        "lead": "Bulky home linen is hard to wash at home. We clean blankets, quilts (razai), comforters, curtains and bedsheets thoroughly and deliver them fresh and fully dry across Varanasi.",
        "rates": ["home"],
        "faq": [
            ("How long does blanket cleaning take?",
             "Heavy items such as blankets, quilts and curtains usually take up to 7 days so they are cleaned and dried completely."),
            ("Do you remove and re-hang curtains?",
             "We clean curtains that are handed over at pickup. Please remove hooks and rings before pickup where possible."),
            ("When should I get blankets cleaned?",
             "Before winter, to air out stored blankets, and at the end of winter before storing them away."),
        ],
    },
}

# Main page ke "Areas we serve" aur service pages dono — Varanasi ke mohalle
AREAS = [
    "Sundarpur", "Lanka", "BHU", "Assi", "Nagwa", "Bhelupur", "Durgakund", "Sigra",
    "Mahmoorganj", "Rathyatra", "Luxa", "Godowlia", "Maldahiya", "Nadesar", "Cantt",
    "Shivpur", "Pandeypur", "Sarnath", "Chitaipur", "Manduadih", "Lahartara", "Kamachha",
]
