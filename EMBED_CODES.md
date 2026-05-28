# Reddit Reviews Widget — Embed Codes

Use one of the following tested embed codes to add the widget to your WordPress site or any other platform. All versions use a fixed `height` attribute instead of the `position: absolute` / `padding-bottom` responsive wrapper, which was causing layout and API issues.

---

## 1. Basic Working Code ✅ (Recommended)

The simplest option. Works on any platform with no wrapper needed.

```html
<iframe 
  src="https://reddit-airlucent-api-production.up.railway.app/widget.html"
  width="100%"
  height="1200"
  frameborder="0"
  allow="none"
  style="border: none; border-radius: 8px;"
  title="Real Reddit Reviews Widget"
></iframe>
```

---

## 2. WordPress Custom HTML Block Code

Paste this directly into a **Custom HTML** block in the WordPress editor. The `max-width` container keeps the widget from stretching too wide on large screens.

```html
<!-- Paste this in WordPress Custom HTML block -->
<div style="width: 100%; max-width: 1400px; margin: 0 auto;">
  <iframe 
    src="https://reddit-airlucent-api-production.up.railway.app/widget.html"
    width="100%"
    height="1200"
    frameborder="0"
    allow="none"
    style="border: none; border-radius: 8px; display: block;"
    title="Real Reddit Reviews Widget"
  ></iframe>
</div>
```

---

## 3. Responsive with Container

Adds padding and a subtle box shadow for a polished look. Good for landing pages.

```html
<div style="width: 100%; max-width: 1400px; margin: 2rem auto; padding: 0 1rem;">
  <iframe 
    src="https://reddit-airlucent-api-production.up.railway.app/widget.html"
    width="100%"
    height="1200"
    frameborder="0"
    allow="none"
    style="border: none; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); display: block;"
    title="Real Reddit Reviews Widget"
  ></iframe>
</div>
```

---

## 4. Mobile-Optimized Code

Minimal margins and `max-width: 100%` on the iframe itself to prevent overflow on small screens.

```html
<div style="width: 100%; margin: 1rem 0;">
  <iframe 
    src="https://reddit-airlucent-api-production.up.railway.app/widget.html"
    width="100%"
    height="1200"
    frameborder="0"
    allow="none"
    style="border: none; border-radius: 8px; display: block; max-width: 100%;"
    title="Real Reddit Reviews Widget"
  ></iframe>
</div>
```

---

## ⚠️ What NOT to Use

The following pattern uses `position: absolute` and a `padding-bottom` percentage trick. **Do not use this** — it gives the iframe incorrect dimensions, which breaks the widget layout and causes API fetch failures.

```html
<!-- ❌ BROKEN — do not use -->
<div style="position: relative; padding-bottom: 56.25%; height: 0; overflow: hidden;">
  <iframe style="position: absolute; top: 0; left: 0; width: 100%; height: 100%;"
    src="https://reddit-airlucent-api-production.up.railway.app/widget.html">
  </iframe>
</div>
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Failed to fetch products" | Verify `ALLOWED_ORIGIN` is set correctly in Railway environment variables |
| Activity feed is empty | Wait a few minutes — data loads asynchronously on first render |
| Widget is completely blank | Open browser DevTools console and check for CORS errors |
| Height is cut off | Increase the `height` attribute — try `1400` or `1600` |
| Widget looks narrow on desktop | Increase `max-width` on the wrapper `div` (e.g. `1400px` or `100%`) |

---

## Widget Details

- **URL**: `https://reddit-airlucent-api-production.up.railway.app/widget.html`
- **API key**: Pre-configured inside `widget.html` — no changes needed
- **Features**: Product listings, filters, Reddit activity feed
