# Deploying Untangle on Render, with Supabase + Razorpay

Frontend and backend both live on Render. User accounts live in Supabase
Postgres. Payments go through a Razorpay Payment Link. Total cost to get
live: **$0** — all three have free tiers, and nothing charges you until a
real customer actually pays.

---

## Part 1 — Get the code on GitHub

1. Create a free GitHub account if you don't have one: https://github.com
2. Create a new repository (e.g. `untangle`), and push these files to it:
   `backend.py`, `db.py`, `requirements.txt`, `index.html`.
   - Easiest path if you're not comfortable with git: on the repo page, use
     **Add file → Upload files** and drag them in directly through the browser.

---

## Part 2 — Set up Supabase (free Postgres database)

1. Sign up at https://supabase.com — free, no card required.
2. **New Project**. Pick a name, set a database password (save it
   somewhere — you'll need it in a second), and choose a region close to
   your users.
3. Once it's created, go to **Project Settings → Database → Connection
   string**. Choose the **Connection Pooling** URI (not "Direct
   connection") — it's built for exactly this kind of always-on small
   backend. Copy it — it looks like:
   ```
   postgresql://postgres.xxxxxxxxxxxx:[YOUR-PASSWORD]@aws-0-region.pooler.supabase.com:6543/postgres
   ```
4. Replace `[YOUR-PASSWORD]` in that string with the database password
   from step 2. This full string is your `DATABASE_URL`.

   **Already have the separate pieces instead** (`SUPABASE_DB_HOST`,
   `SUPABASE_DB_PORT`, `SUPABASE_DB_NAME`, `SUPABASE_DB_USER`,
   `SUPABASE_DB_PASSWORD`)? That's fine too — `db.py` builds the connection
   string from those automatically if `DATABASE_URL` isn't set. Just set
   all five as environment variables on Render instead of `DATABASE_URL`,
   using the values from Supabase's **Connect** panel (the "Connection
   parameters" tab shows these individually rather than as one string).

You don't need to manually create the `users` table — `db.py` does this
automatically the first time the backend starts (`CREATE TABLE IF NOT
EXISTS`). If you'd rather create it yourself in Supabase's **SQL Editor**,
here's the exact schema it uses:

```sql
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    created_at DATE DEFAULT CURRENT_DATE NOT NULL,
    attempts INT DEFAULT 0,
    is_premium BOOLEAN NOT NULL DEFAULT FALSE,
    last_updated DATE DEFAULT CURRENT_DATE NOT NULL,
    end_date DATE
);
```

---

## Part 3 — Deploy the backend to Render (free)

1. Sign up at https://render.com (no credit card required for the free tier).
2. Click **New → Web Service**, connect GitHub, and pick your `untangle` repo.
3. Configure it:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn backend:app`
   - **Instance Type**: Free
4. Add environment variables (your service → **Environment**):
   - `GEMINI_API_KEY` = your Gemini API key
   - `DATABASE_URL` = the Supabase connection string from Part 2
     (or the five separate `SUPABASE_DB_*` variables instead — see Part 2)
   - `ADMIN_KEY` = make up any secret string — this lets you manually
     unlock a user's account later if a payment webhook ever misfires
   - `RAZORPAY_WEBHOOK_SECRET` = leave blank for now, filled in during Part 5
5. Click **Deploy**. Render gives you a live URL, e.g.
   `https://untangle-backend.onrender.com` — **this is the link you paste
   into `index.html`** (see next step). It sleeps after 15 minutes idle and
   takes ~30-50 seconds to wake up on the next request — fine for early users.
6. Check it worked: visit `https://your-backend-url.onrender.com/health` —
   you should see `"db_configured": true` and `"key_configured": true`.

---

## Part 4 — Deploy the frontend, also on Render (free)

1. Open `index.html` and find this line near the top of the `<script>` block
   (it's clearly marked with a comment):
   ```js
   const BACKEND_BASE = 'http://localhost:5000';
   ```
   Replace it with your real Render backend URL from Part 3:
   ```js
   const BACKEND_BASE = 'https://untangle-backend.onrender.com';
   ```
   This is the **only** place you need to paste that link.
2. Push the updated `index.html` back to your GitHub repo.
3. In Render: **New → Static Site**, connect the same repo.
   - **Build Command**: leave blank
   - **Publish Directory**: `.` (the root, since `index.html` sits there)
4. Deploy. Render gives you a second URL, e.g.
   `https://untangle.onrender.com` — that's the link you share with users.

At this point the app is fully live, with a working free tier (3 organizes
total per account before it asks you to upgrade).

---

## Part 5 — Add real payments with a Razorpay Payment Link (no money, no code)

### 5a. Create your Razorpay account
1. Sign up at https://dashboard.razorpay.com/signup — free.
2. Stay in **Test Mode** for now (toggle top-right of dashboard).

### 5b. Create the Payment Link
1. **Payment Products → Payment Links → + Create Payment Link → Standard
   Payment Link**.
2. Fill in:
   - **Amount**: ₹399 (or whatever you decide)
   - **Currency**: **INR** — international currencies need a separate
     activation step, skip that for now
   - **Payment For**: e.g. "Untangle — 1 month unlimited"
   - Leave Customer Details blank — this is one shared link everyone uses,
     each person enters their own info at checkout
3. Copy the link (looks like `https://rzp.io/rzp/abcXYZ12`) and paste it
   into `index.html`:
   ```js
   const RAZORPAY_PAYMENT_LINK = 'PASTE_YOUR_RAZORPAY_PAYMENT_LINK_HERE';
   ```
   Push the change back to GitHub so your Render static site picks it up.

> Note: this is a one-time payment, not auto-recurring billing — simplest
> to set up, and fine for an MVP. Since `end_date` is set to 29 days out,
> customers naturally need to pay again roughly monthly. Auto-recurring
> billing would need Razorpay's separate Subscriptions API, a bigger step
> for later.

### 5c. Set up the webhook (make sure you're in Test Mode when you do this)
Razorpay keeps **separate webhooks for Test Mode and Live Mode** — double
check the toggle before creating this.

1. **Account & Settings → Webhooks → + Add New Webhook**.
2. Webhook URL: `https://untangle-backend.onrender.com/webhook` (your real
   Render backend URL + `/webhook`).
3. Set a **Secret** (any string you make up) — this goes in Render's
   `RAZORPAY_WEBHOOK_SECRET`. Note: setting up a webhook in Test Mode
   prompts for a confirmation OTP — the fixed test OTP is `754081`.
4. Check the event: **Payment Links → payment_link.paid**.
5. Save, then add `RAZORPAY_WEBHOOK_SECRET` to Render's environment
   variables and redeploy.

### 5d. Test it end-to-end (still no real money)
1. Open your live frontend, enter an email, organize 3 times to use up the
   free attempts, then click **Upgrade**.
2. On the Razorpay page, enter the **same email**, then pay with a test
   card: `4111 1111 1111 1111` (Visa, domestic), any future expiry, any
   CVC. If cards don't work, try UPI with `success@razorpay` as the UPI ID.
3. Back in the app, that email's plan pill should now say **Premium**. If
   it doesn't, check the webhook's delivery log in the Razorpay dashboard
   (make sure you're viewing it in **Test Mode**) to see whether it fired
   and what response your backend gave.

### 5e. If a payment succeeds but the account doesn't unlock
Use the manual override instead of digging further right away:
```bash
curl -X POST https://untangle-backend.onrender.com/admin/grant-premium \
  -H "X-Admin-Key: YOUR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"email": "the-customer@example.com"}'
```
This unlocks them immediately while you debug the webhook separately.

### 5f. Go live for real
1. Switch Razorpay's dashboard from **Test Mode** to **Live Mode**.
2. Razorpay will ask for KYC (business/bank details) the first time you
   activate live payments — standard, free, only required once.
3. Repeat 5b and 5c in Live Mode (test and live are separate environments)
   to get a live Payment Link and a live webhook secret.
4. Paste the live Payment Link into `index.html`, and update
   `RAZORPAY_WEBHOOK_SECRET` on Render with the live webhook secret.

---

## What you now have

- A live, properly-named app (Untangle) hosted entirely on Render for $0/month
- Real user accounts in Supabase Postgres — durable, queryable, not flat
  files that could get wiped on a redeploy
- Premium that **actually expires** after 29 days and reverts to free
  automatically, no cleanup job needed
- A `/status` endpoint the UI uses to show live plan info (attempts left,
  or days left) without waiting for the user to hit a wall first
- A manual admin override (`/admin/grant-premium`) for whenever a webhook
  needs a human backstop
- A real Razorpay payment flow, set up entirely from the dashboard,
  costing you nothing until a customer actually pays
